"""Frozen candidate signals for offline research, never live execution.

The candidate combines completed hourly SMA20/50 trend, a fresh prior-20-bar
5m Donchian breakout, and ATR14 Wilder/RMA stops. Parameters are declared in
advance; this module contains no fitting, order submission, or profitability
claim. Callers must supply contiguous, confirmed 5m candles, then execute any
signal no earlier than its ``ready_at_ms`` (the following candle's open).
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from decimal import Decimal

from .okx import Candle


D = Decimal
BAR_MS = 300_000
HOUR_MS = 3_600_000
RESEARCH_WARMUP_BARS = 720
TREND_FAST_HOURS = 20
TREND_SLOW_HOURS = 50
BREAKOUT_BARS = 20
ATR_BARS = 14
MIN_STOP_LOSS_PCT = D("0.005")
MAX_STOP_LOSS_PCT = D("0.03")
ATR_MULTIPLE = D("1.5")
REWARD_RISK = D("2")


def _finite(value: Decimal, *, positive: bool = True) -> bool:
    return (isinstance(value, Decimal) and value.is_finite()
            and (value > 0 if positive else value >= 0)
            and value <= D("1e30"))


@dataclass(frozen=True)
class Signal:
    side: str
    stop_loss_pct: Decimal
    take_profit_pct: Decimal
    atr: Decimal | None
    ready_at_ms: int

    def __post_init__(self) -> None:
        if (type(self.side) is not str or self.side not in {"long", "short"}
                or not _finite(self.stop_loss_pct) or self.stop_loss_pct >= 1
                or not _finite(self.take_profit_pct) or self.take_profit_pct >= 1
                or (self.atr is not None and not _finite(self.atr, positive=False))
                or type(self.ready_at_ms) is not int or self.ready_at_ms < 0
                or self.ready_at_ms % BAR_MS):
            raise ValueError("Invalid research signal")


class HourlyBreakoutStrategy:
    """Research-only, close-confirmed hourly trend and fresh 5m breakout.

    Only complete aligned groups of twelve 5m candles enter the hourly SMAs.
    An initial partial hour is discarded. ATR seeds from fourteen true ranges
    (requiring fifteen candles), then uses Wilder's ``(13*ATR + TR)/14``.

    A fresh breakout is a false-to-true transition of the breakout predicate,
    each evaluated against that candle's own preceding twenty highs/lows.
    Consecutive new highs/lows therefore cannot generate repeated signals.
    A rejected event is consumed: a later trend change or lower volatility
    cannot revive it. Stops above the volatility ceiling are skipped, never
    clipped to a smaller, misleading risk distance.
    """

    def __init__(self, *, min_stop_loss_pct: Decimal = MIN_STOP_LOSS_PCT,
                 max_stop_loss_pct: Decimal = MAX_STOP_LOSS_PCT,
                 atr_multiple: Decimal = ATR_MULTIPLE,
                 reward_risk: Decimal = REWARD_RISK):
        if (any(not _finite(value) for value in
                (min_stop_loss_pct, max_stop_loss_pct, atr_multiple, reward_risk))
                or not min_stop_loss_pct <= max_stop_loss_pct < 1
                or max_stop_loss_pct * reward_risk >= 1):
            raise ValueError("Invalid research strategy parameters")
        self.min_stop_loss_pct = min_stop_loss_pct
        self.max_stop_loss_pct = max_stop_loss_pct
        self.atr_multiple = atr_multiple
        self.reward_risk = reward_risk
        self._prior: deque[Candle] = deque(maxlen=BREAKOUT_BARS)
        self._hour_bars: list[Candle] = []
        self._hour_closes: deque[Decimal] = deque(maxlen=TREND_SLOW_HOURS)
        self._hour_bucket: int | None = None
        self._last_hour_end_ms: int | None = None
        self._last: Candle | None = None
        self._seed_ranges: list[Decimal] = []
        self._atr: Decimal | None = None
        self._was_long: bool | None = None
        self._was_short: bool | None = None

    @property
    def atr(self) -> Decimal | None:
        return self._atr

    @property
    def last_hour_end_ms(self) -> int | None:
        return self._last_hour_end_ms

    @property
    def trend(self) -> str | None:
        if len(self._hour_closes) < TREND_SLOW_HOURS:
            return None
        closes = tuple(self._hour_closes)
        fast = sum(closes[-TREND_FAST_HOURS:]) / TREND_FAST_HOURS
        slow = sum(closes) / TREND_SLOW_HOURS
        if fast > slow and closes[-1] > slow:
            return "long"
        if fast < slow and closes[-1] < slow:
            return "short"
        return None

    @staticmethod
    def _validate(candle: Candle) -> None:
        if (not isinstance(candle, Candle) or type(candle.ts) is not int
                or not 0 <= candle.ts <= 32_503_680_000_000 or candle.ts % BAR_MS
                or any(not _finite(value) for value in
                       (candle.open, candle.high, candle.low, candle.close))
                or any(not -30 <= value.as_tuple().exponent <= 30 for value in
                       (candle.open, candle.high, candle.low, candle.close))
                or candle.high < max(candle.open, candle.low, candle.close)
                or candle.low > min(candle.open, candle.high, candle.close)):
            raise ValueError("Research strategy requires valid aligned closed 5m OHLC candles")

    def update(self, candle: Candle) -> Signal | None:
        self._validate(candle)
        if self._last is not None:
            if candle.ts == self._last.ts and candle == self._last:
                return None
            if candle.ts != self._last.ts + BAR_MS:
                raise ValueError("Research candles must be contiguous and strictly ordered")

        bucket = candle.ts // HOUR_MS * HOUR_MS
        if bucket != self._hour_bucket:
            self._hour_bucket = bucket
            self._hour_bars = []
        self._hour_bars.append(candle)
        if (len(self._hour_bars) == 12 and self._hour_bars[0].ts == bucket
                and candle.ts + BAR_MS == bucket + HOUR_MS):
            self._hour_closes.append(candle.close)
            self._last_hour_end_ms = bucket + HOUR_MS

        if self._last is not None:
            true_range = max(candle.high - candle.low,
                             abs(candle.high - self._last.close),
                             abs(candle.low - self._last.close))
            if self._atr is None:
                self._seed_ranges.append(true_range)
                if len(self._seed_ranges) == ATR_BARS:
                    self._atr = sum(self._seed_ranges) / ATR_BARS
                    self._seed_ranges.clear()
            else:
                self._atr = (self._atr * (ATR_BARS - 1) + true_range) / ATR_BARS

        fresh_long = fresh_short = False
        if len(self._prior) == BREAKOUT_BARS:
            is_long = candle.close > max(bar.high for bar in self._prior)
            is_short = candle.close < min(bar.low for bar in self._prior)
            fresh_long = is_long and self._was_long is False
            fresh_short = is_short and self._was_short is False
            self._was_long, self._was_short = is_long, is_short
        else:
            self._was_long = self._was_short = None
        self._prior.append(candle)
        self._last = candle

        side = "long" if fresh_long else "short" if fresh_short else None
        if side is None or self.trend != side or self._atr is None:
            return None
        raw_stop = self.atr_multiple * self._atr / candle.close
        if raw_stop > self.max_stop_loss_pct:
            return None
        stop = max(raw_stop, self.min_stop_loss_pct)
        return Signal(side, stop, self.reward_risk * stop, self._atr, candle.ts + BAR_MS)
