import copy
import hashlib
import io
import json
import tempfile
import unittest
import urllib.error
import urllib.parse
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from okx_bot.config import Config
from okx_bot import history


D = Decimal
NOW = 1_761_868_800_000
CONFIG = Config()
INTERVAL = 900_000


def instrument_row():
    return {"instId": "BTC-USDT-SWAP", "instType": "SWAP", "ctType": "linear", "ctValCcy": "BTC",
        "settleCcy": "USDT", "state": "live", "ctVal": "0.01", "lotSz": "0.01", "minSz": "0.01",
        "maxMktSz": "10000", "tickSz": "0.1"}


def trade_row(ts):
    return [str(ts), "100", "102", "98", "101", "1", "0.01", "1.01", "1"]


def mark_row(ts):
    return [str(ts), "100.1", "102.1", "98.1", "101.1", "1"]


class FakeHistoryClient:
    def __init__(self, config=CONFIG, days=1):
        self.metadata = [instrument_row()]
        self.end = NOW // INTERVAL * INTERVAL
        self.start = self.end - days * history.DAY_MS
        self.first = self.start - (config.slow_window + 1) * INTERVAL
        timestamps = list(range(self.first, self.end, INTERVAL))
        self.trade = [trade_row(ts) for ts in reversed(timestamps)]
        self.mark = [mark_row(ts) for ts in reversed(timestamps)]
        funding_first = self.start - self.start % history.FUNDING_MAX_GAP_MS - history.FUNDING_MAX_GAP_MS
        self.funding = [{"instId": config.instrument, "fundingTime": str(ts), "fundingRate": "0.02", "realizedRate": "0.0001"}
            for ts in reversed(range(funding_first, self.end + 1, history.FUNDING_MAX_GAP_MS))]
        self.calls = []
        self.overlap = False
        self.repeat = False

    def get(self, endpoint, params):
        self.calls.append((endpoint, params))
        if endpoint == history.ENDPOINTS["instrument"]:
            return self.metadata
        cursor = int(params["after"])
        source = self.funding if endpoint == history.ENDPOINTS["funding"] else self.trade if endpoint == history.ENDPOINTS["trade"] else self.mark
        stamp = (lambda row: int(row["fundingTime"])) if endpoint == history.ENDPOINTS["funding"] else (lambda row: int(row[0]))
        if self.repeat:
            return copy.deepcopy(source[:100])
        rows = [row for row in source if stamp(row) <= cursor if self.overlap or stamp(row) < cursor]
        return copy.deepcopy(rows[:100])


class Response(io.BytesIO):
    status = 200


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeHistoryClient()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "history.json"

    def fetch(self):
        with patch("okx_bot.history.PublicHistoryClient", return_value=self.client), patch("okx_bot.history._now_ms", return_value=NOW + 10_000):
            return history.fetch_dataset(CONFIG, days=1, now_ms=NOW)

    def write_payload(self, payload):
        payload["snapshot"].pop("sha256", None)
        payload["snapshot"]["sha256"] = hashlib.sha256(history._canonical(payload)).hexdigest()
        self.path.write_bytes(history._canonical(payload))

    def test_fetch_has_warmup_closed_aligned_trade_mark_and_realized_funding(self):
        data = self.fetch()
        self.assertEqual(data.start_ms, self.client.start)
        self.assertEqual(data.end_ms, self.client.end)
        self.assertEqual(data.warmup_bars, CONFIG.slow_window + 1)
        self.assertEqual(len(data.trade_candles), 96 + 21)
        self.assertEqual(data.trade_candles[0].ts, self.client.first)
        self.assertEqual(data.trade_candles[-1].ts, data.end_ms - INTERVAL)
        self.assertEqual([c.ts for c in data.trade_candles], [c.ts for c in data.mark_candles])
        self.assertNotEqual(data.trade_candles[0].close, data.mark_candles[0].close)
        self.assertTrue(all(rate == D("0.0001") for _, rate in data.funding_rates))
        self.assertTrue(all(data.start_ms <= ts < data.end_ms for ts, _ in data.funding_rates))
        self.assertLess(data.funding_before[0], data.start_ms)
        self.assertEqual(data.funding_at_end[0], data.end_ms)
        history_calls = [params for endpoint, params in self.client.calls if endpoint != history.ENDPOINTS["instrument"]]
        self.assertTrue(all(params["limit"] == "100" for params in history_calls))
        trade_calls = [params for endpoint, params in self.client.calls if endpoint == history.ENDPOINTS["trade"]]
        self.assertGreaterEqual(len(trade_calls), 2)
        self.assertLess(int(trade_calls[1]["after"]), int(trade_calls[0]["after"]))

    def test_round_trip_schema_decimal_strings_and_offline_load_without_network(self):
        data = self.fetch()
        history.save_dataset(data, self.path)
        payload = json.loads(self.path.read_bytes())
        self.assertEqual(payload["provenance"], "production_public")
        self.assertEqual(payload["source"]["host"], "https://www.okx.com")
        self.assertEqual(payload["source"]["endpoints"], history.ENDPOINTS)
        self.assertEqual(payload["snapshot"]["warmup_bars"], 21)
        self.assertEqual(payload["snapshot"]["bar"], "15m")
        self.assertIsInstance(payload["trade_candles"][0][1], str)
        self.assertIsInstance(payload["funding_rates"][0][1], str)
        with patch("okx_bot.history.PublicHistoryClient", side_effect=AssertionError("offline load cannot request")):
            self.assertEqual(history.load_dataset(self.path), data)

    def test_fetch_allows_identical_pagination_overlap(self):
        self.client.overlap = True
        self.assertEqual(len(self.fetch().trade_candles), 117)

    def test_missing_or_unconfirmed_trade_or_mark_bar_rejected(self):
        for field in ("trade", "mark"):
            for missing in (True, False):
                with self.subTest(field=field, missing=missing):
                    self.client = FakeHistoryClient()
                    rows = getattr(self.client, field)
                    if missing:
                        rows.pop(20)
                    else:
                        rows[20][-1] = "0"
                    with self.assertRaises(history.HistoryError):
                        self.fetch()

    def test_bad_confirm_layout_does_not_confuse_trade_and_mark(self):
        self.client.mark[0].extend(["0", "0", "1"])
        with self.assertRaises(history.HistoryError):
            self.fetch()

    def test_out_of_order_conflicting_duplicate_and_stuck_page_rejected(self):
        self.client.trade[0], self.client.trade[1] = self.client.trade[1], self.client.trade[0]
        with self.assertRaises(history.HistoryError):
            self.fetch()
        self.client = FakeHistoryClient()
        conflict = self.client.trade[5].copy()
        conflict[4] = "100.9"
        self.client.trade.insert(6, conflict)
        with self.assertRaises(history.HistoryError):
            self.fetch()
        self.client = FakeHistoryClient()
        self.client.repeat = True
        with self.assertRaises(history.HistoryError):
            self.fetch()

    def test_no_mark_endpoint_data_is_a_blocker_not_trade_fallback(self):
        self.client.mark = []
        with self.assertRaises(history.HistoryError):
            self.fetch()

    def test_metadata_inverse_wrong_instrument_and_nonfinite_prices_fail(self):
        for field, value in (("ctType", "inverse"), ("settleCcy", "USD"), ("ctValCcy", "USDT"),
                             ("instId", "ETH-USDT-SWAP"), ("ctVal", "NaN"), ("tickSz", "0")):
            with self.subTest(field=field):
                self.client = FakeHistoryClient()
                self.client.metadata[0][field] = value
                with self.assertRaises(history.HistoryError):
                    self.fetch()
        self.client = FakeHistoryClient()
        self.client.trade[0][1] = "Infinity"
        with self.assertRaises(history.HistoryError):
            self.fetch()

    def test_funding_realized_zero_preferred_and_empty_realized_falls_back(self):
        for row in self.client.funding:
            row["realizedRate"] = "0"
        data = self.fetch()
        self.assertTrue(all(rate == 0 for _, rate in data.funding_rates))
        self.client = FakeHistoryClient()
        for row in self.client.funding:
            row["realizedRate"] = ""
        self.assertTrue(all(rate == D("0.02") for _, rate in self.fetch().funding_rates))

    def test_missing_nonfinite_conflicting_or_gapped_funding_rejected(self):
        for case in ("empty", "gap", "nan", "conflict", "boundary"):
            with self.subTest(case=case):
                self.client = FakeHistoryClient()
                if case == "empty":
                    self.client.funding = []
                elif case == "gap":
                    self.client.funding.pop(1)
                elif case == "nan":
                    self.client.funding[0]["realizedRate"] = "NaN"
                elif case == "conflict":
                    duplicate = self.client.funding[1].copy()
                    duplicate["realizedRate"] = "0.001"
                    self.client.funding.insert(2, duplicate)
                else:
                    self.client.funding = [row for row in self.client.funding if int(row["fundingTime"]) >= self.client.start]
                with self.assertRaises(history.HistoryError):
                    self.fetch()

    def test_invalid_days_fail_before_any_client(self):
        for days in (0, 91, -1, True, 1.5, "30"):
            with self.subTest(days=days), patch("okx_bot.history.PublicHistoryClient") as client:
                with self.assertRaises(history.HistoryError):
                    history.fetch_dataset(CONFIG, days=days)
                client.assert_not_called()

    def test_cache_revalidates_content_even_if_hash_is_recomputed(self):
        history.save_dataset(self.fetch(), self.path)
        original = json.loads(self.path.read_bytes())
        changes = [
            lambda p: p["trade_candles"].pop(20),
            lambda p: p["mark_candles"].__setitem__(10, p["mark_candles"][9]),
            lambda p: p["trade_candles"][0].__setitem__(1, "NaN"),
            lambda p: p["trade_candles"][0].__setitem__(1, 100.0),
            lambda p: p["trade_candles"][0].__setitem__(0, str(p["trade_candles"][0][0])),
            lambda p: p["snapshot"].__setitem__("start_ms", str(p["snapshot"]["start_ms"])),
            lambda p: p["funding_rates"][0].__setitem__(0, str(p["funding_rates"][0][0])),
            lambda p: p["funding_rates"].clear(),
            lambda p: p["snapshot"].__setitem__("funding_before", None),
            lambda p: p["snapshot"].__setitem__("warmup_bars", 20),
            lambda p: p["snapshot"].__setitem__("bar", "1m"),
            lambda p: p["snapshot"].__setitem__("instId", "ETH-USDT-SWAP"),
            lambda p: p["instrument"].__setitem__("ctType", "inverse"),
            lambda p: p.__setitem__("provenance", "demo"),
            lambda p: p["source"].__setitem__("host", "https://other.example"),
        ]
        for mutate in changes:
            with self.subTest(mutate=changes.index(mutate)):
                payload = copy.deepcopy(original)
                mutate(payload)
                self.write_payload(payload)
                with self.assertRaises(history.HistoryError):
                    history.load_dataset(self.path)

    def test_corrupt_hash_duplicate_json_keys_and_missing_file_fail_safely(self):
        history.save_dataset(self.fetch(), self.path)
        content = self.path.read_bytes().replace(b'"101"', b'"100"', 1)
        self.path.write_bytes(content)
        with self.assertRaises(history.HistoryError):
            history.load_dataset(self.path)
        for content in (b'{"version":1,"version":1}', b'{"x":NaN}', b'not-json diagnostic-secret'):
            self.path.write_bytes(content)
            with self.assertRaises(history.HistoryError) as raised:
                history.load_dataset(self.path)
            self.assertNotIn("diagnostic-secret", str(raised.exception))
        with self.assertRaises(history.HistoryError):
            history.load_dataset(self.path.with_name("missing.json"))

    def test_invalid_save_preserves_existing_cache(self):
        data = self.fetch()
        history.save_dataset(data, self.path)
        before = self.path.read_bytes()
        with self.assertRaises(history.HistoryError):
            history.save_dataset(replace(data, mark_candles=[]), self.path)
        self.assertEqual(self.path.read_bytes(), before)

    def test_snapshot_size_limit_read_and_write(self):
        data = self.fetch()
        with patch("okx_bot.history.MAX_CACHE_BYTES", 100):
            with self.assertRaises(history.HistoryError):
                history.save_dataset(data, self.path)
            self.path.write_bytes(b"x" * 101)
            with self.assertRaises(history.HistoryError):
                history.load_dataset(self.path)


class PublicTransportTests(unittest.TestCase):
    def get(self, client):
        return client.get(history.ENDPOINTS["funding"], {"instId": "BTC-USDT-SWAP", "limit": "100", "after": str(NOW)})

    def test_production_get_no_auth_no_simulated_header_and_bounded_timeout(self):
        calls = []
        def opener(request, timeout):
            calls.append((request, timeout))
            return Response(b'{"code":"0","data":[]}')
        client = history.PublicHistoryClient(opener=opener, sleep=lambda _: None)
        self.assertEqual(self.get(client), [])
        request, timeout = calls[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(urllib.parse.urlsplit(request.full_url).netloc, "www.okx.com")
        names = {name.lower() for name, _ in request.header_items()}
        self.assertFalse(any(name.startswith("ok-access-") for name in names))
        self.assertNotIn("x-simulated-trading", names)
        self.assertLessEqual(timeout, 30)
        with self.assertRaises(history.HistoryError):
            client.get("/api/v5/trade/order", {})
        with patch("okx_bot.history.BASE_URL", "https://other.example"), self.assertRaises(history.HistoryError):
            self.get(client)

    def test_retry_only_limited_transient_http_errors_and_never_exposes_remote_message(self):
        calls, notices = [], []
        def opener(request, timeout):
            calls.append(request)
            if len(calls) <= 2:
                raise urllib.error.HTTPError(request.full_url, 429 if len(calls) == 1 else 503,
                    "diagnostic-secret", {}, io.BytesIO(b'{"msg":"diagnostic-secret"}'))
            return Response(b'{"code":"0","data":[]}')
        client = history.PublicHistoryClient(opener=opener, sleep=lambda _: None, progress=notices.append)
        self.assertEqual(self.get(client), [])
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(notices), 2)
        self.assertNotIn("diagnostic-secret", " ".join(notices))

    def test_persistent_server_error_stops_after_three_requests(self):
        calls = []
        def opener(request, timeout):
            calls.append(request)
            raise urllib.error.HTTPError(request.full_url, 503, "diagnostic-secret", {}, io.BytesIO(b"diagnostic-secret"))
        with self.assertRaises(history.HistoryError) as raised:
            self.get(history.PublicHistoryClient(opener=opener, sleep=lambda _: None))
        self.assertEqual(len(calls), 3)
        self.assertIn("503", str(raised.exception))
        self.assertNotIn("diagnostic-secret", str(raised.exception))

    def test_redirect_and_error_http_body_claiming_success_are_not_accepted(self):
        for status in (302, 401, 403):
            calls = []
            def opener(request, timeout):
                calls.append(request)
                raise urllib.error.HTTPError(request.full_url, status, "diagnostic-secret",
                    {"Location": "https://other.example/diagnostic-secret"}, io.BytesIO(b'{"code":"0","data":[]}'))
            with self.subTest(status=status), self.assertRaises(history.HistoryError) as raised:
                self.get(history.PublicHistoryClient(opener=opener, sleep=lambda _: None))
            self.assertEqual(len(calls), 1)
            self.assertNotIn("diagnostic-secret", str(raised.exception))
        self.assertIsNone(history._NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.example"))

    def test_transport_and_invalid_or_oversized_json_fail_without_sensitive_text(self):
        for kind in ("transport", "json", "oversize", "nan", "api", "too_many"):
            calls = []
            def opener(request, timeout):
                calls.append(request)
                if kind == "transport":
                    raise urllib.error.URLError("diagnostic-secret")
                if kind == "json":
                    return Response(b"<html>diagnostic-secret</html>")
                if kind == "oversize":
                    return Response(b"x" * (history.MAX_RESPONSE_BYTES + 1))
                if kind == "nan":
                    return Response(b'{"code":"0","data":[NaN],"msg":"diagnostic-secret"}')
                if kind == "too_many":
                    return Response(json.dumps({"code": "0", "data": list(range(101))}).encode())
                return Response(b'{"code":"50101","msg":"diagnostic-secret"}')
            with self.subTest(kind=kind), self.assertRaises(history.HistoryError) as raised:
                self.get(history.PublicHistoryClient(opener=opener, sleep=lambda _: None))
            self.assertEqual(len(calls), 1)
            self.assertNotIn("diagnostic-secret", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
