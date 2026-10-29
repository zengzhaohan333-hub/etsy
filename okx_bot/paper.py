"""Deterministic, deliberately synthetic paper replay; no network or credentials."""
from __future__ import annotations

import math
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from .config import Config
from .core import CrossStrategy, bar_milliseconds, position_size, protection_prices
from .okx import Candle
from .state import StateStore, exclusive_lock

D = Decimal


@dataclass
class Position:
    side: str
    quantity: Decimal  # base coin quantity, NOT contracts
    entry_price: Decimal
    stop_loss: Decimal
    take_profit: Decimal
    entry_fee: Decimal
    entry_time: int


def sample_candles(count: int, bar: str = "15m") -> list[Candle]:
    """Manufactured BTC-like prices solely for exercising the application."""
    candles = []
    previous = D("60000")
    for index in range(count):
        close = D(str(round(60000 + 1800 * math.sin(index / 13) + 550 * math.sin(index / 4), 2)))
        spread = D("140")
        candles.append(Candle(1_735_689_600_000 + index * bar_milliseconds(bar),
                              previous, max(previous, close) + spread,
                              min(previous, close) - spread, close))
        previous = close
    return candles


class PaperEngine:
    def __init__(self, config: Config, store: StateStore | None = None):
        self.config, self.store = config, store
        self.cash = config.initial_equity
        self.peak = self.cash
        self.position: Position | None = None
        self.strategy = CrossStrategy(config.fast_window, config.slow_window)
        self.pending: str | None = None
        self.halted = False
        self.halt_reason = ""
        self.fees = D(0)
        self.trades: list[dict] = []
        self.events: list[dict] = []
        self.candles: list[dict] = []
        self.last_ts: int | None = None
        self.price = D(0)
        self.processed = 0
        self.run_id = uuid.uuid4().hex

    def equity(self, price: Decimal) -> Decimal:
        if not self.position:
            return self.cash
        p = self.position
        sign = 1 if p.side == "long" else -1
        return self.cash + sign * (price - p.entry_price) * p.quantity

    def _event(self, ts: int, message: str):
        self.events.append({"time": ts, "message": message})
        self.events = self.events[-100:]

    def _fill(self, price: Decimal, buy: bool) -> Decimal:
        return price * (1 + self.config.slippage_rate * (1 if buy else -1))

    def open(self, side: str, price: Decimal, ts: int):
        if self.position or self.halted:
            return
        fill = self._fill(price, side == "long")
        quantity = position_size(self.config, self.cash, fill, D(1), D("0.00001"), D("0.00001"), D("100000"))
        if quantity == 0:
            self._event(ts, "资金或交易粒度不足，跳过开仓")
            return
        sl, tp = protection_prices(self.config, side, fill, D("0.01"))
        fee = quantity * fill * self.config.fee_rate
        self.cash -= fee
        self.fees += fee
        self.position = Position(side, quantity, fill, sl, tp, fee, ts)
        self._event(ts, "模拟开多" if side == "long" else "模拟开空")

    def close(self, price: Decimal, ts: int, reason: str):
        if not self.position:
            return
        p = self.position
        fill = self._fill(price, p.side == "short")
        gross = (fill - p.entry_price) * p.quantity * (1 if p.side == "long" else -1)
        fee = fill * p.quantity * self.config.fee_rate
        self.cash += gross - fee
        self.fees += fee
        trade = {"time": ts, "entry_time": p.entry_time, "side": p.side,
                 "quantity": str(p.quantity), "entry_price": str(p.entry_price),
                 "exit_price": str(fill), "pnl": str(gross - p.entry_fee - fee),
                 "fees": str(p.entry_fee + fee), "reason": reason}
        self.trades.append(trade)
        if self.store:
            self.store.audit({"run_id": self.run_id, **trade})
        self.position = None
        self._event(ts, f"模拟平仓：{reason}")

    def _guard_drawdown(self, price: Decimal, ts: int):
        equity = self.equity(price)
        self.peak = max(self.peak, equity)
        if (self.peak - equity) / self.peak >= self.config.max_drawdown_pct:
            self.close(price, ts, "max_drawdown")
            self.halted, self.halt_reason = True, "本次运行达到权益回撤上限"
            self.pending = None
            self._event(ts, self.halt_reason)

    def process(self, c: Candle):
        if self.last_ts is not None and c.ts <= self.last_ts:
            return  # repeated polls must not execute duplicate signals
        if self.halted:
            return
        if any(not p.is_finite() or p <= 0 for p in (c.open, c.high, c.low, c.close)):
            raise ValueError("行情价格无效")
        if c.high < max(c.open, c.close, c.low) or c.low > min(c.open, c.close, c.high):
            raise ValueError("行情 OHLC 无效")
        if self.last_ts is not None and c.ts - self.last_ts != bar_milliseconds(self.config.bar):
            raise ValueError("行情不连续，停止处理以免跨过保护价")
        self.price, self.last_ts = c.close, c.ts
        # Only opening gaps precede strategy orders; do not look ahead to the
        # bar's full range when a reversal should already execute at the open.
        p = self.position
        bracket_exit = False
        if p:
            gap_sl = c.open <= p.stop_loss if p.side == "long" else c.open >= p.stop_loss
            gap_tp = c.open >= p.take_profit if p.side == "long" else c.open <= p.take_profit
            if gap_sl:
                self.close(c.open, c.ts, "stop_loss")
                bracket_exit = True
            elif gap_tp:
                self.close(p.take_profit, c.ts, "take_profit")
                bracket_exit = True
        self._guard_drawdown(c.open, c.ts)
        # Signals observed on the PREVIOUS closed bar execute at this bar's open.
        if self.pending and not bracket_exit:
            if self.position and self.position.side != self.pending:
                self.close(c.open, c.ts, "signal_reversal")
                self._guard_drawdown(c.open, c.ts)
            if not self.position:
                self.open(self.pending, c.open, c.ts)
        p = self.position
        if p:
            hit_sl = c.low <= p.stop_loss if p.side == "long" else c.high >= p.stop_loss
            hit_tp = c.high >= p.take_profit if p.side == "long" else c.low <= p.take_profit
            if hit_sl:  # unknown intrabar path: assume stop precedes take-profit
                self.close(p.stop_loss, c.ts, "stop_loss")
            elif hit_tp:
                self.close(p.take_profit, c.ts, "take_profit")
        self.pending = self.strategy.update(c)
        self.processed += 1
        self.candles.append({"time": c.ts, "close": str(c.close)})
        self.candles = self.candles[-120:]
        self._guard_drawdown(c.close, c.ts)
        if self.halted:
            self.pending = None

    def finish(self, reason: str):
        if self.last_ts is not None:
            self.close(self.price, self.last_ts, reason)
        self.pending = None

    def snapshot(self) -> dict:
        equity = self.equity(self.price)
        drawdown = max(D(0), (self.peak - equity) / self.peak * 100)
        return {"mode": "paper", "feed": "synthetic", "run_id": self.run_id,
                "instrument": self.config.instrument, "bar": self.config.bar,
                "equity": str(equity), "cash": str(self.cash), "price": str(self.price),
                "position": asdict(self.position) if self.position else None,
                "trades": self.trades[-100:], "total_trades": len(self.trades),
                "candles": self.candles, "events": self.events,
                "halted": self.halted, "halt_reason": self.halt_reason,
                "drawdown_pct": str(drawdown), "fees": str(self.fees),
                "processed_bars": self.processed,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "config": {k: str(v) for k, v in asdict(self.config).items()}}


def run_paper(config: Config, state_dir: Path, bars: int = 320, interval: float = 0,
              stop: threading.Event | None = None) -> dict:
    if config.instrument != "BTC-USDT-SWAP":
        raise ValueError("合成演示行情仅支持 BTC-USDT-SWAP；不会将 BTC 演示价格套用到其他币种")
    if not config.slow_window + 2 <= bars <= 100_000 or not 0 <= interval <= 60:
        raise ValueError("bars 需要覆盖慢均线且不超过 100000；interval 须在 0 到 60 秒之间")
    stop = stop or threading.Event()
    store = StateStore(state_dir)
    with exclusive_lock(state_dir / "paper.lock"):
        engine = PaperEngine(config, store)
        engine._event(0, "本地模拟启动：合成演示行情，每次运行重置虚拟余额")
        for candle in sample_candles(bars, config.bar):
            if stop.is_set() or engine.halted:
                break
            engine.process(candle)
            store.save(engine.snapshot())
            if stop.wait(interval):
                break
        engine.finish("user_stop" if stop.is_set() else "replay_end")
        engine._event(engine.last_ts or 0, "本次模拟结束，模拟持仓已结算")
        result = engine.snapshot()
        store.save(result)
        return result
