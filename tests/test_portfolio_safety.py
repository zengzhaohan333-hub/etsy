"""Independent cross-market safety regressions using the owner's fake exchange."""
import json
from decimal import Decimal as D
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from okx_bot import portfolio
from test_portfolio import FakePortfolioClient, INTERVAL, MARKETS, NOW, NoSleepStop, config


class MixedSignals:
    def __init__(self, fast, slow):
        pass

    def update(self, candle):
        # Each fake market has its own scale: BTC 100, ETH 10, SOL 1, XRP .1.
        # Their different directions must never offset gross exposure.
        return "long" if candle.close in (D(100), D(1)) else "short"


class PortfolioSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.client = FakePortfolioClient(self.directory)
        self.stop = NoSleepStop()
        for target in ("okx_bot.portfolio._now_ms", "okx_bot.demo._now_ms"):
            clock = patch(target, side_effect=lambda: self.client.now)
            clock.start()
            self.addCleanup(clock.stop)
        strategy = patch("okx_bot.portfolio.CrossStrategy", MixedSignals)
        strategy.start()
        self.addCleanup(strategy.stop)

    def run_bot(self, profile=None, *, cycles=1):
        return portfolio.run_portfolio(profile or config(), self.directory, self.stop,
                                       poll_seconds=.001, client=self.client, max_cycles=cycles)

    def test_two_longs_and_two_shorts_still_use_shared_800_gross_cap(self):
        result = self.run_bot()
        self.assertFalse(result["halted"], result)
        self.assertEqual(len(self.client.place_calls), 4)
        orders = {order["instrument"]: order for order in self.client.place_calls}
        for market, expected in zip(MARKETS, ("buy", "sell", "buy", "sell")):
            self.assertEqual(orders[market]["side"], expected)
            self.assertLessEqual(orders[market]["notional"], D(200))
            self.assertGreater(orders[market]["notional"], D(190))
        actual_gross = self.client.current_notional()
        self.assertGreater(actual_gross, D(790))
        self.assertLessEqual(actual_gross, D(800))
        self.assertEqual(D(result["gross_notional"]), actual_gross)
        self.assertEqual([result["markets"][market]["position"]["side"] for market in MARKETS],
                         ["long", "short", "long", "short"])

    def test_held_long_and_short_do_not_net_to_zero_when_shared_cap_is_full(self):
        def move_held_marks_after_second_entry(market):
            if market == MARKETS[1]:
                # Both marks move just 0.5%, within their 1% SL / 2% TP bands.
                # Existing gross now exceeds the 400 cap through price movement.
                for held in MARKETS[:2]:
                    self.client.prices[held] *= D("1.005")
        self.client.after_place = move_held_marks_after_second_entry
        result = self.run_bot(config(max_total_notional=D(400)))
        self.assertFalse(result["halted"], result)
        self.assertEqual([order["instrument"] for order in self.client.place_calls], list(MARKETS[:2]))
        self.assertEqual([order["side"] for order in self.client.place_calls], ["buy", "sell"])
        self.assertGreater(self.client.current_notional(), D(400))
        self.assertEqual(D(result["gross_notional"]), self.client.current_notional())
        for held in MARKETS[:2]:
            self.assertTrue(self.client.position_rows[held])
            self.assertTrue(self.client.algo_rows[held])
        for skipped in MARKETS[2:]:
            self.assertEqual(result["markets"][skipped]["entries"], 0)
            self.assertEqual(result["markets"][skipped]["last_event"], "portfolio_cap_reached")

    def test_mark_aging_during_intent_write_prevents_the_first_market_submit(self):
        class OnlyBtcSignal:
            def __init__(self, fast, slow):
                pass
            def update(self, candle):
                return "long" if candle.close == D(100) else None
        original_mark = self.client.mark_price
        def slightly_old_mark(market):
            price, timestamp = original_mark(market)
            return price, timestamp - 10_000
        self.client.mark_price = slightly_old_mark
        original_save = portfolio._save
        def slow_intent(path, payload):
            original_save(path, payload)
            if path.name == "demo-journal.json" and payload["orders"][-1]["status"] == "intent":
                self.client.now += 25_000
        with patch("okx_bot.portfolio.CrossStrategy", OnlyBtcSignal), \
             patch("okx_bot.portfolio._save", side_effect=slow_intent):
            result = self.run_bot()
        self.assertFalse(result["halted"], result)
        self.assertEqual(self.client.place_calls, [])
        self.assertEqual(result["markets"][MARKETS[0]]["skipped_signals"], 1)
        journal = json.loads((self.directory / "demo-journal.json").read_text())
        self.assertEqual(journal["orders"][0]["status"], "closed")
        self.assertTrue(journal["orders"][0]["never_submitted"])

    def test_one_second_http_latency_keeps_all_four_signals_eligible_and_equity_fresh(self):
        self.client.now = NOW - INTERVAL
        elapsed = [0.0]
        startup_finished = [False]
        submit_times = []
        methods = ("account_config", "server_time", "instrument", "positions",
                   "pending_orders", "pending_algo_orders", "candles", "balances",
                   "mark_price", "set_leverage", "get_leverage", "place_market_order", "query_order")
        def with_latency(name, method):
            def request(*args, **kwargs):
                if name == "place_market_order":
                    submit_times.append(self.client.now)
                # The adapter queries four algorithm order types separately.
                seconds = 4 if name == "pending_algo_orders" else 1
                self.client.now += seconds * 1000
                elapsed[0] += seconds
                result = method(*args, **kwargs)
                if name == "get_leverage" and args[0] == MARKETS[-1] and not startup_finished[0]:
                    # A genuinely new 5m candle closes after all startup reads.
                    # Wall time advances to the boundary; monotonic does not jump.
                    self.client.now = NOW
                    startup_finished[0] = True
                return result
            return request
        for name in methods:
            setattr(self.client, name, with_latency(name, getattr(self.client, name)))
        with patch("okx_bot.read_retry.time.monotonic", side_effect=lambda: elapsed[0]):
            result = portfolio.run_portfolio(config(), self.directory, self.stop,
                                           poll_seconds=15, client=self.client, max_cycles=1)
        self.assertFalse(result["halted"], result)
        self.assertEqual(len(self.client.place_calls), 4)
        self.assertEqual({order["instrument"] for order in self.client.place_calls}, set(MARKETS))
        self.assertTrue(any(timestamp - NOW > 30_000 for timestamp in submit_times))
        self.assertEqual(result["skipped_signals"], 0)
        self.assertLessEqual(self.client.current_notional(), D(800))
        self.assertEqual(result["last_success_at_ms"], self.client.now)
        self.assertLessEqual(self.client.now - result["equity_checked_at_ms"], 1000)

    def test_next_new_signal_batch_rotates_priority_after_exchange_exits(self):
        def exchange_exits_and_next_bar(seconds):
            if seconds == .001:
                # Emulate native TP/SL completing every old position; the bot
                # must reconcile those before taking a new crossing batch.
                for market in MARKETS:
                    self.client.position_rows[market] = []
                    self.client.algo_rows[market] = []
                self.client.now += INTERVAL
        self.stop.on_wait = exchange_exits_and_next_bar
        result = self.run_bot(cycles=2)
        self.assertFalse(result["halted"], result)
        self.assertEqual(len(self.client.place_calls), 8)
        first = [order["instrument"] for order in self.client.place_calls[:4]]
        second = [order["instrument"] for order in self.client.place_calls[4:]]
        self.assertEqual(first, list(MARKETS))
        self.assertEqual(second, list(MARKETS[1:] + MARKETS[:1]))
        self.assertLessEqual(self.client.current_notional(), D(800))
        journal = json.loads((self.directory / "demo-journal.json").read_text())
        self.assertTrue(all(order["status"] == "closed" for order in journal["orders"][:4]))
        self.assertTrue(all(order["status"] == "protected" for order in journal["orders"][4:]))


if __name__ == "__main__":
    unittest.main()
