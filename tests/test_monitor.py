"""Read-only monitoring checks. All files are local fixtures, no exchange access."""
import io
import json
from email.message import Message
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from okx_bot.monitor import DemoMonitor, PORTFOLIO_INSTRUMENTS, handler_for


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

    def test_tls_halt_subtypes_have_distinct_safe_messages(self):
        cases = (
            ("tls", "certificate", ("证书验证失败", "系统时间", "证书链", "保留证书校验")),
            ("tls", "handshake", ("TLS 握手失败", "代理连接", "保留证书校验")),
            ("transport", "unexpected_eof", ("TLS 连接被提前关闭", "有限读取重试", "代理稳定性")),
        )
        for category, subtype, expected in cases:
            with self.subTest(category=category, subtype=subtype):
                self.state.update(halted=True, halt_category=category, halt_tls_failure=subtype,
                                  halt_reason="test-secret TLS raw exception test-key")
                self.write()
                result = self.monitor.snapshot(self.now)
                for text in expected:
                    self.assertIn(text, result["halt_message"])
                self.assertNotIn("test-secret", json.dumps(result))
                self.assertNotIn("test-key", json.dumps(result))
                if subtype == "unexpected_eof":
                    self.assertNotIn("证书验证失败", result["halt_message"])

    def test_tls_unknown_or_mismatched_subtype_does_not_invent_a_diagnosis(self):
        cases = (("tls", "unexpected_eof"), ("transport", "certificate"), ("timeout", "handshake"),
                 ("tls", "certificate test-secret"), ("tls", None), ("tls", ["certificate"]),
                 ("tls", {"message": "test-secret"}))
        for category, subtype in cases:
            with self.subTest(category=category, subtype=subtype):
                self.state.update(halted=True, halt_category=category, halt_tls_failure=subtype,
                                  halt_reason="test-key")
                self.write()
                result = self.monitor.snapshot(self.now)
                self.assertNotIn("TLS 连接被提前关闭", result["halt_message"])
                self.assertNotIn("证书验证失败", result["halt_message"])
                if category == "tls":
                    self.assertIn("证书校验或握手失败", result["halt_message"])
                self.assertNotIn("test-secret", json.dumps(result))
                self.assertNotIn("test-key", json.dumps(result))

    def test_connection_only_exposes_valid_tls_diagnostic_scalars(self):
        self.state["connection"] = {"state": "retrying", "attempt": 1, "max_attempts": 3,
                                    "category": "transport", "tls_failure": "unexpected_eof",
                                    "api_code": "50013", "http_status": 503,
                                    "message": "test-secret", "exception": "test-key"}
        self.write()
        result = self.monitor.snapshot(self.now)
        self.assertEqual(result["connection"], {"state": "retrying", "attempt": 1, "max_attempts": 3,
                                               "category": "transport", "tls_failure": "unexpected_eof",
                                               "api_code": "50013", "http_status": 503})
        self.assertNotIn("test-secret", json.dumps(result))
        self.assertNotIn("test-key", json.dumps(result))
        for category, subtype in (("tls", "unexpected_eof"), ("transport", "certificate"),
                                  ("tls test-secret", "certificate")):
            with self.subTest(category=category, subtype=subtype):
                self.state["connection"].update(category=category, tls_failure=subtype,
                                                api_code="50013 test-secret", http_status="503")
                self.write()
                clean = self.monitor.snapshot(self.now)["connection"]
                self.assertNotIn("tls_failure", clean)
                self.assertNotIn("api_code", clean)
                self.assertNotIn("http_status", clean)
                self.assertNotIn("test-secret", json.dumps(clean))

    def test_user_stopped_state_is_distinct_from_halt_and_stale_health(self):
        self.state.update(phase="stopped", connection={"state": "connected", "attempt": 0, "max_attempts": 3})
        self.write()
        result = self.monitor.snapshot(self.now + 100_000)
        self.assertEqual(result["health"], "stopped")
        self.assertTrue(result["stale"])
        self.assertFalse(result["halted"])
        self.assertEqual(result["equity_curve"], [])

    def portfolio(self):
        self.state.update(scope="portfolio", instruments=list(PORTFOLIO_INSTRUMENTS), bar="5m",
                          phase="running", last_success_at_ms=self.now,
                          connection={"state": "connected", "attempt": 0, "max_attempts": 3},
                          markets={instrument: {"position": None, "entries": 0, "phase": "running",
                                                "last_success_at_ms": self.now}
                                   for instrument in PORTFOLIO_INSTRUMENTS})

    def test_portfolio_keeps_independent_positions_and_one_account_equity(self):
        self.portfolio()
        self.state["entries"] = 3
        self.state.update(gross_notional="350", max_total_notional="800", max_notional_per_market="200")
        self.state["markets"]["BTC-USDT-SWAP"].update(entries=1, position={
            "side": "long", "contracts": ".12", "entry_price": "70000", "secret": "test-key"},
            phase="holding", notional="150", mark_price="70000")
        self.state["markets"]["ETH-USDT-SWAP"].update(entries=2, position={
            "side": "short", "contracts": "2", "entry_price": "2000"})
        self.state["markets"]["SOL-USDT-SWAP"]["last_success_at_ms"] = self.now - 100_000
        self.write()
        before = self.status.read_bytes()
        result = self.monitor.snapshot(self.now)
        self.assertTrue(result["ok"])
        self.assertEqual(result["scope"], "portfolio")
        self.assertEqual(result["bar"], "5m")
        self.assertEqual(result["equity"], "5000")
        self.assertEqual(result["entries"], 3)
        self.assertEqual(result["gross_notional"], "350")
        self.assertEqual(result["max_total_notional"], "800")
        self.assertEqual(result["max_notional_per_market"], "200")
        self.assertEqual(len(result["equity_curve"]), 1)
        self.assertIsNone(result["position"])
        self.assertEqual(result["markets"]["BTC-USDT-SWAP"]["position"]["side"], "long")
        self.assertEqual(result["markets"]["BTC-USDT-SWAP"]["phase"], "holding")
        self.assertEqual(result["markets"]["BTC-USDT-SWAP"]["notional"], "150")
        self.assertEqual(result["markets"]["ETH-USDT-SWAP"]["position"]["side"], "short")
        self.assertEqual(result["markets"]["ETH-USDT-SWAP"]["entries"], 2)
        self.assertIsNone(result["markets"]["XRP-USDT-SWAP"]["position"])
        self.assertEqual(result["markets"]["SOL-USDT-SWAP"]["last_success_at_ms"], self.now - 100_000)
        self.assertTrue(all("equity" not in row for row in result["markets"].values()))
        self.assertNotIn("test-key", json.dumps(result))
        self.assertEqual(self.status.read_bytes(), before)

    def test_portfolio_config_uses_portfolio_file_and_journal_instrument_is_whitelisted(self):
        self.portfolio()
        self.write()
        (self.root / "config.paper.toml").write_text('bar="15m"\nmax_notional="999"\n')
        (self.root / "config.portfolio.toml").write_text(
            'bar="5m"\nfast_window=5\nslow_window=20\nmax_notional="200"\n'
            'max_total_notional="800"\nmax_drawdown_pct=".05"\nsecret="test-secret"\n')
        journal = self.directory / "demo-journal.json"
        journal.write_text(json.dumps({"version": 1, "orders": [
            {"instrument": "ETH-USDT-SWAP", "status": "protected", "side": "short", "size": "1"},
            {"instrument": "test-secret", "status": "closed", "side": "long"}]}))
        result = self.monitor.snapshot(self.now)
        self.assertEqual(result["profile"]["bar"], "5m")
        self.assertEqual(result["profile"]["max_notional"], "200")
        self.assertEqual(result["profile"]["max_total_notional"], "800")
        self.assertEqual(result["orders"][1]["instrument"], "ETH-USDT-SWAP")
        self.assertNotIn("instrument", result["orders"][0])
        self.assertNotIn("test-secret", json.dumps(result))

    def test_portfolio_missing_or_unresolved_records_never_look_flat(self):
        self.portfolio()
        del self.state["markets"]["SOL-USDT-SWAP"]
        self.state["markets"]["ETH-USDT-SWAP"].update(entries=True, position={"side": "long"},
                                                                   phase="test-secret")
        self.state["markets"]["DOGE-USDT-SWAP"] = {"secret": "test-key"}
        self.write()
        journal = self.directory / "demo-journal.json"
        journal.write_text(json.dumps({"version": 1, "orders": [
            {"instrument": "XRP-USDT-SWAP", "status": "inspect_required", "side": "long"}]}))
        result = self.monitor.snapshot(self.now)
        self.assertTrue(result["ok"])
        for instrument in ("SOL-USDT-SWAP", "ETH-USDT-SWAP", "XRP-USDT-SWAP"):
            self.assertTrue(result["markets"][instrument]["position"]["uncertain"])
        self.assertIsNone(result["markets"]["ETH-USDT-SWAP"]["entries"])
        self.assertIsNone(result["markets"]["ETH-USDT-SWAP"]["phase"])
        self.assertIn("不能将缺失记录当作空仓", result["warning"])
        self.assertNotIn("DOGE-USDT-SWAP", json.dumps(result))
        self.assertNotIn("test-secret", json.dumps(result))
        self.assertNotIn("test-key", json.dumps(result))

    def test_portfolio_rejects_unexpected_instrument_membership(self):
        self.portfolio()
        self.state["instruments"][-1] = "DOGE-USDT-SWAP"
        self.write()
        self.assertFalse(self.monitor.snapshot(self.now)["ok"])

    def test_balance_query_time_controls_curve_sampling_not_cycle_heartbeat(self):
        self.portfolio()
        self.state["equity_checked_at_ms"] = self.now - 120_000
        self.write()
        result = self.monitor.snapshot(self.now)
        self.assertEqual(result["equity_checked_at_ms"], self.now - 120_000)
        self.assertEqual(result["health"], "updating")
        self.assertEqual(result["equity_curve"], [])
        self.state["equity_checked_at_ms"] = self.now - 10_000
        self.write()
        result = self.monitor.snapshot(self.now)
        self.assertEqual(result["equity_curve"], [{"ts": self.now - 10_000, "equity": "5000"}])
        self.state.update(updated_at_ms=self.now + 5000, last_success_at_ms=self.now + 5000,
                          equity_checked_at_ms=self.now - 10_000)
        self.write()
        self.assertEqual(len(self.monitor.snapshot(self.now + 5000)["equity_curve"]), 1)

    def test_fresh_balance_requires_fresh_complete_cycle_and_connected_state(self):
        self.portfolio()
        self.state.update(equity_checked_at_ms=self.now, last_success_at_ms=self.now - 120_000)
        self.write()
        self.assertEqual(self.monitor.snapshot(self.now)["equity_curve"], [])
        self.state["last_success_at_ms"] = self.now
        for phase, connection_state in (("running", "retrying"), ("starting", "connected"),
                                        ("running", "checking"), ("stopped", "connected")):
            with self.subTest(phase=phase, connection=connection_state):
                self.state["phase"] = phase
                self.state["connection"]["state"] = connection_state
                self.write()
                self.assertEqual(self.monitor.snapshot(self.now)["equity_curve"], [])

    def test_invalid_explicit_balance_timestamp_is_not_replaced_by_fresh_heartbeat(self):
        self.portfolio()
        for timestamp in (True, "test-secret", self.now + 99_999_999_999_999, [self.now], None):
            with self.subTest(timestamp=timestamp):
                self.state["equity_checked_at_ms"] = timestamp
                self.write()
                result = self.monitor.snapshot(self.now)
                self.assertIsNone(result["equity_checked_at_ms"])
                self.assertEqual(result["equity_curve"], [])
                self.assertNotIn("test-secret", json.dumps(result))

    def test_unknown_pending_portfolio_order_does_not_claim_any_market_flat(self):
        self.portfolio()
        self.write()
        (self.directory / "demo-journal.json").write_text(json.dumps({"version": 1, "orders": [
            {"status": "intent", "side": "long"}]}))
        result = self.monitor.snapshot(self.now)
        self.assertTrue(all(value["position"]["uncertain"] for value in result["markets"].values()))

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
