"""Read-only monitoring checks. All files are local fixtures, no exchange access."""
import io
import json
from email.message import Message
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from okx_bot.monitor import DemoMonitor, handler_for


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.directory = self.root / ".runtime-demo"
        self.directory.mkdir()
        self.status = self.directory / "status.json"
        self.now = 1_793_338_450_000
        self.state = {"mode": "demo", "halted": False, "halt_reason": "test-secret",
                      "equity": "5000", "entries": 0, "position": None,
                      "updated_at_ms": self.now, "api_key": "test-key"}
        self.write()
        self.monitor = DemoMonitor(self.status)

    def write(self):
        self.status.write_text(json.dumps(self.state), encoding="utf-8")

    def request(self, method="GET", path="/api/status", headers=None, report=None):
        cls = handler_for(self.monitor, report)
        handler = cls.__new__(cls)
        handler.server = SimpleNamespace(server_address=("127.0.0.1", 8766))
        handler.headers = Message()
        for key, value in {"Host": "127.0.0.1:8766", **(headers or {})}.items():
            if value is not None:
                handler.headers[key] = value
        handler.path, handler.wfile = path, io.BytesIO()
        handler.send_response, handler.send_header, handler.end_headers = Mock(), Mock(), Mock()
        getattr(handler, "do_" + method)()
        return handler.send_response.call_args.args[0], handler.wfile.getvalue(), handler

    def test_reads_legacy_robot_without_keys_and_never_modifies_source(self):
        before = self.status.read_bytes()
        with patch("okx_bot.okx.OkxClient", side_effect=AssertionError("no exchange client")), \
             patch("subprocess.Popen", side_effect=AssertionError("no robot process")):
            result = self.monitor.snapshot(self.now)
        self.assertTrue(result["ok"])
        self.assertEqual(result["health"], "updating")
        self.assertEqual(result["equity"], "5000")
        self.assertEqual(result["entries"], 0)
        self.assertIsNone(result["position"])
        self.assertEqual(self.status.read_bytes(), before)
        self.assertEqual(list(self.directory.iterdir()), [self.status])
        self.assertNotIn("test-secret", json.dumps(result))
        self.assertNotIn("test-key", json.dumps(result))

    def test_stale_or_future_heartbeat_never_looks_current(self):
        for clock in (self.now + 90_001, self.now - 5_001):
            with self.subTest(clock=clock):
                result = self.monitor.snapshot(clock)
                self.assertTrue(result["stale"])
                self.assertEqual(result["health"], "stale")
        self.state["halted"] = True
        self.write()
        self.assertEqual(self.monitor.snapshot(self.now)["health"], "halted")

    def test_corrupt_missing_paper_or_invalid_file_is_not_a_flat_healthy_account(self):
        for raw in ('not json', '{"mode":"paper"}', '[]', '{"mode":"demo","halted":false}'):
            with self.subTest(raw=raw):
                self.status.write_text(raw)
                result = self.monitor.snapshot(self.now)
                self.assertFalse(result["ok"])
                self.assertNotIn("equity", result)
        self.status.unlink()
        self.assertFalse(self.monitor.snapshot(self.now)["ok"])
        with self.assertRaises(ValueError):
            DemoMonitor(self.directory / "credentials.json")

    def test_positions_and_journal_are_scalar_filtered_and_never_report_synthetic_pnl(self):
        self.state["position"] = {"side": "short", "contracts": ".1", "entry_price": "77000", "secret": "test-key"}
        self.write()
        rows = [{"side": "short", "size": ".1", "stop_loss": "77770", "take_profit": "75460",
                 "created_at_ms": self.now, "order_id": "123456789", "status": "protected",
                 "reason": "test-secret", "api_key": "test-key"},
                {"side": "long", "status": "closed", "never_submitted": True,
                 "order_id": "test-secret", "size": "NaN"}]
        journal = self.directory / "demo-journal.json"
        journal.write_text(json.dumps({"version": 1, "orders": rows}))
        original = journal.read_bytes()
        result = self.monitor.snapshot(self.now)
        self.assertEqual(result["position"], {"side": "short", "contracts": "0.1", "entry_price": "77000", "uncertain": False})
        self.assertEqual(result["orders"][0]["status"], "never_submitted")
        self.assertIsNone(result["orders"][0]["size"])
        self.assertEqual(result["orders"][1]["order_id"], "123456789")
        self.assertNotIn("pnl", result["orders"][1])
        self.assertEqual(journal.read_bytes(), original)
        self.assertNotIn("test-secret", json.dumps(result))
        self.assertNotIn("test-key", json.dumps(result))

    def test_unverified_position_remains_explicitly_uncertain(self):
        self.state.update(halted=True, position={"side": "long", "contracts": "1",
                          "status": "unknown; inspect exchange"})
        self.write()
        result = self.monitor.snapshot(self.now)
        self.assertTrue(result["position"]["uncertain"])
        self.assertEqual(result["health"], "halted")

    def test_curve_only_adds_new_fresh_timestamps_and_keeps_max_2000_points(self):
        self.monitor.snapshot(self.now)
        self.monitor.snapshot(self.now)
        self.assertEqual(len(self.monitor.curve), 1)
        self.state.update(updated_at_ms=self.now + 15_000, equity="5001")
        self.write()
        result = self.monitor.snapshot(self.now + 15_000)
        self.assertEqual(len(result["equity_curve"]), 2)
        self.assertEqual(result["equity_curve"][-1]["equity"], "5001")
        self.assertEqual(self.monitor.curve.maxlen, 2000)
        self.monitor.snapshot(self.now + 200_000)
        self.assertEqual(len(self.monitor.curve), 2)

    def test_number_exponents_and_unknown_config_fields_are_not_exposed(self):
        self.state["equity"] = "1e-999999999"
        self.write()
        (self.root / "config.paper.toml").write_text('instrument="BTC-USDT-SWAP"\nbar="15m"\nfast_window=5\nslow_window=20\nmax_notional="200"\nsecret="test-secret"\n')
        result = self.monitor.snapshot(self.now)
        self.assertIsNone(result["equity"])
        self.assertEqual(result["profile"]["fast_window"], 5)
        self.assertNotIn("test-secret", json.dumps(result))

    def test_retry_heartbeat_does_not_look_like_a_fresh_account_sample(self):
        self.state.update(phase="running", connection={"state": "retrying", "attempt": 2, "max_attempts": 3,
                          "secret": "test-secret"}, last_success_at_ms=self.now - 100_000)
        self.write()
        result = self.monitor.snapshot(self.now)
        self.assertEqual(result["health"], "retrying")
        self.assertFalse(result["stale"])
        self.assertEqual(result["equity_curve"], [])
        self.assertNotIn("test-secret", json.dumps(result))
        self.state["connection"]["state"] = "checking"
        self.write()
        self.assertEqual(self.monitor.snapshot(self.now)["health"], "checking")
        self.state["connection"]["state"] = "connected"
        self.state["last_success_at_ms"] = self.now
        self.write()
        result = self.monitor.snapshot(self.now)
        self.assertEqual(result["health"], "updating")
        self.assertEqual(len(result["equity_curve"]), 1)

    def test_controlled_halt_messages_help_legacy_and_new_robots_without_raw_errors(self):
        self.state.update(halted=True, halt_reason="OKX HTTPS request failed; check network, time and demo credentials")
        self.write()
        self.assertIn("HTTPS", self.monitor.snapshot(self.now)["halt_message"])
        self.state.update(halt_api_code="50110", halt_reason="test-secret")
        self.write()
        self.assertIn("白名单", self.monitor.snapshot(self.now)["halt_message"])
        self.state.update(halt_api_code="50110 test-key", halt_category="timeout")
        self.write()
        result = self.monitor.snapshot(self.now)
        self.assertIn("超时", result["halt_message"])
        self.assertNotIn("test-secret", json.dumps(result))
        self.assertNotIn("test-key", json.dumps(result))

    def test_user_stopped_state_is_distinct_from_halt_and_stale_health(self):
        self.state.update(phase="stopped", connection={"state": "connected", "attempt": 0, "max_attempts": 3})
        self.write()
        result = self.monitor.snapshot(self.now + 100_000)
        self.assertEqual(result["health"], "stopped")
        self.assertTrue(result["stale"])
        self.assertFalse(result["halted"])
        self.assertEqual(result["equity_curve"], [])

    def test_local_page_nonce_and_read_only_routes(self):
        code, body, handler = self.request(path="/")
        self.assertEqual(code, 200)
        self.assertNotIn(b"__NONCE__", body)
        self.assertIn('欧易模拟盘'.encode(), body)
        self.assertNotIn(b'<script src=', body)
        headers = dict(call.args for call in handler.send_header.call_args_list)
        self.assertIn("nonce-", headers["Content-Security-Policy"])
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(self.request(method="POST", path="/api/start")[0], 405)
        for path in ("/../status.json", "/.env", "/api/status?file=secret", "/api/stop"):
            self.assertEqual(self.request(path=path)[0], 404)

    def test_foreign_hosts_origins_and_cross_site_requests_are_blocked(self):
        for headers in ({"Host": "evil.example:8766"}, {"Origin": "https://evil.example"},
                        {"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-site"}):
            with self.subTest(headers=headers):
                self.assertEqual(self.request(headers=headers)[0], 403)
        self.assertEqual(self.request(headers={"Host": "localhost:8766", "Origin": "http://localhost:8766"})[0], 200)

    def test_only_known_report_can_be_read_and_scripts_disabled_on_report_route(self):
        report = self.root / "report.html"
        report.write_text("<h1>Report</h1>")
        code, data, handler = self.request(path="/report", report=report)
        self.assertEqual(code, 200)
        self.assertEqual(data, b"<h1>Report</h1>")
        headers = dict(call.args for call in handler.send_header.call_args_list)
        self.assertNotIn("script-src", headers["Content-Security-Policy"])
        self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
        report.unlink()
        self.assertEqual(self.request(path="/report", report=report)[0], 404)


if __name__ == "__main__":
    unittest.main()
