"""Frozen low-turnover gates and accepted-fill accounting, without market IO."""
from dataclasses import replace
from decimal import Decimal as D
import unittest

from okx_bot.okx import Candle
from okx_bot.research_low_turnover_signals import (BAR_MS, COOLDOWN_MS, DAY_MS,
    FROZEN_AT_UTC, HOUR_MS, LowTurnoverStrategy, RULES, VARIANT_NAME)
from okx_bot.research_signals import HourlyBreakoutStrategy


def bar(index, price, *, wick="0.1"):
    price, width = D(str(price)), D(wick)
    return Candle(index * BAR_MS, price, price + width, price - width, price)


def warm(strategy, *, short=False, wick="0.1", hours=None):
    rows = []
    for index in range(720):
        hour = index // 12
        price = (D(200) - D(hour) / 100 if short else D(100) + D(hour) / 100) if hours is None else hours[hour]
        item = bar(index, price, wick=wick)
        strategy.update(item)
        rows.append(item)
    return rows


def with_costs(fee="0", slip="0"):
    return LowTurnoverStrategy(fee_rate=D(fee), slippage_rate=D(slip))


def stop_signal(strategy, stop):
    price = D(102)
    # Warmup's constant 0.2 TR gives ATR=0.2. These symmetric ranges contain
    # the preceding 100.59 close, so the next Wilder ATR is exact.
    target_atr = price * D(stop) / D("1.5")
    true_range = target_atr * 14 - D("0.2") * 13
    return strategy.update(Candle(720 * BAR_MS, price,
                                  price + true_range / 2, price - true_range / 2, price))


class LowTurnoverTests(unittest.TestCase):
    def test_definition_and_frozen_parameters_are_explicit_and_immutable(self):
        self.assertEqual(VARIANT_NAME, "low_turnover_v2")
        self.assertEqual(FROZEN_AT_UTC, "2026-10-30T15:19:06Z")
        self.assertEqual(RULES["hourly_sma_slope_lookback_hours"], 3)
        self.assertEqual(RULES["breakout_atr_buffer"], "0.25")
        self.assertEqual(RULES["minimum_net_reward_risk"], "1.5")
        self.assertEqual(RULES["data_role"], "development")
        with self.assertRaises(TypeError):
            RULES["breakout_atr_buffer"] = "0.1"

    def test_slope_filters_long_and_short_even_when_original_hourly_trend_agrees(self):
        for short in (False, True):
            for slope in (-1, 0, 1):
                with self.subTest(short=short, slope=slope):
                    hours = ([D(100)] * 37 + ([D(120)] * 18 + [D(118)] * 5 if slope < 0
                              else [D(120)] * 23 if slope == 0
                              else [D(118)] * 18 + [D(120)] * 5))
                    if short:
                        hours = [D(250) - value for value in hours]
                    strategy, original = with_costs(), HourlyBreakoutStrategy()
                    for item in warm(strategy, hours=hours):
                        original.update(item)
                    close = hours[-1] + (D("-0.3") if short else D("0.3"))
                    item = bar(720, close)
                    base_signal = original.update(item)
                    self.assertEqual(base_signal.side, "short" if short else "long")
                    result = strategy.update(item)
                    if slope > 0:
                        self.assertEqual(result.side, base_signal.side)
                    else:
                        self.assertIsNone(result)

    def test_atr_breakout_buffer_exact_boundary_and_mirror_short_side(self):
        for short in (False, True):
            for epsilon, accepted in ((D("-0.000001"), False), (D(0), True), (D("0.000001"), True)):
                with self.subTest(short=short, epsilon=epsilon):
                    strategy = with_costs()
                    warm(strategy, short=short, wick="0.11")
                    previous = D("199.41") if short else D("100.59")
                    move = D("0.164") + epsilon
                    price = previous - move if short else previous + move
                    result = strategy.update(bar(720, price, wick="0"))
                    if accepted:
                        self.assertEqual(result.side, "short" if short else "long")
                        if epsilon == 0:
                            self.assertEqual(result.atr, D("0.216"))
                    else:
                        self.assertIsNone(result)

    def test_effective_scenario_costs_can_reject_the_same_exact_signal(self):
        normal, stressed = with_costs("0.0005", "0.0002"), with_costs("0.001", "0.0004")
        warm(normal)
        warm(stressed)
        signal = stop_signal(normal, "0.01")
        self.assertEqual(signal.stop_loss_pct, D("0.01"))
        self.assertEqual(signal.take_profit_pct, D("0.02"))
        self.assertIsNone(stop_signal(stressed, "0.01"))
        self.assertEqual(normal.round_trip_friction, D("0.0014"))
        self.assertEqual(stressed.round_trip_friction, D("0.0028"))

    def test_net_reward_risk_threshold_includes_equality_without_division_rounding(self):
        exact = with_costs("0.0005", "0.0002")
        higher_cost = with_costs("0.000500001", "0.0002")
        warm(exact)
        warm(higher_cost)
        self.assertIsNotNone(stop_signal(exact, "0.007"))
        self.assertIsNone(stop_signal(higher_cost, "0.007"))

    def test_stress_costs_above_live_profile_bounds_are_accepted_but_negative_reward_is_not(self):
        strategy = with_costs("0.02", "0.03")
        warm(strategy)
        self.assertIsNone(stop_signal(strategy, "0.01"))
        for invalid in (D("NaN"), D("Infinity"), D("-0.0001"), D(1), 0.0005, True):
            for field in ("fee_rate", "slippage_rate"):
                kwargs = {"fee_rate": D(0), "slippage_rate": D(0), field: invalid}
                with self.subTest(field=field, invalid=invalid), self.assertRaises(ValueError):
                    LowTurnoverStrategy(**kwargs)

    def test_generated_unfilled_signals_do_not_consume_daily_entries_or_cooldown(self):
        strategy = with_costs()
        warm(strategy)
        signals = []
        for index, price in enumerate((101, "100.9", "101.4", "101.2", "101.8"), 720):
            signal = strategy.update(bar(index, price))
            if signal is not None:
                signals.append(signal)
        self.assertEqual(len(signals), 3)
        self.assertEqual([signal.ready_at_ms for signal in signals], [721 * BAR_MS, 723 * BAR_MS, 725 * BAR_MS])

    def test_accepted_fills_enforce_two_per_utc8_day_and_six_hours(self):
        strategy = with_costs()
        day_start = 16 * HOUR_MS  # UTC+8 midnight, not UTC midnight.
        strategy.on_entry(day_start)
        with self.assertRaises(ValueError):
            strategy.on_entry(day_start + COOLDOWN_MS - BAR_MS)
        strategy.on_entry(day_start + COOLDOWN_MS)
        with self.assertRaises(ValueError):
            strategy.on_entry(day_start + 2 * COOLDOWN_MS)
        strategy.on_entry(day_start + DAY_MS)
        strategy.on_entry(day_start + DAY_MS + COOLDOWN_MS)

    def test_utc8_day_rollover_does_not_reset_cooldown(self):
        strategy = with_costs()
        day_start = 16 * HOUR_MS
        strategy.on_entry(day_start + 23 * HOUR_MS)
        with self.assertRaises(ValueError):
            strategy.on_entry(day_start + DAY_MS)
        strategy.on_entry(day_start + DAY_MS + 5 * HOUR_MS)
        strategy.on_entry(day_start + DAY_MS + 11 * HOUR_MS)
        with self.assertRaises(ValueError):
            strategy.on_entry(day_start + DAY_MS + 17 * HOUR_MS)

    def test_cooldown_uses_signal_next_open_and_allows_exact_six_hour_boundary(self):
        strategy = with_costs()
        warm(strategy)
        first = strategy.update(bar(720, 101))
        strategy.on_entry(first.ready_at_ms)
        for index in range(721, 792):
            strategy.update(bar(index, "100.9"))
        second = strategy.update(bar(792, "101.5"))
        self.assertIsNotNone(second)
        self.assertEqual(second.ready_at_ms - first.ready_at_ms, COOLDOWN_MS)
        strategy.on_entry(second.ready_at_ms)

    def test_throttle_rejections_still_consume_breakouts_and_never_queue_them(self):
        strategy = with_costs()
        warm(strategy)
        first = strategy.update(bar(720, 101))
        strategy.on_entry(first.ready_at_ms)
        strategy.update(bar(721, "100.9"))
        for index in range(722, 795):
            self.assertIsNone(strategy.update(bar(index, D("101.4") + D("0.2") * (index - 722))))
        # The continuous breakout did not revive when the six hours expired.
        strategy.update(bar(795, "115.7"))
        self.assertIsNotNone(strategy.update(bar(796, "116.1")))

    def test_duplicate_conflicting_and_invalid_entry_events_cannot_double_count(self):
        strategy = with_costs()
        for invalid in (-BAR_MS, 1, True, BAR_MS + 0.0):
            with self.subTest(timestamp=invalid), self.assertRaises(ValueError):
                strategy.on_entry(invalid)
        strategy.on_entry(BAR_MS)
        with self.assertRaises(ValueError):
            strategy.on_entry(BAR_MS)
        with self.assertRaises(ValueError):
            strategy.on_entry(0)
        first = bar(0, 100)
        strategy.update(first)
        self.assertIsNone(strategy.update(first))
        with self.assertRaises(ValueError):
            strategy.update(replace(first, close=D("100.05")))
        with self.assertRaises(ValueError):
            strategy.update(bar(2, 100))


if __name__ == "__main__":
    unittest.main()
