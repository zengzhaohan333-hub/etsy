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
        self.assertEqual(set(error.__dict__), {"api_code", "http_status", "category", "request_ip", "tls_failure"})

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

    def test_typed_tls_subtypes_are_safe_and_unwrapped_once(self):
        cases = [
            (ssl.SSLEOFError(8, "test-secret"), "transport", "unexpected_eof"),
            (urllib.error.URLError(ssl.SSLEOFError(8, "test-passphrase")), "transport", "unexpected_eof"),
            (ssl.SSLCertVerificationError(1, "test-secret"), "tls", "certificate"),
            (urllib.error.URLError(ssl.SSLCertVerificationError(1, "test-key")), "tls", "certificate"),
            (ssl.SSLError(1, "test-secret"), "tls", "handshake"),
            (urllib.error.URLError(ssl.SSLError(1, "EOF test-passphrase")), "tls", "handshake"),
            (urllib.error.URLError("SSLEOFError test-secret"), "transport", None),
            (urllib.error.URLError(urllib.error.URLError(ssl.SSLCertVerificationError(1, "test-key"))), "transport", None),
        ]
        for failure, category, tls_failure in cases:
            with self.subTest(kind=type(failure).__name__, category=category, tls_failure=tls_failure):
                transport = Transport(failure=failure)
                with self.assertRaises(OkxError) as raised:
                    self.client(transport).account_config()
                self.assertEqual(raised.exception.category, category)
                self.assertEqual(raised.exception.tls_failure, tls_failure)
                self.assertEqual(len(transport.requests), 1)
                self.assert_safe_diagnostic(raised.exception)

    def test_error_constructor_only_retains_known_tls_failure_enum(self):
        for value in ("certificate", "unexpected_eof", "handshake"):
            error = OkxError("Controlled message", tls_failure=value)
            self.assertEqual(error.tls_failure, value)
            self.assert_safe_diagnostic(error)
        for value in (None, "test-secret", "handshake test-passphrase", "certificate\n", True, 1, [], {}):
            with self.subTest(value=type(value).__name__):
                error = OkxError("Controlled message", tls_failure=value)
                self.assertIsNone(error.tls_failure)
                self.assert_safe_diagnostic(error)

    def test_tls_failure_boundary_helper_validates_value_category_pair_without_coercion(self):
        from okx_bot.okx import _safe_tls_failure
        for value, category in (("certificate", "tls"), ("handshake", "tls"), ("unexpected_eof", "transport")):
            self.assertEqual(_safe_tls_failure(value, category), value)
        for value, category in (("certificate", "transport"), ("handshake", "transport"),
                                ("unexpected_eof", "tls"), ("test-secret", "tls"),
                                (None, "tls"), ([], "tls"), ({}, "transport"),
                                ("certificate", []), ("handshake", {}),
                                ("unexpected_eof", None), (True, "transport")):
            with self.subTest(value_type=type(value).__name__, category_type=type(category).__name__):
                self.assertIsNone(_safe_tls_failure(value, category))

    def test_http_error_body_tls_failure_retains_type_without_losing_status(self):
        from okx_bot.read_retry import is_retryable_read_error
        class BrokenBody(io.BytesIO):
            def read(self, size=-1):
                raise self.failure
        for cause, category, tls_failure, retryable in (
            (ssl.SSLCertVerificationError(1, "test-secret"), "tls", "certificate", False),
            (ssl.SSLError(1, "test-passphrase"), "tls", "handshake", False),
            (ssl.SSLEOFError(8, "test-key"), "transport", "unexpected_eof", True),
        ):
            for order in (False, True):
                with self.subTest(tls_failure=tls_failure, order=order):
                    stream = BrokenBody()
                    stream.failure = cause
                    failure = urllib.error.HTTPError("https://www.okx.com", 503, "test-secret", {}, stream)
                    transport = Transport(failure=failure)
                    expected_error = AmbiguousOrderError if order else OkxError
                    with self.assertRaises(expected_error) as raised:
                        if order:
                            self.client(transport).place_market_order("BTC-USDT-SWAP", "buy", "1", "b1", stop_loss="1", take_profit="2")
                        else:
                            self.client(transport).account_config()
                    self.assertEqual(raised.exception.http_status, 503)
                    self.assertEqual(raised.exception.category, category)
                    self.assertEqual(raised.exception.tls_failure, tls_failure)
                    self.assertEqual(is_retryable_read_error(raised.exception), retryable and not order)
                    self.assertEqual(len(transport.requests), 1)
                    self.assert_safe_diagnostic(raised.exception)

    @staticmethod
    def retry_stop():
        class Stop:
            def __init__(self):
                self.delays = []
            def is_set(self):
                return False
            def wait(self, delay):
                self.delays.append(delay)
                return False
        return Stop()

    def test_read_wrapper_can_retry_typed_tls_eof_with_fresh_read_only_request(self):
        from okx_bot.read_retry import ReadRetryClient
        requests = []
        def opener(request, timeout):
            requests.append(request)
            if len(requests) == 1:
                raise urllib.error.URLError(ssl.SSLEOFError(8, "test-secret"))
            return io.BytesIO(b'{"code":"0","data":[{"acctLv":"2","posMode":"net_mode"}]}')
        stop = self.retry_stop()
        client = ReadRetryClient(OkxClient(self.credentials, opener=opener), stop)
        self.assertEqual(client.account_config()["acctLv"], "2")
        self.assertEqual(len(requests), 2)
        self.assertIsNot(requests[0], requests[1])
        self.assertTrue(all(request.get_method() == "GET" for request in requests))
        self.assertEqual(stop.delays, [2])

    def test_read_wrapper_keeps_certificate_and_unknown_handshake_nonretryable(self):
        from okx_bot.read_retry import ReadRetryClient
        for failure, tls_failure in ((ssl.SSLCertVerificationError(1, "test-secret"), "certificate"),
                                     (ssl.SSLError(1, "EOF occurred test-passphrase"), "handshake")):
            with self.subTest(tls_failure=tls_failure):
                transport = Transport(failure=urllib.error.URLError(failure))
                stop = self.retry_stop()
                with self.assertRaises(OkxError) as raised:
                    ReadRetryClient(self.client(transport), stop).account_config()
                self.assertEqual(raised.exception.category, "tls")
                self.assertEqual(raised.exception.tls_failure, tls_failure)
                self.assertEqual(len(transport.requests), 1)
                self.assertEqual(stop.delays, [])
                self.assert_safe_diagnostic(raised.exception)

    def test_order_eof_is_ambiguous_and_never_retried_by_either_layer(self):
        from okx_bot.read_retry import ReadRetryClient
        for failure, expected in ((ssl.SSLEOFError(8, "test-secret"), "unexpected_eof"),
                                  (ssl.SSLCertVerificationError(1, "test-key"), "certificate"),
                                  (ssl.SSLError(1, "test-passphrase"), "handshake")):
            with self.subTest(tls_failure=expected):
                transport = Transport(failure=urllib.error.URLError(failure))
                stop = self.retry_stop()
                with self.assertRaises(AmbiguousOrderError) as raised:
                    ReadRetryClient(self.client(transport), stop).place_market_order(
                        "BTC-USDT-SWAP", "buy", "1", "b1", stop_loss="1", take_profit="2")
                self.assertEqual(raised.exception.tls_failure, expected)
                self.assertEqual(len(transport.requests), 1)
                self.assertEqual(stop.delays, [])
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

    def test_whitelist_request_ip_uses_complete_known_50110_messages_in_both_paths(self):
        cases = [
            ("Your IP 65.75.221.17 is not included in your API key's IP whitelist.", "65.75.221.17"),
            ("Your IP address 65.75.221.17 is not included in your API key's IP whitelist", "65.75.221.17"),
            ("Your IP 2001:0DB8:0000:0000:0000:0000:0000:0001 is not included in your API key's IP whitelist.", "2001:db8::1"),
            ("Your IP ::FFFF:65.75.221.17 is not included in your API key's IP whitelist.", "65.75.221.17"),
        ]
        for message, expected in cases:
            for http_error in (False, True):
                with self.subTest(message=message, http_error=http_error):
                    body = json.dumps({"code": "50110", "msg": message,
                                       "debug": "test-key test-secret test-passphrase"}).encode()
                    transport = self.http_transport(401, body) if http_error else Transport(raw=body)
                    with self.assertRaises(OkxError) as raised:
                        self.client(transport).account_config()
                    self.assertEqual(raised.exception.api_code, "50110")
                    self.assertEqual(raised.exception.request_ip, expected)
                    self.assertEqual(str(raised.exception), "OKX rejected request (code 50110)")
                    self.assertNotIn(message, str(raised.exception))
                    self.assert_safe_diagnostic(raised.exception)

    def test_whitelist_request_ip_is_not_extracted_from_arbitrary_context(self):
        template = "Your IP 65.75.221.17 is not included in your API key's IP whitelist."
        messages = [
            "test-secret 65.75.221.17 test-key", "IP: 65.75.221.17", "test-passphrase " + template,
            template + " test-secret", template + "\n", "\n" + template, template + "\r\n",
            template + " " + template, template.lower(), template.replace("Your IP", "Your ip"),
            template.replace("65.75.221.17", "test-secret"),
            template.replace("65.75.221.17", "65.75.221.17,192.0.2.1"),
            template.replace("65.75.221.17", "65.75.221.17 192.0.2.1"),
            template.replace("65.75.221.17", "256.75.221.17"),
            template.replace("65.75.221.17", "065.75.221.17"),
            template.replace("65.75.221.17", "65.75.221.17:443"),
            template.replace("65.75.221.17", "65.75.221.17/32"),
            template.replace("65.75.221.17", "fe80::1%eth0"),
            template.replace("65.75.221.17", "[2001:db8::1]"),
            template.replace("65.75.221.17", "2001:db8::1/128"),
            template.replace("65.75.221.17", "1"),
            template.replace("65.75.221.17", "２００１:db8::1"),
            "x" * 1000 + template, None, 50110, {"ip": "65.75.221.17"},
        ]
        for message in messages:
            for http_error in (False, True):
                with self.subTest(message=message, http_error=http_error):
                    body = json.dumps({"code": "50110", "msg": message}).encode()
                    transport = self.http_transport(401, body) if http_error else Transport(raw=body)
                    with self.assertRaises(OkxError) as raised:
                        self.client(transport).account_config()
                    self.assertEqual(raised.exception.api_code, "50110")
                    self.assertIsNone(raised.exception.request_ip)
                    self.assert_safe_diagnostic(raised.exception)

    def test_whitelist_request_ip_requires_exact_code_50110(self):
        message = "Your IP 65.75.221.17 is not included in your API key's IP whitelist."
        for code in ("50101", "50111", "050110", "50110 test-secret", None):
            for http_error in (False, True):
                with self.subTest(code=code, http_error=http_error):
                    body = json.dumps({"code": code, "msg": message}).encode()
                    transport = self.http_transport(401, body) if http_error else Transport(raw=body)
                    with self.assertRaises(OkxError) as raised:
                        self.client(transport).account_config()
                    self.assertIsNone(raised.exception.request_ip)
                    self.assert_safe_diagnostic(raised.exception)

    def test_whitelist_request_ip_is_not_parsed_from_oversized_http_error_body(self):
        body = json.dumps({"code": "50110", "msg": "Your IP 65.75.221.17 is not included in your API key's IP whitelist.",
                           "padding": "x" * 100_000}).encode()
        with self.assertRaises(OkxError) as raised:
            self.client(self.http_transport(401, body)).account_config()
        self.assertIsNone(raised.exception.api_code)
        self.assertIsNone(raised.exception.request_ip)
        self.assert_safe_diagnostic(raised.exception)

    def test_error_constructor_revalidates_request_ip_and_code(self):
        for value in ("65.75.221.17 test-secret", "test-key", "fe80::1%eth0", "65.75.221.17/32",
                      "65.75.221.17\n", "65.75.221.17,192.0.2.1", 657522117, None):
            with self.subTest(value=value):
                error = OkxError("Controlled message", api_code="50110", request_ip=value)
                self.assertIsNone(error.request_ip)
                self.assert_safe_diagnostic(error)
        error = OkxError("Controlled message", api_code="50111", request_ip="65.75.221.17")
        self.assertIsNone(error.request_ip)
        error = OkxError("Controlled message", api_code="50110", request_ip="::ffff:65.75.221.17")
        self.assertEqual(error.request_ip, "65.75.221.17")
        self.assert_safe_diagnostic(error)

    def test_whitelist_ip_on_order_http_failure_stays_ambiguous_without_retry(self):
        body = json.dumps({"code": "50110", "msg": "Your IP 65.75.221.17 is not included in your API key's IP whitelist."}).encode()
        transport = self.http_transport(401, body)
        with self.assertRaises(AmbiguousOrderError) as raised:
            self.client(transport).place_market_order("BTC-USDT-SWAP", "buy", "1", "b1", stop_loss="1", take_profit="2")
        self.assertEqual(raised.exception.request_ip, "65.75.221.17")
        self.assertEqual(len(transport.requests), 1)
        self.assert_safe_diagnostic(raised.exception)


if __name__ == "__main__":
    unittest.main()
