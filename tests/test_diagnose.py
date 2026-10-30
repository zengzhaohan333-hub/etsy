import contextlib
import io
import json
import unittest
from unittest.mock import Mock, patch

from okx_bot import diagnose
from okx_bot.okx import OkxClient, OkxError


class SequenceClient:
    def __init__(self, outcomes):
        self.outcomes = iter(outcomes)
        self.calls = 0

    def server_time(self):
        self.calls += 1
        result = next(self.outcomes)
        if isinstance(result, BaseException):
            raise result
        return result

    def __getattr__(self, name):
        raise AssertionError("Diagnostic must not access other client methods: " + name)


class DiagnoseTests(unittest.TestCase):
    def run_checks(self, outcomes, *, attempts=3, clocks=None):
        output = io.StringIO()
        client = SequenceClient(outcomes)
        delay = Mock()
        values = iter(clocks or [1000, 1020] * attempts)
        result = diagnose.run_diagnostics(attempts=attempts, client=client,
            clock_ms=lambda: next(values), delay=delay, output=output)
        return result, output.getvalue(), client, delay

    def test_default_three_independent_successes_report_timing_without_hidden_requests(self):
        result, output, client, delay = self.run_checks([1010] * 3)
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual((result["completed"], result["successes"], result["failures"]), (3, 3, 0))
        self.assertEqual(client.calls, 3)
        self.assertEqual(delay.call_args_list, [((1,),), ((1,),)])
        self.assertTrue(all(check["round_trip_ms"] == 20 and check["clock_delta_ms"] == 0
                            for check in result["checks"]))
        self.assertIn("成功 3/3", output)
        self.assertIn("不代表账户授权", output)

    def test_failure_does_not_hide_next_check_and_any_failure_means_nonzero(self):
        failure = OkxError("never show original transport text", category="timeout")
        result, output, client, delay = self.run_checks([failure, 1010, 1010])
        self.assertEqual(result["exit_code"], 1)
        self.assertEqual((result["successes"], result["failures"]), (2, 1))
        self.assertEqual(client.calls, 3)
        self.assertEqual(delay.call_count, 2)
        self.assertIn("category=timeout", output)
        self.assertNotIn("never show original", output)

    def test_tls_diagnostics_show_only_safe_consistent_reason(self):
        failures = [OkxError("hidden", category="tls", tls_failure="certificate"),
                    OkxError("hidden", category="tls", tls_failure="handshake"),
                    OkxError("hidden", category="transport", tls_failure="unexpected_eof")]
        result, output, _, _ = self.run_checks(failures)
        self.assertEqual(result["failures"], 3)
        self.assertEqual([check["tls_failure"] for check in result["checks"]],
                         ["certificate", "handshake", "unexpected_eof"])
        for reason in ("certificate", "handshake", "unexpected_eof"):
            self.assertIn("tls_failure=" + reason, output)
        self.assertNotIn("hidden", output)

    def test_diagnostic_boundary_rejects_tampered_metadata_and_raw_error_content(self):
        secret = "http://fake-user:fake-secret@proxy.invalid:8080/private-body"
        error = OkxError(secret, category="transport")
        error.api_code = secret
        error.http_status = "503 " + secret
        error.category = secret
        error.tls_failure = secret
        mismatch = OkxError(secret, category="transport", tls_failure="certificate")
        result, output, _, _ = self.run_checks([error, mismatch, RuntimeError(secret)])
        self.assertNotIn(secret, output + json.dumps(result))
        self.assertEqual(result["checks"][0]["category"], "unknown")
        for name in ("api_code", "http_status", "tls_failure"):
            self.assertNotIn(name, result["checks"][0])
        self.assertNotIn("tls_failure", result["checks"][1])

    def test_http_and_api_diagnostics_retain_only_valid_numeric_fields(self):
        result, output, _, _ = self.run_checks([
            OkxError("private body", category="server", http_status=503, api_code="50013"),
            OkxError("private body", category="http", http_status=401, api_code="50101"),
            OkxError("private body", category="rate_limit", http_status=429)])
        self.assertEqual(result["checks"][0]["http_status"], 503)
        self.assertEqual(result["checks"][0]["api_code"], "50013")
        self.assertIn("http_status=401", output)
        self.assertIn("api_code=50101", output)
        self.assertNotIn("private body", output)

    def test_invalid_server_time_is_a_failure_and_clock_skew_is_explicit(self):
        result, output, client, _ = self.run_checks([True, "1010", 20_000])
        self.assertEqual(client.calls, 3)
        self.assertEqual(result["successes"], 1)
        self.assertEqual(result["failures"], 2)
        self.assertEqual(result["checks"][0]["category"], "invalid_response")
        self.assertTrue(result["checks"][2]["clock_warning"])
        self.assertIn("超过 5 秒", output)

    def test_bad_local_clock_does_not_skip_public_request_or_invent_timing(self):
        result, output, client, delay = self.run_checks([1000], attempts=1, clocks=[2000, 1000])
        self.assertEqual(client.calls, 1)
        self.assertEqual(result["exit_code"], 0)
        self.assertNotIn("clock_delta_ms", result["checks"][0])
        self.assertTrue(result["checks"][0]["clock_warning"])
        self.assertIn("本机计时异常", output)
        delay.assert_not_called()

    def test_default_client_has_no_credentials_and_diagnostic_never_loads_them(self):
        client = SequenceClient([1010])
        with patch("okx_bot.diagnose.OkxClient", return_value=client) as factory, \
             patch("okx_bot.okx.Credentials.from_env", side_effect=AssertionError("must not load credentials")), \
             patch("builtins.open", side_effect=AssertionError("must not read/write files")):
            result = diagnose.run_diagnostics(attempts=1, clock_ms=lambda: 1010,
                                              delay=Mock(), output=io.StringIO())
        factory.assert_called_once_with(credentials=None)
        self.assertEqual(result["exit_code"], 0)

    def test_real_public_request_keeps_bot_headers_https_and_unsigned_route(self):
        captured = []
        class Response(io.BytesIO):
            status = 200
        def transport(request, timeout):
            captured.append((request, timeout))
            return Response(b'{"code":"0","data":[{"ts":"1010"}]}')
        client = OkxClient(credentials=None, opener=transport)
        result = diagnose.run_diagnostics(attempts=1, client=client,
                                          clock_ms=lambda: 1010, delay=Mock(), output=io.StringIO())
        self.assertEqual(result["exit_code"], 0)
        request, timeout = captured[0]
        headers = {name.lower(): value for name, value in request.header_items()}
        self.assertEqual(request.full_url, "https://www.okx.com/api/v5/public/time")
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(headers["x-simulated-trading"], "1")
        self.assertEqual(headers["user-agent"], "okx-demo-bot/0.1")
        self.assertFalse(any(name.startswith("ok-access-") for name in headers))
        self.assertEqual(timeout, 10)

    def test_constructor_failure_is_safe_and_does_not_claim_requests_completed(self):
        secret = "http://fake-user:fake-password@proxy.invalid"
        output = io.StringIO()
        with patch("okx_bot.diagnose.OkxClient", side_effect=ValueError(secret)):
            result = diagnose.run_diagnostics(output=output, delay=Mock())
        self.assertEqual(result["exit_code"], 1)
        self.assertEqual(result["completed"], 0)
        self.assertTrue(result["initialization_failed"])
        self.assertNotIn(secret, output.getvalue())

    def test_attempts_validation_precedes_client_creation(self):
        for attempts in (0, 4, True, "3", 1.5):
            with self.subTest(attempts=attempts), patch("okx_bot.diagnose.OkxClient") as factory:
                with self.assertRaises(ValueError):
                    diagnose.run_diagnostics(attempts=attempts, output=io.StringIO())
                factory.assert_not_called()

    def test_cli_default_and_single_check_propagate_status_and_cancel_safely(self):
        with patch("okx_bot.diagnose.run_diagnostics", return_value={"exit_code": 1}) as run:
            self.assertEqual(diagnose.main([]), 1)
            run.assert_called_once_with(attempts=3)
        with patch("okx_bot.diagnose.run_diagnostics", return_value={"exit_code": 0}) as run:
            self.assertEqual(diagnose.main(["--attempts", "1"]), 0)
            run.assert_called_once_with(attempts=1)
        with patch("okx_bot.diagnose.run_diagnostics", side_effect=KeyboardInterrupt), \
             contextlib.redirect_stderr(io.StringIO()) as output:
            self.assertEqual(diagnose.main([]), 130)
            self.assertIn("诊断已取消", output.getvalue())
        with patch("okx_bot.diagnose.run_diagnostics", side_effect=RuntimeError("hidden-proxy-password")), \
             contextlib.redirect_stderr(io.StringIO()) as output:
            self.assertEqual(diagnose.main([]), 1)
            self.assertNotIn("hidden-proxy-password", output.getvalue())


if __name__ == "__main__":
    unittest.main()
