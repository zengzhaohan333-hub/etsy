import io
import json
from email.message import Message
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from okx_bot.web import PaperProcess, handler_for


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "config.toml"
        self.config.write_text("", encoding="utf-8")
        self.manager = PaperProcess(self.config, self.root / "state")

    def request(self, method, path, headers=None, body=b"{}"):
        cls = handler_for(self.manager, "session-token")
        handler = cls.__new__(cls)
        handler.server = SimpleNamespace(server_address=("127.0.0.1", 8765))
        handler.headers = Message()
        defaults = {"Host": "127.0.0.1:8765", "Content-Type": "application/json",
                    "Content-Length": str(len(body)), "X-CSRF-Token": "session-token"}
        defaults.update(headers or {})
        for k, v in defaults.items():
            if v is not None:
                handler.headers[k] = v
        handler.path = path
        handler.rfile = io.BytesIO(body)
        handler.wfile = io.BytesIO()
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()
        getattr(handler, "do_" + method)()
        code = handler.send_response.call_args.args[0]
        return code, handler.wfile.getvalue(), handler

    def test_static_dashboard_has_token_and_no_external_assets(self):
        code, body, _ = self.request("GET", "/")
        self.assertEqual(code, 200)
        self.assertIn(b'session-token', body)
        self.assertNotIn(b'__CSRF_TOKEN__', body)
        self.assertNotIn(b'<script src=', body)
        self.assertIn("无真实资金".encode(), body)

    def test_status_loads_worker_state(self):
        self.manager.state_dir.mkdir()
        (self.manager.state_dir / "status.json").write_text('{"equity":"999.12"}')
        code, body, _ = self.request("GET", "/api/status")
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["state"]["equity"], "999.12")
        self.assertFalse(json.loads(body)["running"])

    def test_foreign_host_rejected_on_reads_and_writes(self):
        for method, path in [("GET", "/"), ("POST", "/api/start")]:
            self.assertEqual(self.request(method, path, {"Host": "evil.example:8765"})[0], 403)

    def test_foreign_origin_rejected_even_with_token(self):
        code, _, _ = self.request("POST", "/api/start", {"Origin": "https://evil.example"})
        self.assertEqual(code, 403)
        self.assertIsNone(self.manager.process)

    def test_missing_or_wrong_token_rejected(self):
        for token in [None, "incorrect", "é"]:
            self.assertEqual(self.request("POST", "/api/start", {"X-CSRF-Token": token})[0], 403)

    def test_json_only_and_no_user_command_parameters(self):
        self.assertEqual(self.request("POST", "/api/start", {"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.request("POST", "/api/start", body=b'{"mode":"live"}')[0], 400)
        self.assertEqual(self.request("POST", "/api/start", body=b'not json')[0], 400)
        self.assertEqual(self.request("POST", "/api/start", {"Content-Length": "9000"})[0], 413)

    @patch("okx_bot.web.subprocess.Popen")
    def test_start_is_paper_only_and_idempotent(self, popen):
        child = popen.return_value
        child.poll.return_value = None
        code, body, _ = self.request("POST", "/api/start", {"Origin": "http://127.0.0.1:8765"})
        self.assertEqual(code, 200)
        self.assertTrue(json.loads(body)["started"])
        command = popen.call_args.args[0]
        self.assertEqual(command[1:4], ["-m", "okx_bot", "paper"])
        self.assertNotIn("demo", command)
        self.assertNotIn("live", command)
        self.assertNotIn("shell", popen.call_args.kwargs)
        _, body, _ = self.request("POST", "/api/start")
        self.assertFalse(json.loads(body)["started"])
        popen.assert_called_once()
        code, body, _ = self.request("POST", "/api/stop")
        self.assertEqual(code, 200)
        self.assertTrue(json.loads(body)["stopped"])
        child.terminate.assert_called_once()
        child.wait.assert_called_once_with(timeout=5)

    def test_stop_never_signals_unknown_process(self):
        self.assertFalse(self.manager.stop())

    def test_corrupt_state_is_an_error_not_a_false_success(self):
        self.manager.state_dir.mkdir()
        (self.manager.state_dir / "status.json").write_text("not json")
        state = self.manager.status()
        self.assertIsNone(state["state"])
        self.assertTrue(state["error"])
        (self.manager.state_dir / "status.json").write_text('{"equity":"1000"}')
        self.assertIsNone(self.manager.status()["error"])


if __name__ == "__main__":
    unittest.main()
