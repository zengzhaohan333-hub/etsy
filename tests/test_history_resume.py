"""Resume public downloads across process restarts; fixtures are not market data."""
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from okx_bot import history
from okx_bot.history_cache import HistoryPageCache
from test_history import CONFIG, NOW, FakeHistoryClient


class HistoryResumeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name) / "downloads"
        self.config = replace(CONFIG, bar="5m")

    def client(self):
        return FakeHistoryClient(self.config, warmup_bars=720)

    def fetch(self, client, notices=None):
        with patch("okx_bot.history.PublicHistoryClient", return_value=client), \
             patch("okx_bot.history._now_ms", return_value=NOW + 10_000):
            return history.fetch_dataset(self.config, days=1, now_ms=NOW,
                warmup_bars=720, checkpoint_dir=self.directory,
                progress=notices.append if notices is not None else None)

    def cache(self, client, kind="trade"):
        return HistoryPageCache(self.directory, inst_id=self.config.instrument,
            bar=self.config.bar, start_ms=client.start if kind == "funding" else client.first,
            end_ms=client.end, kind=kind)

    def test_failure_after_800_candles_resumes_exact_next_request_after_restart(self):
        first = self.client()
        original = first.get
        failed_cursor = first.end - 800 * 300_000
        def interrupted(endpoint, params):
            if endpoint == history.ENDPOINTS["trade"] and int(params["after"]) == failed_cursor:
                raise history.HistoryError("模拟请求超时", category="timeout")
            return original(endpoint, params)
        first.get = interrupted
        with self.assertRaises(history.HistoryError):
            self.fetch(first)
        self.assertEqual(len(list(self.directory.rglob("*.json"))), 8)

        second, notices = self.client(), []
        result = self.fetch(second, notices)
        requests = [params for endpoint, params in second.calls if endpoint == history.ENDPOINTS["trade"]]
        self.assertEqual(int(requests[0]["after"]), failed_cursor)
        self.assertEqual(len(requests), 3)
        self.assertTrue(any("已校验缓存成交价" in message and "800/1008" in message for message in notices))
        self.assertEqual(len(result.trade_candles), 1008)
        self.assertEqual(result.trade_candles[0].ts, first.first)
        history._validate(result)

    def test_mark_failure_keeps_complete_trade_pages_and_last_good_mark_page(self):
        first = self.client()
        original = first.get
        failed_cursor = first.end - 100 * 300_000
        def interrupted(endpoint, params):
            if endpoint == history.ENDPOINTS["mark"] and int(params["after"]) == failed_cursor:
                raise history.HistoryError("模拟连接中断", category="connection")
            return original(endpoint, params)
        first.get = interrupted
        with self.assertRaises(history.HistoryError):
            self.fetch(first)
        second = self.client()
        result = self.fetch(second)
        self.assertFalse(any(endpoint == history.ENDPOINTS["trade"] for endpoint, _ in second.calls))
        mark_requests = [params for endpoint, params in second.calls if endpoint == history.ENDPOINTS["mark"]]
        self.assertEqual(int(mark_requests[0]["after"]), failed_cursor)
        self.assertEqual(len(result.mark_candles), len(result.trade_candles))

        # A completed retry still refreshes current contract metadata, then
        # validates all public pages locally instead of downloading them again.
        third = self.client()
        self.assertEqual(self.fetch(third), result)
        self.assertEqual([endpoint for endpoint, _ in third.calls], [history.ENDPOINTS["instrument"]])

    def test_unconfirmed_or_gapped_page_is_not_saved_or_skipped_on_retry(self):
        for defect in ("unconfirmed", "gap"):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as temporary:
                self.directory = Path(temporary) / "downloads"
                broken = self.client()
                if defect == "unconfirmed":
                    broken.trade[5][8] = "0"
                else:
                    broken.trade.pop(5)
                with self.assertRaises(history.HistoryError):
                    self.fetch(broken)
                self.assertFalse(list(self.directory.rglob("*.json")))
                working = self.client()
                self.fetch(working)
                requests = [params for endpoint, params in working.calls if endpoint == history.ENDPOINTS["trade"]]
                self.assertEqual(int(requests[0]["after"]), working.end)

    def test_replay_revalidates_cached_ohlc_and_continuity_even_with_valid_digest(self):
        for rows in ("gap", "bad_ohlc"):
            with self.subTest(rows=rows), tempfile.TemporaryDirectory() as temporary:
                self.directory = Path(temporary) / "downloads"
                client = self.client()
                page = [client.trade[0], client.trade[2]] if rows == "gap" else [client.trade[0]]
                if rows == "bad_ohlc":
                    page[0][2] = "1"  # High below open; JSON/digest alone is insufficient.
                self.cache(client).save(client.end, page)
                with self.assertRaises(history.HistoryError):
                    self.fetch(client)
                self.assertEqual([endpoint for endpoint, _ in client.calls], [history.ENDPOINTS["instrument"]])

    def test_funding_coverage_still_required_after_resuming_both_complete_price_series(self):
        client = self.client()
        self.fetch(client)
        broken = self.client()
        rows = [row for index, row in enumerate(broken.funding) if index != 1]
        funding_cache = self.cache(broken, "funding")
        funding_cache.path_for(broken.end + 1).unlink()
        funding_cache.save(broken.end + 1, rows)
        with self.assertRaises(history.HistoryError):
            self.fetch(broken)
        self.assertEqual([endpoint for endpoint, _ in broken.calls], [history.ENDPOINTS["instrument"]])


if __name__ == "__main__":
    unittest.main()
