"""Deterministic candidate semantics; no market download or order IO."""
from dataclasses import FrozenInstanceError, replace
from decimal import Decimal as D
import unittest

from okx_bot.okx import Candle
from okx_bot.research_signals import (BAR_MS, HOUR_MS, HourlyBreakoutStrategy,
                                      RESEARCH_WARMUP_BARS, Signal)


def bar(index, close, *, wick="0.1"):
    price, spread = D(str(close)), D(wick)
    return Candle(index * BAR_MS, price, price + spread, price - spread, price)


def warm(strategy, *, short=False, first=0, step=D(1)):
    bars = []
    for index in range(first, first + RESEARCH_WARMUP_BARS):
        price = D(200) - D(index // 12) * step if short else D(100) + D(index // 12) * step
        item = bar(index, price)
        strategy.update(item)
        bars.append(item)
    return bars


def breakout_with_stop(strategy, raw_stop):
    # Choose this bar's range so Wilder's next ATR gives the declared raw stop.
    price = D(102)
    target_atr = price * D(raw_stop) / strategy.atr_multiple
    true_range = target_atr * 14 - strategy.atr * 13
    item = Candle(RESEARCH_WARMUP_BARS * BAR_MS, price,
                  price + true_range / 2, price - true_range / 2, price)
    return strategy.update(item)


class ResearchSignalTests(unittest.TestCase):
    def test_hourly_trend_waits_for_fifty_complete_aligned_hours(self):
        strategy = HourlyBreakoutStrategy()
        for index in range(50 * 12 - 1):
            self.assertIsNone(strategy.update(bar(index, 100 + index // 12)))
        self.assertIsNone(strategy.trend)
        self.assertEqual(strategy.last_hour_end_ms, 49 * HOUR_MS)
        strategy.update(bar(50 * 12 - 1, 149))
        self.assertEqual(strategy.trend, "long")
        self.assertEqual(strategy.last_hour_end_ms, 50 * HOUR_MS)

    def test_initial_partial_hour_cannot_count_toward_fifty_hours(self):
        strategy = HourlyBreakoutStrategy()
        for index in range(1, 51 * 12 - 1):
            strategy.update(bar(index, 100 + index // 12))
        self.assertIsNone(strategy.trend)
        self.assertEqual(strategy.last_hour_end_ms, 50 * HOUR_MS)
        strategy.update(bar(51 * 12 - 1, 150))
        self.assertEqual(strategy.trend, "long")
        self.assertEqual(strategy.last_hour_end_ms, 51 * HOUR_MS)

    def test_partial_future_hour_does_not_flip_trend_or_create_a_signal(self):
        strategy = HourlyBreakoutStrategy()
        for index in range(50 * 12):
            strategy.update(bar(index, 101 if index // 12 < 30 else 100))
        self.assertEqual(strategy.trend, "short")
        for index in range(600, 611):
            self.assertIsNone(strategy.update(bar(index, 120 + index - 600)))
            self.assertEqual(strategy.trend, "short")
            self.assertEqual(strategy.last_hour_end_ms, 50 * HOUR_MS)
        # The completed hour may change trend, but cannot revive its old break.
        self.assertIsNone(strategy.update(bar(611, 131)))
        self.assertEqual(strategy.trend, "long")
        strategy.update(bar(612, 130))
        self.assertEqual(strategy.update(bar(613, 132)).side, "long")

    def test_prior_high_excludes_current_wick_and_breakout_is_not_repeated(self):
        strategy = HourlyBreakoutStrategy()
        warm(strategy)
        first = bar(720, "160", wick="1")
        signal = strategy.update(first)
        self.assertIsInstance(signal, Signal)
        self.assertEqual(signal.side, "long")
        self.assertEqual(signal.ready_at_ms, first.ts + BAR_MS)
        self.assertEqual(signal.stop_loss_pct, D("0.005"))
        self.assertEqual(signal.take_profit_pct, D("0.01"))
        self.assertIsNone(strategy.update(bar(721, "162")))
        self.assertIsNone(strategy.update(bar(722, "163")))
        self.assertIsNone(strategy.update(bar(723, "161")))
        self.assertEqual(strategy.update(bar(724, "164")).side, "long")
        with self.assertRaises(FrozenInstanceError):
            signal.side = "short"

    def test_prior_wick_and_equal_threshold_block_close_only_breakout(self):
        strategy = HourlyBreakoutStrategy()
        warm(strategy)
        # Last warmup high=159.1; a higher close below/equal to it is no break.
        self.assertIsNone(strategy.update(bar(720, "159.05", wick="0")))
        self.assertIsNone(strategy.update(bar(721, "159.1", wick="0")))
        self.assertEqual(strategy.update(bar(722, "159.2", wick="0")).side, "long")

    def test_short_side_uses_prior_lows_and_the_same_fresh_event_rule(self):
        strategy = HourlyBreakoutStrategy()
        warm(strategy, short=True)
        self.assertEqual(strategy.trend, "short")
        signal = strategy.update(bar(720, 140))
        self.assertEqual(signal.side, "short")
        self.assertIsNone(strategy.update(bar(721, 139)))
        strategy.update(bar(722, 140))
        self.assertEqual(strategy.update(bar(723, 138)).side, "short")

    def test_wilder_seed_requires_fourteen_true_ranges_then_uses_rma(self):
        strategy = HourlyBreakoutStrategy()
        for index in range(14):
            strategy.update(bar(index, 100, wick="1"))
        self.assertIsNone(strategy.atr)
        strategy.update(bar(14, 100, wick="1"))
        self.assertEqual(strategy.atr, D(2))
        strategy.update(bar(15, 100, wick="8"))
        self.assertEqual(strategy.atr, D(3))
        strategy.update(bar(16, 100, wick="8.5"))
        self.assertEqual(strategy.atr, D(4))

    def test_true_range_includes_gap_from_previous_close(self):
        strategy = HourlyBreakoutStrategy()
        strategy.update(bar(0, 100, wick="0"))
        strategy.update(Candle(BAR_MS, D(110), D(111), D(109), D(110)))
        for index in range(2, 15):
            strategy.update(bar(index, 110, wick="0"))
        self.assertEqual(strategy.atr, D(11) / 14)

    def test_atr_stop_and_two_r_take_profit_are_frozen_in_signal(self):
        strategy = HourlyBreakoutStrategy()
        warm(strategy, step=D("0.01"))
        signal = breakout_with_stop(strategy, "0.02")
        self.assertEqual(signal.stop_loss_pct, D("0.02"))
        self.assertEqual(signal.take_profit_pct, D("0.04"))
        atr = signal.atr
        strategy.update(bar(721, 199))
        self.assertEqual(signal.atr, atr)
        self.assertNotEqual(strategy.atr, atr)

    def test_three_percent_boundary_allowed_but_wider_stop_skips_and_consumes_event(self):
        strategy = HourlyBreakoutStrategy()
        warm(strategy, step=D("0.01"))
        self.assertEqual(breakout_with_stop(strategy, "0.03").stop_loss_pct, D("0.03"))
        strategy = HourlyBreakoutStrategy()
        warm(strategy, step=D("0.01"))
        self.assertIsNone(breakout_with_stop(strategy, "0.03000001"))
        # Even after ATR decays below the cap, continuing the old break is
        # still not a new event. A later genuine reset is required.
        for index in range(721, 741):
            self.assertIsNone(strategy.update(bar(index, 230 + index - 721)))
        self.assertLess(strategy.atr_multiple * strategy.atr / D(249), D("0.03"))

    def test_identical_duplicate_is_idempotent_but_conflict_gap_and_old_data_fail(self):
        strategy = HourlyBreakoutStrategy()
        item = bar(0, 100)
        strategy.update(item)
        self.assertIsNone(strategy.update(item))
        self.assertIsNone(strategy.atr)
        for invalid in (replace(item, close=D("100.05")), bar(2, 101)):
            with self.assertRaises(ValueError):
                strategy.update(invalid)
        strategy.update(bar(1, 100))
        with self.assertRaises(ValueError):
            strategy.update(item)

    def test_invalid_ohlc_timestamps_and_parameters_fail_before_state_changes(self):
        strategy = HourlyBreakoutStrategy()
        original = bar(0, 100)
        for invalid in (replace(original, ts=1), replace(original, ts=True),
                        replace(original, close=D("NaN")), replace(original, high=D(99)),
                        replace(original, low=D(-1)), replace(original, open=100.0)):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                strategy.update(invalid)
        strategy.update(original)
        for params in ({"min_stop_loss_pct": D(0)}, {"max_stop_loss_pct": D("0.004")},
                       {"atr_multiple": D("Infinity")}, {"reward_risk": 2.0}):
            with self.subTest(params=params), self.assertRaises(ValueError):
                HourlyBreakoutStrategy(**params)

    def test_future_suffix_cannot_change_already_emitted_prefix(self):
        prefix = [bar(index, 100 + index // 12) for index in range(720)]
        prefix.extend([bar(720, 160), bar(721, 161), bar(722, 159), bar(723, 162)])
        first, second = HourlyBreakoutStrategy(), HourlyBreakoutStrategy()
        expected = [first.update(item) for item in prefix]
        actual = [second.update(item) for item in prefix]
        for index in range(724, 760):
            second.update(bar(index, 150 + index % 20))
        self.assertEqual(actual, expected)
        self.assertTrue(any(signal is not None for signal in expected))

    def test_baseline_signal_can_explicitly_have_no_atr(self):
        signal = Signal("long", D("0.01"), D("0.02"), None, BAR_MS)
        self.assertIsNone(signal.atr)
        for invalid in (D("NaN"), D(-1), "2"):
            with self.subTest(atr=invalid), self.assertRaises(ValueError):
                Signal("long", D("0.01"), D("0.02"), invalid, BAR_MS)


if __name__ == "__main__":
    unittest.main()
