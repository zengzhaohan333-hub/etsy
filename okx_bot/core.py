"""Shared closed-candle signal and conservative contract sizing."""
from __future__ import annotations

from collections import deque
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

from .config import Config
from .okx import Candle

D = Decimal


def bar_milliseconds(bar: str) -> int:
    return int(bar[:-1]) * (60_000 if bar[-1] == "m" else 3_600_000)


class CrossStrategy:
    """Emit only fresh SMA crossings, never an initial alignment signal."""

    def __init__(self, fast: int, slow: int):
        if not 2 <= fast < slow <= 200:
            raise ValueError("Invalid strategy windows")
        self.fast, self.slow = fast, slow
        self.closes: deque[Decimal] = deque(maxlen=slow)
        self.previous: Decimal | None = None
        self.last_ts: int | None = None

    def update(self, candle: Candle) -> str | None:
        if self.last_ts is not None and candle.ts <= self.last_ts:
            return None
        if not candle.close.is_finite() or candle.close <= 0:
            raise ValueError("Invalid candle close")
        self.last_ts = candle.ts
        self.closes.append(candle.close)
        if len(self.closes) < self.slow:
            return None
        values = list(self.closes)
        difference = sum(values[-self.fast:]) / self.fast - sum(values) / self.slow
        previous, self.previous = self.previous, difference
        if previous is not None:
            if previous <= 0 < difference:
                return "long"
            if previous >= 0 > difference:
                return "short"
        return None


def position_size(config: Config, equity: Decimal, price: Decimal,
                  unit_size: Decimal, step: Decimal, min_size: Decimal,
                  max_size: Decimal) -> Decimal:
    """Return contracts rounded DOWN. unit_size is base coins per contract.

    Includes estimated round-trip fees and slippage in the stop risk budget.
    This budget is a sizing assumption, not a maximum loss guarantee.
    """
    if any(not value.is_finite() or value <= 0 for value in
           (equity, price, unit_size, step, min_size, max_size)):
        return D(0)
    stop, slip, fee = config.stop_loss_pct, config.slippage_rate, config.fee_rate
    # Bound the adverse short-side exit: both slippage and exit fees apply to
    # the increased stop notional. The simpler stop+2*fee underestimates this.
    adverse_move = stop + 2 * slip + stop * slip
    loss_fraction = adverse_move + fee * (2 + stop + 2 * slip + stop * slip)
    notional = min(config.max_notional, equity * config.max_position_fraction,
                   equity * config.risk_per_trade / loss_fraction,
                   equity / (1 + config.fee_rate))
    size = (min(notional / (price * unit_size * (1 + slip)), max_size) / step).to_integral_value(rounding=ROUND_FLOOR) * step
    return size if size >= min_size else D(0)


def protection_prices(config: Config, side: str, price: Decimal,
                      tick: Decimal) -> tuple[Decimal, Decimal]:
    if side not in {"long", "short"} or price <= 0 or tick <= 0:
        raise ValueError("Invalid protection price input")
    direction = 1 if side == "long" else -1
    # Round the stop towards entry, avoiding risk expansion by a price tick.
    rounding = ROUND_CEILING if side == "long" else ROUND_FLOOR
    stop = ((price * (1 - direction * config.stop_loss_pct)) / tick).to_integral_value(rounding=rounding) * tick
    take = ((price * (1 + direction * config.take_profit_pct)) / tick).to_integral_value(rounding=rounding) * tick
    if not ((0 < stop < price < take) if side == "long" else (0 < take < price < stop)):
        raise ValueError("Price tick is too large for the configured stop/take profit")
    return stop, take
