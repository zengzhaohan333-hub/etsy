"""Strict, non-secret settings for the four-market 5m demo portfolio.

All markets share one account and one aggregate notional cap. Per-market Config
objects carry strategy/risk settings; enforcing aggregate exposure belongs to
the portfolio execution coordinator.
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, fields
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .config import Config


DEFAULT_INSTRUMENTS = ("BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP", "XRP-USDT-SWAP")
MAX_CONFIG_BYTES = 64 * 1024


@dataclass(frozen=True)
class PortfolioConfig:
    instruments: tuple[str, ...] = DEFAULT_INSTRUMENTS
    bar: str = "5m"
    fast_window: int = 5
    slow_window: int = 20
    initial_equity: Decimal = Decimal("10000")
    risk_per_trade: Decimal = Decimal("0.005")
    max_position_fraction: Decimal = Decimal("0.10")
    max_notional: Decimal = Decimal("200")
    max_total_notional: Decimal = Decimal("800")
    stop_loss_pct: Decimal = Decimal("0.01")
    take_profit_pct: Decimal = Decimal("0.02")
    max_drawdown_pct: Decimal = Decimal("0.05")
    fee_rate: Decimal = Decimal("0.0005")
    slippage_rate: Decimal = Decimal("0.0002")

    def __post_init__(self) -> None:
        if (type(self.instruments) is not tuple or len(self.instruments) != 4
                or any(type(value) is not str for value in self.instruments)
                or len(set(self.instruments)) != 4 or set(self.instruments) != set(DEFAULT_INSTRUMENTS)):
            raise ValueError("组合必须完整包含 BTC、ETH、SOL、XRP 四个 USDT 永续合约，且不得重复或加入其他标的")
        if type(self.bar) is not str or self.bar != "5m":
            raise ValueError("四币种模拟组合的 bar 必须是 5m")
        if not isinstance(self.max_total_notional, Decimal) or not self.max_total_notional.is_finite():
            raise ValueError("max_total_notional 必须是有限十进制数")
        if self.max_total_notional <= 0:
            raise ValueError("max_total_notional 必须大于零")
        # Reuse every existing strategy, numeric, finite-value and range check.
        self.market_configs()
        if self.max_notional > self.max_total_notional:
            raise ValueError("每币 max_notional 不得超过共享 max_total_notional")

    def market_configs(self) -> tuple[Config, ...]:
        """Return separate validated market configs in the declared order."""
        shared = {field.name: getattr(self, field.name)
                  for field in fields(Config) if field.name != "instrument"}
        return tuple(Config(instrument=instrument, **shared) for instrument in self.instruments)


def load_portfolio_config(path: Path) -> PortfolioConfig:
    """Load known TOML fields only; decimal inputs become exact Decimal values."""
    with Path(path).open("rb") as stream:
        raw = stream.read(MAX_CONFIG_BYTES + 1)
    if len(raw) > MAX_CONFIG_BYTES:
        raise ValueError("组合配置文件超过 64 KiB")
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeError):
        raise ValueError("组合配置不是有效的 UTF-8 TOML") from None
    known = {field.name: field for field in fields(PortfolioConfig)}
    if data.keys() - known.keys():
        raise ValueError("存在未知组合配置项；仅支持已声明的策略、风控和四币种设置")
    for name, value in data.items():
        if isinstance(known[name].default, Decimal):
            if type(value) not in (str, int, float):
                raise ValueError(f"{name} 必须是十进制数，不能使用布尔值、数组或表")
            try:
                data[name] = Decimal(str(value))
            except (InvalidOperation, ValueError):
                raise ValueError(f"{name} 必须是十进制数") from None
        elif name == "instruments":
            if type(value) is not list or any(type(item) is not str for item in value):
                raise ValueError("instruments 必须是四个合约名称组成的数组")
            data[name] = tuple(value)
        elif name == "bar" and type(value) is not str:
            raise ValueError("bar 必须是字符串 5m")
    return PortfolioConfig(**data)
