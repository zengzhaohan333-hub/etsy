import contextlib
import getpass
import io
import json
import os
import signal
import tempfile
import threading
import time
import unittest
import urllib.error
import warnings
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from okx_bot import connect
from okx_bot.config import Config
from okx_bot.okx import Credentials, Instrument, OkxClient as RealOkxClient, OkxError


ENV = {"OKX_API_KEY": "example-key-for-tests", "OKX_API_SECRET": "example-secret-for-tests",
       "OKX_API_PASSPHRASE": "example-passphrase-for-tests"}


class FakeClient:
    def __init__(self):
        self.credentials = None
        self.account = {"posMode": "net_mode", "acctLv": "2"}
        self.balance = {"currency": "USDT", "equity": Decimal("1000"), "available": Decimal("900")}
        self.position_rows = []
        self.order_rows = []
        self.algo_rows = []
        self.calls = []
        self.instrument_info = Instrument("BTC-USDT-SWAP", Decimal("0.01"), Decimal("0.01"),
                                          Decimal("0.01"), Decimal("10000"))
        self.server_ms = time.time_ns() // 1_000_000

    def account_config(self):
        self.calls.append("account_config")
        return self.account

    def server_time(self):
        self.calls.append("server_time")
        return self.server_ms

    def instrument(self, inst_id):
        self.calls.append("instrument")
        return self.instrument_info

    def balances(self):
        self.calls.append("balances")
        return self.balance

    def positions(self, inst_id):
        self.calls.append("positions")
        return self.position_rows

    def pending_orders(self, inst_id):
        self.calls.append("pending_orders")
        return self.order_rows

    def pending_algo_orders(self, inst_id):
        self.calls.append("pending_algo_orders")
        return self.algo_rows


class ConnectTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient()
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.state = Path(self.temporary.name) / "demo-state"
        self.stdin = Mock()
        self.stdin.isatty.return_value = True
        self.entered = Mock(side_effect=list(ENV.values()))
        self.answer = Mock(return_value="")
        self.runner = Mock(return_value={"mode": "demo", "halted": False})
        def make_client(credentials):
            self.client.credentials = credentials
            return self.client
        self.factory = Mock(side_effect=make_client)
        patches = [
            patch("okx_bot.connect.OkxClient", self.factory),
            patch("okx_bot.connect.load_config", return_value=Config()),
            patch("okx_bot.connect.getpass.getpass", self.entered),
            patch("okx_bot.connect.run_demo", self.runner),
            patch("okx_bot.connect.sys.stdin", self.stdin),
            patch("builtins.input", self.answer),
            patch.dict(os.environ, {}, clear=True),
            contextlib.redirect_stdout(self.stdout),
            contextlib.redirect_stderr(self.stderr),
        ]
        for item in patches:
            item.__enter__()
            self.addCleanup(item.__exit__, None, None, None)

    def invoke(self, *arguments):
        return connect.main(["--state-dir", str(self.state), *arguments])

    def assert_no_secrets_printed(self):
        output = self.stdout.getvalue() + self.stderr.getvalue()
        for value in ENV.values():
            self.assertNotIn(value, output)
        self.assertNotIn("Traceback", output)

    def test_interactive_check_only_hides_credentials_and_writes_nothing(self):
        before = dict(os.environ)
        with patch("builtins.open", side_effect=AssertionError("must not write credential files")):
            self.assertEqual(self.invoke("--check-only"), 0)
        self.assertEqual(dict(os.environ), before)
        self.assertFalse(self.state.exists())
        self.assertEqual(self.entered.call_count, 3)
        self.answer.assert_not_called()
        self.runner.assert_not_called()
        for call in self.entered.call_args_list:
            self.assertIn("输入不会显示", call.args[0])
            self.assertIs(call.kwargs["stream"], self.stderr)
        self.assert_no_secrets_printed()

    def test_complete_environment_credentials_need_no_prompt_or_environment_change(self):
        os.environ.update(ENV)
        before = dict(os.environ)
        self.stdin.isatty.return_value = False
        self.assertEqual(self.invoke("--check-only"), 0)
        self.entered.assert_not_called()
        self.assertEqual(dict(os.environ), before)
        self.assertEqual(self.client.credentials, Credentials(*ENV.values()))
        self.runner.assert_not_called()
        self.assert_no_secrets_printed()

    def test_partial_environment_only_prompts_missing_values(self):
        os.environ["OKX_API_KEY"] = ENV["OKX_API_KEY"]
        self.entered.side_effect = [ENV["OKX_API_SECRET"], ENV["OKX_API_PASSPHRASE"]]
        before = dict(os.environ)
        self.assertEqual(self.invoke("--check-only"), 0)
        self.assertEqual(self.entered.call_count, 2)
        self.assertIn("Secret Key", self.entered.call_args_list[0].args[0])
        self.assertIn("Passphrase", self.entered.call_args_list[1].args[0])
        self.assertEqual(dict(os.environ), before)
        self.assertEqual(self.client.credentials, Credentials(*ENV.values()))
        self.assert_no_secrets_printed()

    def test_partial_credentials_non_tty_fail_before_prompt_or_api(self):
        os.environ["OKX_API_KEY"] = ENV["OKX_API_KEY"]
        self.stdin.isatty.return_value = False
        self.assertEqual(self.invoke("--check-only"), 1)
        self.entered.assert_not_called()
        self.factory.assert_not_called()
        self.runner.assert_not_called()
        self.assert_no_secrets_printed()

    def test_non_tty_complete_environment_cannot_start_even_if_input_mock_says_demo(self):
        os.environ.update(ENV)
        self.stdin.isatty.return_value = False
        self.answer.return_value = "DEMO"
        self.assertEqual(self.invoke(), 1)
        self.answer.assert_not_called()
        self.runner.assert_not_called()
        self.assert_no_secrets_printed()

    def test_getpass_warning_aborts_before_visible_fallback(self):
        def cannot_hide(*args, **kwargs):
            warnings.warn("cannot turn echo off", getpass.GetPassWarning)
            self.fail("getpass must not reach visible fallback")
        self.entered.side_effect = cannot_hide
        self.assertEqual(self.invoke(), 1)
        self.factory.assert_not_called()
        self.answer.assert_not_called()
        self.runner.assert_not_called()
        self.assertIn("无法关闭输入回显", self.stderr.getvalue())
        self.assert_no_secrets_printed()

    def test_empty_credential_never_reaches_api_or_runner(self):
        self.entered.side_effect = [""]
        self.assertEqual(self.invoke(), 1)
        self.factory.assert_not_called()
        self.runner.assert_not_called()
        self.assert_no_secrets_printed()

    def test_enter_and_nonexact_acknowledgements_never_start(self):
        os.environ.update(ENV)
        for acknowledgement in ("", "demo", "DEMO ", " DEMO", "yes"):
            with self.subTest(acknowledgement=acknowledgement):
                self.answer.return_value = acknowledgement
                with patch("okx_bot.connect.signal.signal") as signals:
                    self.assertEqual(self.invoke(), 0)
                signals.assert_not_called()
                self.runner.assert_not_called()
        self.assertFalse(self.state.exists())
        self.assert_no_secrets_printed()

    def test_exact_demo_starts_same_client_and_restores_signal_handlers(self):
        self.answer.return_value = "DEMO"
        old = {name: signal.getsignal(name) for name in (signal.SIGINT, signal.SIGTERM)}
        self.assertEqual(self.invoke(), 0)
        self.runner.assert_called_once()
        call = self.runner.call_args
        self.assertIs(call.kwargs["client"], self.client)
        self.assertEqual(call.args[1], self.state)
        self.assertIsInstance(call.args[2], threading.Event)
        self.assertEqual(old, {name: signal.getsignal(name) for name in old})
        self.assert_no_secrets_printed()

    def test_running_signal_requests_stop_instead_of_raising(self):
        self.answer.return_value = "DEMO"
        handlers = {}
        def register(name, handler):
            handlers[name] = handler
        def run(config, state_dir, stop, client):
            handlers[signal.SIGINT](signal.SIGINT, None)
            self.assertTrue(stop.is_set())
            return {"halted": False}
        self.runner.side_effect = run
        with patch("okx_bot.connect.signal.signal", side_effect=register):
            self.assertEqual(self.invoke(), 0)
        self.assert_no_secrets_printed()

    def test_failed_checks_never_request_ack_or_start(self):
        invalid_cases = [
            ("account", {"posMode": "long_short_mode", "acctLv": "2"}),
            ("account", {"posMode": "net_mode", "acctLv": "3"}),
            ("balance", {"currency": "USDT", "equity": "0", "available": "0"}),
            ("balance", {"currency": "USDT", "equity": "NaN", "available": "900"}),
            ("position_rows", [{"pos": "1"}]),
            ("order_rows", [{"ordId": "external"}]),
            ("algo_rows", [{"algoId": "external"}]),
            ("server_ms", 0),
        ]
        os.environ.update(ENV)
        for field, value in invalid_cases:
            with self.subTest(field=field, value=value):
                original = getattr(self.client, field)
                setattr(self.client, field, value)
                self.assertEqual(self.invoke(), 1)
                self.answer.assert_not_called()
                self.runner.assert_not_called()
                setattr(self.client, field, original)
        self.assert_no_secrets_printed()

    def test_network_errors_never_echo_exception_credential_text(self):
        self.client.account_config = Mock(side_effect=OkxError(ENV["OKX_API_SECRET"]))
        self.assertEqual(self.invoke(), 1)
        self.runner.assert_not_called()
        self.assert_no_secrets_printed()

    def test_known_numeric_api_code_is_useful_without_raw_error_data(self):
        self.client.account_config = Mock(side_effect=OkxError('OKX rejected request (code 50101)'))
        self.assertEqual(self.invoke(), 1)
        self.assertIn('50101', self.stderr.getvalue())
        self.assertIn('模拟交易', self.stderr.getvalue())
        self.runner.assert_not_called()
        self.assert_no_secrets_printed()

    def test_numeric_code_with_unexpected_extra_data_is_not_echoed(self):
        self.client.account_config = Mock(side_effect=OkxError('OKX rejected request (code 50101) ' + ENV['OKX_API_SECRET']))
        self.assertEqual(self.invoke(), 1)
        self.assertNotIn('50101', self.stderr.getvalue())
        self.assert_no_secrets_printed()

    def test_http_auth_failure_reaches_wizard_as_safe_code_and_hint(self):
        requests = []

        def open_response(request, timeout):
            requests.append(request)
            if request.full_url.endswith('/public/time'):
                return io.BytesIO(json.dumps({"code": "0", "data": [{"ts": str(time.time_ns() // 1_000_000)}]}).encode())
            body = json.dumps({"code": "50110",
                "msg": "Your IP 8.8.4.4 is not included in your API key's IP whitelist.",
                "ignored": ENV["OKX_API_SECRET"]}).encode()
            raise urllib.error.HTTPError(request.full_url, 401, ENV["OKX_API_PASSPHRASE"], {}, io.BytesIO(body))

        client = RealOkxClient(Credentials(*ENV.values()), opener=open_response)
        with patch('okx_bot.connect.OkxClient', return_value=client):
            self.assertEqual(self.invoke(), 1)
        self.assertIn('50110', self.stderr.getvalue())
        self.assertIn('HTTP 401', self.stderr.getvalue())
        self.assertIn('白名单', self.stderr.getvalue())
        self.assertIn('欧易在本次请求中报告的来源 IP：8.8.4.4', self.stderr.getvalue())
        self.assertIn('公共接口及时间检查通过', self.stdout.getvalue())
        self.assertEqual(len(requests), 2)
        public_headers = {name.lower() for name, _value in requests[0].header_items()}
        self.assertFalse(any(name.startswith('ok-access-') for name in public_headers))
        self.answer.assert_not_called()
        self.runner.assert_not_called()
        self.assert_no_secrets_printed()

    def test_source_ip_is_revalidated_and_normalized_before_display(self):
        error = OkxError(ENV['OKX_API_SECRET'])
        error.api_code = '50110'
        error.request_ip = '::ffff:8.8.4.4'
        connect._print_api_error(error)
        self.assertIn('来源 IP：8.8.4.4', self.stderr.getvalue())
        self.assertNotIn('::ffff', self.stderr.getvalue())
        self.assert_no_secrets_printed()

    def test_source_ip_is_not_displayed_for_invalid_values_or_other_codes(self):
        cases = [('50110', ENV['OKX_API_SECRET']), ('50110', '8.8.4.4 ' + ENV['OKX_API_KEY']),
                 ('50110', '2001:4860::1%eth0'), ('50110', '8.8.4.4/24'),
                 ('50110', '8.8.4.4:443'), ('50110', None), ('50101', '8.8.4.4')]
        for code, request_ip in cases:
            with self.subTest(code=code, request_ip=request_ip):
                error = OkxError(ENV['OKX_API_SECRET'])
                error.api_code = code
                error.request_ip = request_ip
                connect._print_api_error(error)
        self.assertNotIn('报告的来源 IP：', self.stderr.getvalue())
        self.assert_no_secrets_printed()

    def test_typed_credentials_errors_show_relevant_hint_without_remote_text(self):
        os.environ.update(ENV)
        for code, expected in (('50105', 'Passphrase'), ('50111', '同一组'), ('50113', '签名')):
            with self.subTest(code=code):
                error = OkxError(ENV['OKX_API_SECRET'])
                error.api_code = code
                error.http_status = 401
                self.client.account_config = Mock(side_effect=error)
                self.assertEqual(self.invoke(), 1)
                self.assertIn(code, self.stderr.getvalue())
                self.assertIn(expected, self.stderr.getvalue())
        self.runner.assert_not_called()
        self.assert_no_secrets_printed()

    def test_unvalidated_diagnostic_attributes_are_not_echoed(self):
        error = OkxError(ENV['OKX_API_SECRET'])
        error.api_code = '50110 ' + ENV['OKX_API_SECRET']
        error.http_status = ENV['OKX_API_KEY']
        error.category = ENV['OKX_API_PASSPHRASE']
        self.client.account_config = Mock(side_effect=error)
        self.assertEqual(self.invoke(), 1)
        self.assertNotIn('50110', self.stderr.getvalue())
        self.assert_no_secrets_printed()

    def test_public_timeout_is_reported_before_any_private_request(self):
        error = OkxError(ENV['OKX_API_SECRET'])
        error.category = 'timeout'
        self.client.server_time = Mock(side_effect=error)
        self.client.account_config = Mock()
        self.assertEqual(self.invoke(), 1)
        self.assertIn('连接超时', self.stderr.getvalue())
        self.client.account_config.assert_not_called()
        self.answer.assert_not_called()
        self.runner.assert_not_called()
        self.assert_no_secrets_printed()

    def test_clock_skew_is_refused_before_signed_private_request(self):
        self.client.server_ms = 0
        self.client.account_config = Mock()
        self.assertEqual(self.invoke(), 1)
        self.assertIn('时间相差超过', self.stderr.getvalue())
        self.client.account_config.assert_not_called()
        self.runner.assert_not_called()
        self.assert_no_secrets_printed()

    def test_unexpected_runner_exception_is_secret_free_and_restores_handlers(self):
        self.answer.return_value = "DEMO"
        self.runner.side_effect = RuntimeError(ENV["OKX_API_PASSPHRASE"])
        previous = signal.getsignal(signal.SIGINT)
        self.assertEqual(self.invoke(), 1)
        self.assertEqual(signal.getsignal(signal.SIGINT), previous)
        self.assert_no_secrets_printed()

    def test_runner_halt_reason_is_not_blindly_printed(self):
        self.answer.return_value = "DEMO"
        self.runner.return_value = {"halted": True, "halt_reason": ENV["OKX_API_SECRET"]}
        self.assertEqual(self.invoke(), 1)
        self.assertIn("status.json", self.stderr.getvalue())
        self.assert_no_secrets_printed()

    def test_eof_or_ctrl_c_at_secret_prompt_aborts_without_traceback(self):
        for error in (EOFError(), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__):
                self.entered.side_effect = error
                self.assertEqual(self.invoke(), 130)
                self.factory.assert_not_called()
                self.runner.assert_not_called()
        self.assert_no_secrets_printed()

    def test_eof_or_ctrl_c_at_ack_aborts_without_starting(self):
        os.environ.update(ENV)
        for error in (EOFError(), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__):
                self.answer.side_effect = error
                self.assertEqual(self.invoke(), 130)
                self.runner.assert_not_called()
        self.assert_no_secrets_printed()

    def test_readonly_check_uses_only_readonly_api_methods(self):
        summary = connect.check_demo(Config(), self.client)
        self.assertTrue(summary["flat"])
        self.assertEqual(self.client.calls, ["server_time", "account_config", "instrument", "balances",
                                             "positions", "pending_orders", "pending_algo_orders"])

    def test_invalid_config_is_sanitized_before_any_credential_prompt(self):
        with patch("okx_bot.connect.load_config", side_effect=ValueError(ENV["OKX_API_SECRET"])):
            self.assertEqual(self.invoke(), 1)
        self.entered.assert_not_called()
        self.factory.assert_not_called()
        self.assert_no_secrets_printed()


if __name__ == "__main__":
    unittest.main()
