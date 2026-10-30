import copy
import io
import json
import unittest
import urllib.parse
from decimal import Decimal as D
from unittest.mock import Mock, patch

from okx_bot.okx import AmbiguousOrderError, Credentials, OkxClient, OkxError
from okx_bot.portfolio_config import DEFAULT_INSTRUMENTS
from okx_bot.read_retry import ReadRetryClient
from okx_bot.snapshot import AccountSnapshotClient, ALGO_FAMILIES


NOW = 1_800_000


def position(inst=DEFAULT_INSTRUMENTS[0]):
    return {"instId": inst, "pos": "1", "avgPx": "100", "posSide": "net", "mgnMode": "isolated", "lever": "1"}


def algo(inst=DEFAULT_INSTRUMENTS[0], family="oco"):
    return {"instId": inst, "algoId": "123", "algoClOrdId": "own1a", "ordType": family,
            "sz": "1", "posSide": "net", "tdMode": "isolated", "side": "sell",
            "slTriggerPx": "99", "slOrdPx": "-1", "slTriggerPxType": "mark",
            "tpTriggerPx": "102", "tpOrdPx": "-1", "tpTriggerPxType": "mark"}


class Client:
    def __init__(self):
        self.calls = []
        self.positions_data = []
        self.orders_data = []
        self.algos_data = {family: [] for family in ALGO_FAMILIES}
        self.marks = [{"instId": inst, "markPx": "100", "ts": str(NOW)} for inst in DEFAULT_INSTRUMENTS]
        self.failure_family = None

    def begin_cycle(self, seconds=60):
        self.calls.append(("begin_cycle", seconds))

    def swap_positions(self):
        self.calls.append(("swap_positions", None))
        return self.positions_data

    def swap_pending_orders(self):
        self.calls.append(("swap_pending_orders", None))
        return self.orders_data

    def swap_pending_algos(self, family):
        self.calls.append(("swap_pending_algos", family))
        if family == self.failure_family:
            raise OkxError("safe temporary failure", category="timeout")
        return self.algos_data[family]

    def swap_mark_prices(self):
        self.calls.append(("swap_mark_prices", None))
        return self.marks

    def positions(self, inst):
        self.calls.append(("positions", inst))
        return [row for row in self.positions_data if row["instId"] == inst]

    def pending_orders(self, inst):
        self.calls.append(("pending_orders", inst))
        return [row for row in self.orders_data if row["instId"] == inst]

    def pending_algo_orders(self, inst):
        self.calls.append(("pending_algo_orders", inst))
        return [row for rows in self.algos_data.values() for row in rows if row["instId"] == inst]

    def mark_price(self, inst):
        self.calls.append(("mark_price", inst))
        row = next(row for row in self.marks if row["instId"] == inst)
        return D(row["markPx"]), int(row["ts"])

    def balances(self):
        self.calls.append(("balances", None))
        return {"equity": D("5000"), "available": D("5000"), "currency": "USDT"}

    def candles(self, inst, *args):
        self.calls.append(("candles", inst))
        return []


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.client = Client()
        self.facade = AccountSnapshotClient(self.client)
        self.clock = patch("okx_bot.snapshot._now_ms", return_value=NOW)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def test_six_private_reads_are_reused_across_every_market_and_protection_check(self):
        self.client.positions_data = [position()]
        self.client.algos_data["oco"] = [algo()]
        self.facade.refresh()
        self.assertEqual(len(self.client.calls), 6)
        for inst in DEFAULT_INSTRUMENTS:
            self.facade.positions(inst)
            self.facade.pending_orders(inst)
            self.facade.pending_orders(inst)  # Existing bracket validator repeats this.
            self.facade.pending_algo_orders(inst)
        self.assertEqual(len(self.client.calls), 6)
        self.assertEqual(self.facade.pending_algo_orders(DEFAULT_INSTRUMENTS[0])[0]["algoId"], "123")
        self.assertEqual(len(self.facade.snapshot_read_times), 6)
        self.assertEqual(set(self.facade.snapshot_components), {"swap_positions", "swap_pending_orders", *("swap_algos_" + family for family in ALGO_FAMILIES)})

    def test_lazy_bulk_mark_read_is_reused_and_keeps_its_request_time(self):
        self.facade.refresh()
        for inst in DEFAULT_INSTRUMENTS:
            self.assertEqual(self.facade.mark_price(inst), (D(100), NOW))
        self.assertEqual(len(self.client.calls), 7)
        self.assertEqual(len(self.facade.snapshot_read_times), 7)
        self.assertEqual(self.facade.snapshot_components["swap_mark_prices"], {"started_at_ms": NOW, "finished_at_ms": NOW})

    def test_refresh_records_request_start_instead_of_relabelling_slow_reads_as_new(self):
        times = iter(range(NOW, NOW + 12_000, 1_000))
        with patch("okx_bot.snapshot._now_ms", side_effect=lambda: next(times)):
            self.facade.refresh()
        self.assertEqual(self.facade.snapshot_read_times, tuple(range(NOW, NOW + 12_000, 2_000)))
        self.assertEqual(self.facade.snapshot_components["swap_positions"], {"started_at_ms": NOW, "finished_at_ms": NOW + 1_000})

    def test_failed_last_component_never_promotes_partial_or_previous_snapshot(self):
        self.facade.refresh()
        self.client.failure_family = ALGO_FAMILIES[-1]
        with self.assertRaises(OkxError):
            self.facade.refresh()
        self.assertFalse(self.facade.has_snapshot)
        self.assertEqual(self.facade.snapshot_components, {})
        with self.assertRaises(OkxError):
            _ = self.facade.snapshot_read_times

    def test_unknown_active_positions_and_orders_are_blocked_not_filtered(self):
        for kind in ("position", "order", "algo"):
            with self.subTest(kind=kind):
                self.client = Client()
                self.facade = AccountSnapshotClient(self.client)
                if kind == "position":
                    self.client.positions_data = [position("DOGE-USDT-SWAP")]
                elif kind == "order":
                    self.client.orders_data = [{"instId": "DOGE-USDT-SWAP", "ordId": "1", "clOrdId": ""}]
                else:
                    self.client.algos_data["oco"] = [algo("DOGE-USDT-SWAP")]
                with self.assertRaises(OkxError) as raised:
                    self.facade.refresh()
                self.assertEqual(raised.exception.category, "invalid_response")
                self.assertFalse(self.facade.has_snapshot)

    def test_zero_historical_position_can_be_ignored_but_missing_fields_cannot(self):
        self.client.positions_data = [{"instId": "DOGE-USDT-SWAP", "pos": "0"}]
        self.facade.refresh()
        self.assertEqual(self.facade.positions(DEFAULT_INSTRUMENTS[0]), [])
        self.client.positions_data = [{"instId": "DOGE-USDT-SWAP"}]
        with self.assertRaises(OkxError):
            self.facade.refresh()

    def test_full_order_pages_and_conflicting_algo_families_are_rejected(self):
        for field in ("orders", "algos"):
            with self.subTest(field=field):
                self.client = Client()
                if field == "orders":
                    self.client.orders_data = [{"instId": DEFAULT_INSTRUMENTS[0], "ordId": str(i)} for i in range(100)]
                else:
                    self.client.algos_data["oco"] = [algo() for _ in range(100)]
                with self.assertRaises(OkxError):
                    AccountSnapshotClient(self.client).refresh()
        self.client = Client()
        self.client.algos_data["conditional"] = [algo(family="oco")]
        with self.assertRaises(OkxError):
            AccountSnapshotClient(self.client).refresh()

    def test_duplicate_positions_order_ids_and_algo_ids_are_rejected(self):
        for kind in ("position", "order", "algo"):
            with self.subTest(kind=kind):
                self.client = Client()
                if kind == "position":
                    self.client.positions_data = [position(), position()]
                elif kind == "order":
                    self.client.orders_data = [{"instId": inst, "ordId": "1"} for inst in DEFAULT_INSTRUMENTS[:2]]
                else:
                    self.client.algos_data["oco"] = [algo(), algo(DEFAULT_INSTRUMENTS[1])]
                with self.assertRaises(OkxError):
                    AccountSnapshotClient(self.client).refresh()

    def test_views_and_timing_metadata_cannot_mutate_cached_state(self):
        self.client.positions_data = [position()]
        self.facade.refresh()
        self.facade.positions(DEFAULT_INSTRUMENTS[0])[0]["pos"] = "500"
        self.facade.snapshot_components["swap_positions"]["started_at_ms"] = 0
        self.client.positions_data[0]["pos"] = "200"
        self.assertEqual(self.facade.positions(DEFAULT_INSTRUMENTS[0])[0]["pos"], "1")
        self.assertEqual(self.facade.snapshot_components["swap_positions"]["started_at_ms"], NOW)

    def test_mutation_invalidates_before_success_or_failure_and_fill_reads_are_live(self):
        for name in ("place_market_order", "set_leverage", "unknown_callable"):
            with self.subTest(name=name):
                self.facade.refresh()
                observed = []
                def fail(*args, **kwargs):
                    observed.append(self.facade.has_snapshot)
                    raise AmbiguousOrderError("safe ambiguous result")
                setattr(self.client, name, fail)
                with self.assertRaises(AmbiguousOrderError):
                    getattr(self.facade, name)()
                self.assertEqual(observed, [False])
                self.client.positions_data = [position()]
                before = len(self.client.calls)
                self.assertEqual(self.facade.positions(DEFAULT_INSTRUMENTS[0])[0]["pos"], "1")
                self.assertEqual(len(self.client.calls), before + 1)
                self.assertEqual(self.client.calls[-1], ("positions", DEFAULT_INSTRUMENTS[0]))

    def test_known_non_cached_reads_and_begin_cycle_do_not_destroy_snapshot(self):
        self.facade.refresh()
        self.facade.begin_cycle(60)
        self.facade.candles(DEFAULT_INSTRUMENTS[0], "5m", 100)
        self.facade.balances()
        self.assertTrue(self.facade.has_snapshot)
        self.assertEqual(self.facade.positions(DEFAULT_INSTRUMENTS[0]), [])

    def test_bad_bulk_mark_data_invalidates_account_view_without_fallback(self):
        for rows in ([], [{"instId": DEFAULT_INSTRUMENTS[0], "markPx": "NaN", "ts": str(NOW)}],
                     [{"instId": inst, "markPx": "100", "ts": "bad"} for inst in DEFAULT_INSTRUMENTS]):
            with self.subTest(rows=len(rows)):
                self.facade.refresh()
                self.client.marks = rows
                with self.assertRaises(OkxError):
                    self.facade.mark_price(DEFAULT_INSTRUMENTS[0])
                self.assertFalse(self.facade.has_snapshot)

    def test_idle_four_market_cycle_can_use_twelve_or_thirteen_gets(self):
        # Two balances + one positions + one ordinary + four algo + four candles
        # = 12 GETs. Four held mark reads share exactly one extra public GET.
        self.facade.balances()
        self.facade.refresh()
        for inst in DEFAULT_INSTRUMENTS:
            self.facade.positions(inst)
            self.facade.pending_orders(inst)
            self.facade.pending_orders(inst)
            self.facade.pending_algo_orders(inst)
            self.facade.candles(inst, "5m", 100)
        self.facade.balances()
        self.assertEqual(len(self.client.calls), 12)
        for inst in DEFAULT_INSTRUMENTS:
            self.facade.mark_price(inst)
        self.assertEqual(len(self.client.calls), 13)

    def test_real_adapter_fake_http_transport_confirms_twelve_or_thirteen_gets(self):
        requests = []
        def opener(request, timeout):
            requests.append(request)
            path = urllib.parse.urlsplit(request.full_url).path
            if path == "/api/v5/account/balance":
                data = [{"details": [{"ccy": "USDT", "eq": "5000", "availEq": "5000"}]}]
            elif path == "/api/v5/public/mark-price":
                data = self.client.marks
            else:
                data = []
            return io.BytesIO(json.dumps({"code": "0", "data": data}).encode())
        class Stop:
            def is_set(self): return False
            def wait(self, delay): raise AssertionError("successful reads must not back off")
        adapter = OkxClient(Credentials("test-key", "test-secret", "test-passphrase"), opener=opener)
        facade = AccountSnapshotClient(ReadRetryClient(adapter, Stop(), classify_exhausted=True))
        facade.begin_cycle(60)
        facade.balances()
        facade.refresh()
        for inst in DEFAULT_INSTRUMENTS:
            facade.positions(inst)
            facade.pending_orders(inst)
            facade.pending_orders(inst)
            facade.pending_algo_orders(inst)
            facade.candles(inst, "5m", 100)
        facade.balances()
        self.assertEqual(len(requests), 12)
        for inst in DEFAULT_INSTRUMENTS:
            facade.mark_price(inst)
        self.assertEqual(len(requests), 13)
        self.assertTrue(all(request.get_method() == "GET" for request in requests))
        paths = [urllib.parse.urlsplit(request.full_url).path for request in requests]
        self.assertEqual(paths.count("/api/v5/account/positions"), 1)
        self.assertEqual(paths.count("/api/v5/trade/orders-pending"), 1)
        self.assertEqual(paths.count("/api/v5/trade/orders-algo-pending"), 4)
        self.assertEqual(paths.count("/api/v5/market/candles"), 4)
        self.assertEqual(paths.count("/api/v5/account/balance"), 2)
        self.assertEqual(paths.count("/api/v5/public/mark-price"), 1)


if __name__ == "__main__":
    unittest.main()
