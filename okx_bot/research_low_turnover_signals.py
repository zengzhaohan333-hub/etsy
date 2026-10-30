"""One predeclared lower-turnover research candidate; no execution or fitting.

Every candle advances the original hourly-trend/Donchian/ATR strategy, including
when additional gates reject an event. Only ``on_entry`` records accepted fills;
a generated, rejected, or unfilled signal never consumes an entry allowance.
This candidate and reused historical data are development research, not a new
holdout result or a daily-return commitment.
"""
from __future__ import annotations

from collections import deque
from decimal import Decimal
from types import MappingProxyType

from .okx import Candle
from .research_signals import (BAR_MS, BREAKOUT_BARS, HOUR_MS,
                               HourlyBreakoutStrategy, Signal, TREND_FAST_HOURS)


D = Decimal
VARIANT_NAME = "low_turnover_v2"
FROZEN_AT_UTC = "2026-10-30T15:19:06Z"
SLOPE_HOURS = 3
BREAKOUT_ATR_BUFFER = D("0.25")
MIN_NET_REWARD_RISK = D("1.5")
COOLDOWN_MS = 6 * HOUR_MS
MAX_ENTRIES_PER_DAY = 2
UTC8_OFFSET_MS = 8 * HOUR_MS
DAY_MS = 86_400_000
RULES = MappingProxyType({
    "variant_name": VARIANT_NAME,
    "frozen_at_utc": FROZEN_AT_UTC,
    "hourly_sma_slope_lookback_hours": SLOPE_HOURS,
    "breakout_atr_buffer": str(BREAKOUT_ATR_BUFFER),
    "minimum_net_reward_risk": str(MIN_NET_REWARD_RISK),
    "round_trip_friction": "2*(effective_fee_rate+effective_slippage_rate)",
    "accepted_entry_cooldown_ms": COOLDOWN_MS,
    "max_accepted_entries_per_market_utc8_day": MAX_ENTRIES_PER_DAY,
    "data_role": "development",
})


def _cost(value: Decimal) -> bool:
    return isinstance(value, Decimal) and value.is_finite() and 0 <= value < 1


def _entry_timestamp(value: int) -> None:
    if type(value) is not int or not 0 <= value <= 32_503_680_000_000 or value % BAR_MS:
        raise ValueError("Accepted research entry must have an aligned nonnegative 5m timestamp")


def _utc8_day(timestamp_ms: int) -> int:
    return (timestamp_ms + UTC8_OFFSET_MS) // DAY_MS


class LowTurnoverStrategy:
    """Frozen gates over the original candidate, with accepted-fill throttles.

    Costs are the scenario's effective rates, including stress multipliers.
    Entry eligibility is checked at the signal's next-open ``ready_at_ms``.
    ``on_entry`` must be called exactly once after a successful accepted fill;
    it independently rejects violations of the per-market daily/cooldown caps.
    A UTC+8 midnight resets the day count, while the six-hour cooldown persists.
    """

    def __init__(self, *, fee_rate: Decimal, slippage_rate: Decimal):
        if not _cost(fee_rate) or not _cost(slippage_rate):
            raise ValueError("Research costs must be finite decimal fractions from zero to below one")
        self.fee_rate = fee_rate
        self.slippage_rate = slippage_rate
        self.round_trip_friction = 2 * (fee_rate + slippage_rate)
        self._base = HourlyBreakoutStrategy()
        self._prior: deque[Candle] = deque(maxlen=BREAKOUT_BARS)
        self._hour_closes: deque[Decimal] = deque(maxlen=TREND_FAST_HOURS + SLOPE_HOURS)
        self._last_ts: int | None = None
        self._last_entry_ms: int | None = None
        self._entry_day: int | None = None
        self._entries_today = 0

    def _entry_allowed(self, timestamp_ms: int) -> bool:
        if self._last_entry_ms is not None and timestamp_ms - self._last_entry_ms < COOLDOWN_MS:
            return False
        return self._entry_day != _utc8_day(timestamp_ms) or self._entries_today < MAX_ENTRIES_PER_DAY

    def on_entry(self, timestamp_ms: int) -> None:
        _entry_timestamp(timestamp_ms)
        if not self._entry_allowed(timestamp_ms):
            raise ValueError("Accepted research entry violates the frozen daily limit or cooldown")
        day = _utc8_day(timestamp_ms)
        self._entries_today = self._entries_today + 1 if self._entry_day == day else 1
        self._entry_day = day
        self._last_entry_ms = timestamp_ms

    def update(self, candle: Candle) -> Signal | None:
        previous_hour = self._base.last_hour_end_ms
        signal = self._base.update(candle)  # Always consume the underlying event.
        if self._last_ts == candle.ts:
            return None  # The base already verified an identical duplicate.
        if self._base.last_hour_end_ms != previous_hour:
            self._hour_closes.append(candle.close)
        prior = tuple(self._prior)
        self._prior.append(candle)
        self._last_ts = candle.ts
        if signal is None or not self._entry_allowed(signal.ready_at_ms):
            return None
        if len(self._hour_closes) < TREND_FAST_HOURS + SLOPE_HOURS or len(prior) != BREAKOUT_BARS:
            return None
        hours = tuple(self._hour_closes)
        current_average = sum(hours[-TREND_FAST_HOURS:]) / TREND_FAST_HOURS
        earlier_average = sum(hours[:TREND_FAST_HOURS]) / TREND_FAST_HOURS
        slope = current_average - earlier_average
        if not (slope > 0 if signal.side == "long" else slope < 0):
            return None
        # The base candidate always supplies a real ATR; None is reserved for
        # the baseline SMA adapter and cannot authorize this candidate.
        if not isinstance(signal.atr, Decimal) or not signal.atr.is_finite() or signal.atr < 0:
            raise ValueError("Low-turnover candidate requires a valid signal ATR")
        distance = (candle.close - max(item.high for item in prior) if signal.side == "long"
                    else min(item.low for item in prior) - candle.close)
        if distance < BREAKOUT_ATR_BUFFER * signal.atr:
            return None
        net_reward = signal.take_profit_pct - self.round_trip_friction
        net_risk = signal.stop_loss_pct + self.round_trip_friction
        if net_reward <= 0 or net_risk <= 0 or net_reward < MIN_NET_REWARD_RISK * net_risk:
            return None
        return signal
