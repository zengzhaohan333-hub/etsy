"""Strict, non-secret TOML configuration."""
from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, fields
from decimal import Decimal, InvalidOperation
from pathlib import Path


@dataclass(frozen=True)
class Config:
    instrument: str = "BTC-USDT-SWAP"
    bar: str = "15m"
    fast_window: int = 5
    slow_window: int = 20
    initial_equity: Decimal = Decimal("10000")
    risk_per_trade: Decimal = Decimal("0.005")
    max_position_fraction: Decimal = Decimal("0.10")
    max_notional: Decimal = Decimal("200")
    stop_loss_pct: Decimal = Decimal("0.01")
    take_profit_pct: Decimal = Decimal("0.02")
    max_drawdown_pct: Decimal = Decimal("0.05")
    fee_rate: Decimal = Decimal("0.0005")
    slippage_rate: Decimal = Decimal("0.0002")

    def __post_init__(self):
        if not re.fullmatch(r"[A-Z0-9]{2,20}-USDT-SWAP", self.instrument):
            raise ValueError("instrument 必须是 USDT 永续合约，例如 BTC-USDT-SWAP")
        if self.bar not in {"1m", "3m", "5m", "15m", "30m", "1H", "2H", "4H"}:
            raise ValueError("bar 支持 1m、3m、5m、15m、30m、1H、2H、4H")
        if (type(self.fast_window) is not int or type(self.slow_window) is not int
                or not 2 <= self.fast_window < self.slow_window <= 200):
            raise ValueError("均线窗口要求 2 <= fast_window < slow_window <= 200")
        for f in fields(self):
            value = getattr(self, f.name)
            if isinstance(f.default, Decimal):
                if not isinstance(value, Decimal) or not value.is_finite():
                    raise ValueError(f"{f.name} 必须是有限十进制数")
        for name in ("initial_equity", "max_notional"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} 必须大于零")
        for name, cap in (("risk_per_trade", "0.02"), ("max_position_fraction", "1"),
                          ("stop_loss_pct", "0.5"), ("take_profit_pct", "0.9"),
                          ("max_drawdown_pct", "0.5")):
            if not 0 < getattr(self, name) <= Decimal(cap):
                raise ValueError(f"{name} 必须大于零且不超过 {cap}")
        for name in ("fee_rate", "slippage_rate"):
            if not 0 <= getattr(self, name) <= Decimal("0.01"):
                raise ValueError(f"{name} 必须在 0 到 0.01 之间")


def load_config(path: Path) -> Config:
    with path.open("rb") as stream:
        data = tomllib.load(stream)
    known = {f.name: f for f in fields(Config)}
    unknown = data.keys() - known.keys()
    if unknown:
        raise ValueError("未知配置项: " + ", ".join(sorted(unknown)))
    for key, value in data.items():
        if isinstance(known[key].default, Decimal):
            if isinstance(value, bool):
                raise ValueError(f"{key} 不能是布尔值")
            try:
                data[key] = Decimal(str(value))
            except InvalidOperation:
                raise ValueError(f"{key} 必须是十进制数") from None
        elif key in {"instrument", "bar"} and not isinstance(value, str):
            raise ValueError(f"{key} 必须是字符串")
    return Config(**data)
