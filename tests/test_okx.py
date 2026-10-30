import base64
import hashlib
import hmac
import http.client
import io
import json
import socket
import ssl
import unittest
import urllib.error
from decimal import Decimal

from okx_bot.okx import AmbiguousOrderError, Credentials, OkxClient, OkxError


class Transport:
    def __init__(self, data=None, *, code="0", failure=None, raw=None):
        self.requests = []
        self.data = [] if data is None else data
        self.code = code
        self.failure = failure
        self.raw = raw

    def __call__(self, request, timeout):
        self.requests.append((request, timeout))
        if self.failure:
            raise self.failure
        content = self.raw if self.raw is not None else json.dumps({"code": self.code, "data": self.data}).encode()
        return io.BytesIO(content)


class OkxTests(unittest.TestCase):
    def setUp(self):
        self.credentials = Credentials("test-key", "test-secret", "test-passphrase")

    def client(self, transport):
        return OkxClient(self.credentials, opener=transport)

    def test_signature_matches_documented_hmac_prehash(self):
        timestamp = "2020-12-08T09:08:57.715Z"
        path = "/api/v5/account/balance?ccy=USDT"
        expected = base64.b64encode(hmac.new(b"test-secret", (timestamp + "GET" + path).encode(), hashlib.sha256).digest()).decode()
        self.assertEqual(OkxClient.sign(timestamp, "GET", path, "", "test-secret"), expected)
        body = '{"instId":"BTC-USDT-SWAP"}'
        self.assertNotEqual(OkxClient.sign(timestamp, "POST", path, body, "test-secret"), expected)

    def test_all_private_calls_are_demo_and_signed_exact_query(self):
        transport = Transport(data=[])
        self.client(transport).positions("BTC-USDT-SWAP")
        request, timeout = transport.requests[0]
        headers = dict((k.lower(), v) for k, v in request.header_items())
        self.assertEqual(headers["x-simulated-trading"], "1")
        self.assertEqual(headers["ok-access-key"], "test-key")
        self.assertEqual(headers["ok-access-passphrase"], "test-passphrase")
        path = "/api/v5/account/positions?instId=BTC-USDT-SWAP"
        self.assertEqual(headers["ok-access-sign"], OkxClient.sign(headers["ok-access-timestamp"], "GET", path, "", "test-secret"))
        self.assertEqual(request.full_url, "https://www.okx.com" + path)
        self.assertEqual(timeout, 10)

    def test_public_request_never_sends_credentials(self):
        transport = Transport(data=[{"ts": "1000"}])
        self.assertEqual(self.client(transport).server_time(), 1000)
        names = [name.lower() for name, _ in transport.requests[0][0].header_items()]
        self.assertFalse(any(name.startswith("ok-access-") for name in names))
        self.assertEqual(dict((k.lower(), v) for k, v in transport.requests[0][0].header_items())["x-simulated-trading"], "1")

    def test_mark_price_has_decimal_and_exchange_timestamp(self):
        transport = Transport(data=[{"instId": "BTC-USDT-SWAP", "markPx": "100000.1", "ts": "12345"}])
        self.assertEqual(self.client(transport).mark_price("BTC-USDT-SWAP"), (Decimal("100000.1"), 12345))
        self.assertIn("/api/v5/public/mark-price?instType=SWAP", transport.requests[0][0].full_url)

    def test_missing_credentials_fail_before_network(self):
        transport = Transport()
        with self.assertRaises(OkxError):
            OkxClient(opener=transport).positions("BTC-USDT-SWAP")
        self.assertEqual(transport.requests, [])
        with self.assertRaises(OkxError):
            Credentials.from_env({})
        self.assertNotIn("test-secret", repr(self.credentials))

    def test_candles_discard_unconfirmed_sort_oldest_and_use_decimal(self):
        transport = Transport(data=[
            ["3000", "10.2", "12.1", "9.9", "11.1", "0", "0", "0", "0"],
            ["2000", "10.2", "12.1", "9.9", "11.1", "0", "0", "0", "1"],
            ["1000", "10.1", "12.1", "9.9", "10.2", "0", "0", "0", "1"],
        ])
        candles = self.client(transport).candles("BTC-USDT-SWAP")
        self.assertEqual([c.ts for c in candles], [1000, 2000])
        self.assertEqual(candles[0].close, Decimal("10.2"))

    def test_invalid_candle_and_conflicting_duplicates_rejected(self):
        invalid_rows = [
            [["1000", "10", "9", "8", "10", "0", "0", "0", "1"]],
            [["1000", "NaN", "12", "8", "10", "0", "0", "0", "1"]],
            [["1000", "10", "12", "8", "10", "0", "0", "0", "1"],
             ["1000", "10", "12", "8", "11", "0", "0", "0", "1"]],
        ]
        for rows in invalid_rows:
            with self.subTest(rows=rows), self.assertRaises(OkxError):
                self.client(Transport(rows)).candles("BTC-USDT-SWAP")

    def test_contract_size_is_contract_units_not_coin_amount(self):
        transport = Transport(data=[{
            "instId": "BTC-USDT-SWAP", "instType": "SWAP", "ctType": "linear",
            "settleCcy": "USDT", "ctValCcy": "BTC", "state": "live",
            "ctVal": "0.01", "lotSz": "0.01", "minSz": "0.01", "maxMktSz": "10000", "tickSz": "0.1",
        }])
        instrument = self.client(transport).instrument("BTC-USDT-SWAP")
        self.assertEqual(instrument.ct_val, Decimal("0.01"))
        self.assertEqual(instrument.lot_size, Decimal("0.01"))
        self.assertEqual(instrument.tick_size, Decimal("0.1"))
        transport.data[0]["ctType"] = "inverse"
        with self.assertRaises(OkxError):
            self.client(transport).instrument("BTC-USDT-SWAP")

    def test_entry_requires_exchange_native_stop_and_take_profit(self):
        transport = Transport(data=[{"ordId": "12345", "clOrdId": "bot1", "sCode": "0"}])
        client = self.client(transport)
        client.place_market_order("BTC-USDT-SWAP", "buy", Decimal("0.1"), "bot1", stop_loss=Decimal("95000"), take_profit=Decimal("105000"))
        request = transport.requests[0][0]
        payload = json.loads(request.data)
        self.assertEqual(payload["sz"], "0.1")
        self.assertEqual(payload["tdMode"], "isolated")
        self.assertEqual(payload["posSide"], "net")
        self.assertFalse(payload["reduceOnly"])
        bracket = payload["attachAlgoOrds"][0]
        self.assertEqual(bracket["slTriggerPx"], "95000")
        self.assertEqual(bracket["slOrdPx"], "-1")
        self.assertEqual(bracket["tpOrdPx"], "-1")
        self.assertEqual(bracket["slTriggerPxType"], "mark")
        self.assertEqual(bracket["tpTriggerPxType"], "mark")
        headers = {k.lower(): v for k, v in request.header_items()}
        self.assertEqual(headers["ok-access-sign"], OkxClient.sign(headers["ok-access-timestamp"], "POST", "/api/v5/trade/order", request.data.decode(), "test-secret"))
        with self.assertRaises(OkxError):
            client.place_market_order("BTC-USDT-SWAP", "buy", "1", "bot2")

    def test_reduce_only_exit_cannot_create_bracket(self):
        transport = Transport(data=[{"ordId": "12345", "clOrdId": "exit1", "sCode": "0"}])
        client = self.client(transport)
        client.place_market_order("BTC-USDT-SWAP", "sell", "0.1", "exit1", reduce_only=True)
        payload = json.loads(transport.requests[0][0].data)
        self.assertTrue(payload["reduceOnly"])
        self.assertNotIn("attachAlgoOrds", payload)
        with self.assertRaises(OkxError):
            client.place_market_order("BTC-USDT-SWAP", "sell", "1", "exit2", reduce_only=True, stop_loss="1")

    def test_rejected_per_order_code_not_reported_as_success(self):
        transport = Transport(data=[{"sCode": "51000", "sMsg": "test-secret"}])
        with self.assertRaisesRegex(OkxError, "51000") as error:
            self.client(transport).place_market_order("BTC-USDT-SWAP", "buy", "1", "b1", stop_loss="1", take_profit="2")
        self.assertNotIn("test-secret", str(error.exception))

    def test_ambiguous_order_timeout_never_retried(self):
        transport = Transport(failure=TimeoutError("request contained test-secret"))
        with self.assertRaises(AmbiguousOrderError) as error:
            self.client(transport).place_market_order("BTC-USDT-SWAP", "buy", "1", "b1", stop_loss="1", take_profit="2")
        self.assertEqual(len(transport.requests), 1)
        self.assertNotIn("test-secret", str(error.exception))

    def test_ambiguous_malformed_response_never_retried(self):
        transport = Transport(raw=b"not-json")
        with self.assertRaises(AmbiguousOrderError):
            self.client(transport).place_market_order("BTC-USDT-SWAP", "buy", "1", "b1", stop_loss="1", take_profit="2")
        self.assertEqual(len(transport.requests), 1)

    def test_okx_endpoint_timeout_and_missing_order_status_are_ambiguous(self):
        for transport in (Transport(data=[], code="50004"),
                          Transport(data=[{"sCode": "50004"}]),
                          Transport(data=[{"ordId": "123", "clOrdId": "b1"}])):
            with self.subTest(transport=transport), self.assertRaises(AmbiguousOrderError):
                self.client(transport).place_market_order("BTC-USDT-SWAP", "buy", "1", "b1", stop_loss="1", take_profit="2")
            self.assertEqual(len(transport.requests), 1)

    def test_response_error_is_sanitized(self):
        transport = Transport(data=[], code="test-secret")
        with self.assertRaises(OkxError) as error:
            self.client(transport).account_config()
        self.assertNotIn("test-secret", str(error.exception))

    def test_order_acknowledgement_is_not_a_fill_or_identity_assumption(self):
        transport = Transport(data=[{"sCode": "0", "ordId": "123", "clOrdId": "wrong"}])
        with self.assertRaises(AmbiguousOrderError):
            self.client(transport).place_market_order("BTC-USDT-SWAP", "buy", "1", "b1", stop_loss="1", take_profit="2")

    def test_fixed_host_and_linear_instrument_validation(self):
        transport = Transport(data=[])
        client = self.client(transport)
        client.BASE_URL = "https://other.example"
        with self.assertRaises(OkxError):
            client.positions("BTC-USDT-SWAP")
        self.assertEqual(transport.requests, [])
        for inst_id in ("BTC-USD-SWAP", "BTC-USDT", "//other.example", "BTC-USDT-SWAP?evil=1"):
            with self.subTest(inst_id=inst_id), self.assertRaises(OkxError):
                self.client(transport).positions(inst_id)

    def test_leverage_is_one_x_only(self):
        transport = Transport(data=[{"lever": "1", "mgnMode": "isolated"}])
        client = self.client(transport)
        with self.assertRaises(OkxError):
            client.set_leverage("BTC-USDT-SWAP", "20")
        client.set_leverage("BTC-USDT-SWAP")
        self.assertEqual(json.loads(transport.requests[0][0].data), {"instId": "BTC-USDT-SWAP", "lever": "1", "mgnMode": "isolated"})

    def test_balance_uses_available_usdt_equity(self):
        transport = Transport(data=[{"totalEq": "999999", "details": [{"ccy": "USDT", "eq": "1000", "availEq": "700", "availBal": "600"}]}])
        balances = self.client(transport).balances()
        self.assertEqual(balances["equity"], Decimal("1000"))
        self.assertEqual(balances["available"], Decimal("700"))

    def test_default_transport_refuses_redirects(self):
        from okx_bot.okx import _NoRedirect
        self.assertIsNone(_NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://other.example"))

    @staticmethod
    def http_transport(status, body, *, headers=None):
        failure = urllib.error.HTTPError("https://www.okx.com/api/v5/account/config?test-secret",
            status, "test-passphrase", headers or {"X-Debug": "test-key"}, io.BytesIO(body))
        return Transport(failure=failure)

    def assert_safe_diagnostic(self, error):
        # Operator-visible diagnostics and attached metadata must not retain the
        # untrusted HTTP message, URL, headers or credential-bearing JSON body.
        displayed = str(error) + repr(error) + repr(error.__dict__)
        for sensitive in ("test-key", "test-secret", "test-passphrase"):
            self.assertNotIn(sensitive, displayed)
        self.assertEqual(set(error.__dict__), {"api_code", "http_status", "category"})

    def test_http_authentication_error_preserves_only_numeric_code_and_status(self):
        for status in (400, 401):
            with self.subTest(status=status):
                body = json.dumps({"code": "50101", "msg": "test-key test-secret test-passphrase"}).encode()
                transport = self.http_transport(status, body)
                with self.assertRaises(OkxError) as raised:
                    self.client(transport).account_config()
                error = raised.exception
                self.assertEqual(error.api_code, "50101")
                self.assertEqual(error.http_status, status)
                self.assertEqual(error.category, "http")
                self.assertEqual(str(error), "OKX rejected request (code 50101)")
                self.assertEqual(len(transport.requests), 1)
                self.assertEqual(dict((name.lower(), value) for name, value in transport.requests[0][0].header_items())["x-simulated-trading"], "1")
                self.assert_safe_diagnostic(error)

    def test_http_html_forbidden_does_not_echo_body_or_reason(self):
        transport = self.http_transport(403, b"<html>test-secret test-passphrase test-key</html>")
        with self.assertRaises(OkxError) as raised:
            self.client(transport).account_config()
        self.assertEqual(raised.exception.http_status, 403)
        self.assertIsNone(raised.exception.api_code)
        self.assertEqual(raised.exception.category, "http")
        self.assert_safe_diagnostic(raised.exception)

    def test_http_rate_limit_server_proxy_and_redirect_have_static_categories(self):
        for status, category in ((429, "rate_limit"), (500, "server"), (502, "server"),
                                 (503, "server"), (407, "proxy"), (301, "redirect"), (302, "redirect")):
            with self.subTest(status=status):
                transport = self.http_transport(status, b'{"code":"50011","msg":"test-secret"}',
                    headers={"Location": "https://test-passphrase:pw@other.example/test-key"})
                with self.assertRaises(OkxError) as raised:
                    self.client(transport).account_config()
                self.assertEqual(raised.exception.http_status, status)
                self.assertEqual(raised.exception.category, category)
                self.assertEqual(raised.exception.api_code, "50011")
                self.assertEqual(len(transport.requests), 1)
                self.assert_safe_diagnostic(raised.exception)

    def test_http_response_that_claims_success_is_never_accepted(self):
        body = b'{"code":"0","data":[{"acctLv":"2","posMode":"net_mode"}],"msg":"test-secret"}'
        for status in (302, 401, 429, 500):
            with self.subTest(status=status):
                transport = self.http_transport(status, body)
                with self.assertRaises(OkxError) as raised:
                    self.client(transport).account_config()
                self.assertIsNone(raised.exception.api_code)
                self.assertEqual(raised.exception.http_status, status)
                self.assert_safe_diagnostic(raised.exception)

    def test_http_error_invalid_codes_and_json_only_keep_http_status(self):
        bodies = [b"not JSON test-secret", b'{"code":"50101 test-secret","msg":"test-key"}',
                  b'{"code":true}', b'{"code":null}', b'{"code":50101.0}',
                  b'{"code":"1234567890123"}', b'{"code":"-50101"}',
                  '{"code":"５０１０１"}'.encode(), b'["50101"]', b'{}', b'\xfftest-secret']
        for body in bodies:
            with self.subTest(body=body):
                transport = self.http_transport(401, body)
                with self.assertRaises(OkxError) as raised:
                    self.client(transport).account_config()
                self.assertIsNone(raised.exception.api_code)
                self.assertEqual(raised.exception.http_status, 401)
                self.assert_safe_diagnostic(raised.exception)

    def test_http_error_body_read_is_bounded_and_closed(self):
        class ObservedBody(io.BytesIO):
            def read(self, size=-1):
                self.requested_size = size
                return super().read(size)
        stream = ObservedBody(b'{"code":"50101","msg":"' + b"x" * 100_000 + b'"}')
        error = urllib.error.HTTPError("https://www.okx.com", 401, "test-secret", {}, stream)
        transport = Transport(failure=error)
        with self.assertRaises(OkxError) as raised:
            self.client(transport).account_config()
        self.assertEqual(stream.requested_size, 64 * 1024 + 1)
        self.assertTrue(stream.closed)
        self.assertIsNone(raised.exception.api_code)
        self.assertEqual(raised.exception.http_status, 401)
        self.assert_safe_diagnostic(raised.exception)

    def test_http_error_body_read_failure_still_keeps_status(self):
        class BrokenBody(io.BytesIO):
            def read(self, size=-1):
                raise self.failure
        for failure in (TimeoutError("test-secret"), http.client.IncompleteRead(b"test-secret")):
            with self.subTest(failure=type(failure).__name__):
                stream = BrokenBody()
                stream.failure = failure
                error = urllib.error.HTTPError("https://www.okx.com", 401, "test-passphrase", {}, stream)
                with self.assertRaises(OkxError) as raised:
                    self.client(Transport(failure=error)).account_config()
                self.assertEqual(raised.exception.http_status, 401)
                self.assertIsNone(raised.exception.api_code)
                self.assert_safe_diagnostic(raised.exception)

    def test_order_http_errors_always_remain_ambiguous_and_never_retry(self):
        cases = [(401, b'{"code":"50101","msg":"test-secret"}'),
                 (429, b'{"code":"50011","msg":"test-secret"}'),
                 (500, b'{"code":"50004","msg":"test-secret"}'),
                 (503, b'<html>test-secret</html>'),
                 (302, b'{"code":"0","data":[{"sCode":"0","ordId":"123","clOrdId":"b1"}]}')]
        for status, body in cases:
            with self.subTest(status=status):
                transport = self.http_transport(status, body)
                with self.assertRaises(AmbiguousOrderError) as raised:
                    self.client(transport).place_market_order("BTC-USDT-SWAP", "buy", "1", "b1", stop_loss="1", take_profit="2")
                self.assertEqual(raised.exception.http_status, status)
                self.assertEqual(len(transport.requests), 1)
                self.assert_safe_diagnostic(raised.exception)

    def test_success_status_business_error_without_data_retains_safe_code(self):
        transport = Transport(raw=b'{"code":"50101","msg":"test-key test-secret test-passphrase"}')
        with self.assertRaises(OkxError) as raised:
            self.client(transport).account_config()
        self.assertEqual(raised.exception.api_code, "50101")
        self.assertEqual(raised.exception.category, "api")
        self.assertEqual(str(raised.exception), "OKX rejected request (code 50101)")
        self.assert_safe_diagnostic(raised.exception)

    def test_invalid_and_oversized_success_response_remain_failures(self):
        bodies = [b'test-secret', b'{"code":"0","data":null,"msg":"test-secret"}', b'[]', b"x" * (4 * 1024 * 1024 + 1)]
        for body in bodies:
            with self.subTest(size=len(body)):
                transport = Transport(raw=body)
                with self.assertRaises(OkxError) as raised:
                    self.client(transport).account_config()
                self.assertEqual(raised.exception.category, "invalid_response")
                self.assert_safe_diagnostic(raised.exception)

    def test_nonexception_http_error_response_cannot_bypass_status_check(self):
        class ErrorResponse(io.BytesIO):
            status = 401
        calls = []
        def opener(request, timeout):
            calls.append(request)
            return ErrorResponse(b'{"code":"0","data":[{"acctLv":"2"}]}')
        with self.assertRaises(OkxError) as raised:
            OkxClient(self.credentials, opener=opener).account_config()
        self.assertEqual(raised.exception.http_status, 401)
        self.assertEqual(len(calls), 1)

    def test_typed_transport_failures_have_static_safe_categories(self):
        failures = [(TimeoutError("test-secret"), "timeout"),
                    (urllib.error.URLError(TimeoutError("test-passphrase")), "timeout"),
                    (urllib.error.URLError(ssl.SSLCertVerificationError("test-secret")), "tls"),
                    (urllib.error.URLError(socket.gaierror(-2, "test-key")), "dns"),
                    (urllib.error.URLError("test-secret test-key"), "transport")]
        for failure, category in failures:
            with self.subTest(category=category):
                transport = Transport(failure=failure)
                with self.assertRaises(OkxError) as raised:
                    self.client(transport).account_config()
                self.assertEqual(raised.exception.category, category)
                self.assertIsNone(raised.exception.api_code)
                self.assertIsNone(raised.exception.http_status)
                self.assert_safe_diagnostic(raised.exception)

    def test_credential_control_and_non_ascii_characters_are_rejected_safely(self):
        original = ["test-key", "test-secret", "test-passphrase"]
        for index in range(3):
            for suffix in ("\r", "\n", "\t", "\x00", "\x1f", "\x7f", "中文", "é"):
                with self.subTest(index=index, suffix=repr(suffix)):
                    values = original.copy()
                    values[index] += suffix
                    with self.assertRaises(OkxError) as raised:
                        Credentials(*values)
                    self.assertEqual(raised.exception.category, "credential_format")
                    self.assert_safe_diagnostic(raised.exception)

    def test_credential_values_preserve_legal_ascii_spaces_and_special_characters(self):
        values = (" test-key ", " test-secret!@#$%^&*()_+-=[]{};:',./? ", " test-passphrase!@# ")
        credentials = Credentials(*values)
        self.assertEqual((credentials.api_key, credentials.api_secret, credentials.passphrase), values)

    def test_safe_error_attributes_validate_input(self):
        error = OkxError("Local message", api_code="test-secret", http_status="401", category="test-key")
        self.assertIsNone(error.api_code)
        self.assertIsNone(error.http_status)
        self.assertEqual(error.category, "local")
        self.assert_safe_diagnostic(error)


if __name__ == "__main__":
    unittest.main()
