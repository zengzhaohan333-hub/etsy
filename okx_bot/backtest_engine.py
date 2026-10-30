"""Historical 15-minute replay of the demo runner's one-position strategy.

No network, credentials or exchange mutation is performed here. Trade candles
form signals and approximate fills; mark candles trigger protection and value
positions. Supplied funding events are settled before same-time new entries.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from .config import Config
from .core import CrossStrategy, position_size, protection_prices
from .okx import Candle, Instrument


D = Decimal
BAR_MS = 15 * 60_000


@dataclass
class _Position:
    side: str
    contracts: Decimal
    quantity: Decimal
    entry_price: Decimal
    entry_time: int
    stop_loss: Decimal
    take_profit: Decimal
    entry_fee: Decimal
    funding_pnl: Decimal = D(0)


def _positive(value: Any, label: str, *, zero: bool = False) -> Decimal:
    if (not isinstance(value, Decimal) or not value.is_finite()
            or value < 0 or (not zero and value == 0)):
        raise ValueError(f"{label} must be a finite {'nonnegative' if zero else 'positive'} Decimal")
    return value


def _validate_candles(candles: list[Candle], label: str) -> None:
    if not candles:
        raise ValueError(f"{label} candles are empty")
    previous = None
    for candle in candles:
        if not isinstance(candle, Candle) or type(candle.ts) is not int or candle.ts < 0 or candle.ts % BAR_MS:
            raise ValueError(f"{label} timestamps must be nonnegative, 15-minute-aligned integers")
        if previous is not None and candle.ts - previous != BAR_MS:
            raise ValueError(f"{label} candles must be strictly ordered and complete, without duplicates or gaps")
        for field in ("open", "high", "low", "close"):
            _positive(getattr(candle, field), f"{label} {field}")
        if candle.high < max(candle.open, candle.low, candle.close) or candle.low > min(candle.open, candle.high, candle.close):
            raise ValueError(f"{label} candle OHLC values are inconsistent")
        previous = candle.ts


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value


def run_backtest(config: Config, trade_candles: list[Candle], mark_candles: list[Candle],
                 funding_rates: list[tuple[int, Decimal]], instrument: Instrument, *,
                 starting_equity: Decimal, start_ms: int, end_ms: int,
                 fee_multiplier: Decimal = D(1), slippage_multiplier: Decimal = D(1)) -> dict:
    """Replay [start_ms, end_ms), keeping the preceding bars only for warm-up.

    A crossing on a measured closed candle can execute only at the next bar's
    open. Crossings while holding are ignored, including one just before a gap
    exit. Drawdown stops entries, never forcibly closes an exchange position.
    """
    if config.bar != "15m":
        raise ValueError("Historical replay currently supports 15m bars only")
    if (type(start_ms) is not int or type(end_ms) is not int or start_ms < 0
            or start_ms >= end_ms or start_ms % BAR_MS or end_ms % BAR_MS):
        raise ValueError("Backtest bounds must define a nonempty, 15-minute-aligned interval")
    _positive(starting_equity, "starting_equity")
    _positive(fee_multiplier, "fee_multiplier", zero=True)
    _positive(slippage_multiplier, "slippage_multiplier", zero=True)
    fee_rate = config.fee_rate * fee_multiplier
    slip_rate = config.slippage_rate * slippage_multiplier
    if fee_rate >= 1 or slip_rate >= 1:
        raise ValueError("Multiplied fee/slippage rates must remain below 100%")
    if (instrument.inst_id != config.instrument or instrument.settle_ccy != "USDT"
            or instrument.ct_val_ccy != config.instrument.split("-")[0] or instrument.state != "live"):
        raise ValueError("Instrument must match the active linear USDT contract")
    for field in ("ct_val", "lot_size", "min_size", "max_market_size", "tick_size"):
        _positive(getattr(instrument, field), f"instrument {field}")
    # A caller may pass the complete dataset to several chronological segments.
    # Read only timestamps outside this segment; future OHLC must not affect a
    # training segment, even through validation. Older history is not preloaded.
    lower = start_ms - (config.slow_window + 1) * BAR_MS
    def selected(candles: list[Candle], label: str) -> list[Candle]:
        result = []
        for candle in candles:
            if not isinstance(candle, Candle) or type(candle.ts) is not int:
                raise ValueError(f"{label} candles require integer timestamps")
            if lower <= candle.ts < end_ms:
                result.append(candle)
        return result
    trade_candles = selected(trade_candles, "trade")
    mark_candles = selected(mark_candles, "mark")
    _validate_candles(trade_candles, "trade")
    _validate_candles(mark_candles, "mark")
    if [c.ts for c in trade_candles] != [c.ts for c in mark_candles]:
        raise ValueError("Trade and mark candles must have exactly matching timestamps")
    warmup = [c for c in trade_candles if c.ts < start_ms]
    measured = [index for index, candle in enumerate(trade_candles) if start_ms <= candle.ts < end_ms]
    if len(warmup) < config.slow_window + 1:
        raise ValueError("At least slow_window + 1 preceding candles are required for warm-up")
    if (len(measured) != (end_ms - start_ms) // BAR_MS
            or trade_candles[measured[0]].ts != start_ms
            or trade_candles[measured[-1]].ts != end_ms - BAR_MS):
        raise ValueError("Trade and mark candles do not completely cover the measured interval")
    settlements: dict[int, Decimal] = {}
    previous_funding = None
    for row in funding_rates:
        if not isinstance(row, (tuple, list)) or len(row) != 2:
            raise ValueError("Funding records must be timestamp/rate pairs")
        ts, rate = row
        if type(ts) is not int or ts < 0 or ts % BAR_MS:
            raise ValueError("Funding settlement timestamps must be 15-minute aligned")
        if previous_funding is not None and ts <= previous_funding:
            raise ValueError("Funding events must be strictly ordered without duplicates")
        if not isinstance(rate, Decimal) or not rate.is_finite():
            raise ValueError("Funding rates must be finite Decimals")
        if start_ms <= ts < end_ms:
            settlements[ts] = rate
        previous_funding = ts

    # Only the sizing helper reads these fields. Retain its exact implementation
    # for stress costs too, without changing the user's frozen Config object.
    sizing = SimpleNamespace(
        stop_loss_pct=config.stop_loss_pct, fee_rate=fee_rate, slippage_rate=slip_rate,
        max_notional=config.max_notional, max_position_fraction=config.max_position_fraction,
        risk_per_trade=config.risk_per_trade)
    strategy = CrossStrategy(config.fast_window, config.slow_window)
    for candle in warmup:
        strategy.update(candle)  # Alignment and crossings here never place orders.
    cash = starting_equity
    peak = starting_equity
    max_drawdown = D(0)
    fees = D(0)
    funding_pnl = D(0)
    applied_funding = 0
    position: _Position | None = None
    pending: str | None = None
    halted = False
    halted_at = None
    entries = 0
    trades: list[dict] = []
    curve: list[dict] = []

    def equity(mark: Decimal) -> Decimal:
        if position is None:
            return cash
        direction = 1 if position.side == "long" else -1
        return cash + direction * (mark - position.entry_price) * position.quantity

    def sample(ts: int, mark: Decimal, event: str) -> None:
        nonlocal peak, max_drawdown, halted, halted_at
        value = equity(mark)
        peak = max(peak, value)
        drawdown = (peak - value) / peak
        max_drawdown = max(max_drawdown, drawdown)
        if not halted and drawdown >= config.max_drawdown_pct:
            halted, halted_at = True, ts
        curve.append({"ts": ts, "equity": value, "event": event})

    def close(reference: Decimal, bar_ts: int, reason: str, *, at_open: bool) -> None:
        nonlocal cash, fees, position
        assert position is not None
        p = position
        direction = 1 if p.side == "long" else -1
        fill = reference * (1 - direction * slip_rate)
        gross = direction * (fill - p.entry_price) * p.quantity
        exit_fee = fill * p.quantity * fee_rate
        cash += gross - exit_fee
        fees += exit_fee
        timestamp = bar_ts if at_open else bar_ts + BAR_MS
        trades.append({
            "side": p.side, "contracts": p.contracts, "quantity": p.quantity,
            "entry_time": p.entry_time, "exit_time": timestamp, "exit_bar_ts": bar_ts,
            "exit_time_precision": "open" if at_open else "bar",
            "entry_price": p.entry_price, "exit_price": fill,
            "stop_loss": p.stop_loss, "take_profit": p.take_profit,
            "gross_pnl": gross, "fees": p.entry_fee + exit_fee,
            "funding_pnl": p.funding_pnl,
            "net_pnl": gross - p.entry_fee - exit_fee + p.funding_pnl,
            "reason": reason})
        position = None
        sample(timestamp, reference, "exit")

    sample(start_ms, mark_candles[measured[0]].open, "initial")
    for index in measured:
        trade, mark = trade_candles[index], mark_candles[index]
        # Existing holders pay/receive the settlement; a new position opened at
        # this timestamp does not inherit a settlement from before its fill.
        if position is not None and trade.ts in settlements:
            direction = 1 if position.side == "long" else -1
            charge = -direction * mark.open * position.quantity * settlements[trade.ts]
            cash += charge
            funding_pnl += charge
            position.funding_pnl += charge
            applied_funding += 1
        sample(trade.ts, mark.open, "open_after_funding")
        if position is not None:
            p = position
            gap_sl = mark.open <= p.stop_loss if p.side == "long" else mark.open >= p.stop_loss
            gap_tp = mark.open >= p.take_profit if p.side == "long" else mark.open <= p.take_profit
            if gap_sl or gap_tp:
                close(trade.open, trade.ts, "gap_stop_loss" if gap_sl else "gap_take_profit", at_open=True)
        if pending is not None and position is None and not halted:
            side = pending
            # Use the higher visible opening price to keep the 1x/notional caps
            # conservative when trade and mark prices have a basis.
            budget = min(config.initial_equity, cash)
            contracts = position_size(sizing, budget, max(trade.open, mark.open), instrument.ct_val,
                                      instrument.lot_size, instrument.min_size, instrument.max_market_size)
            if contracts > 0:
                direction = 1 if side == "long" else -1
                fill = trade.open * (1 + direction * slip_rate)
                quantity = contracts * instrument.ct_val
                entry_fee = fill * quantity * fee_rate
                sl, tp = protection_prices(config, side, mark.open, instrument.tick_size)
                cash -= entry_fee
                fees += entry_fee
                position = _Position(side, contracts, quantity, fill, trade.ts, sl, tp, entry_fee)
                entries += 1
                sample(trade.ts, mark.open, "entry")
        pending = None
        if position is not None:
            p = position
            hit_sl = mark.low <= p.stop_loss if p.side == "long" else mark.high >= p.stop_loss
            hit_tp = mark.high >= p.take_profit if p.side == "long" else mark.low <= p.take_profit
            if hit_sl or hit_tp:
                # No intrabar path is available. Adverse-first resolves a bar
                # that touches both brackets without manufacturing extra data.
                trigger = p.stop_loss if hit_sl else p.take_profit
                reference = min(max(trigger, trade.low), trade.high)
                close(reference, trade.ts,
                      "stop_loss" if hit_sl else "take_profit", at_open=False)
        sample(trade.ts + BAR_MS, mark.close, "close")
        signal = strategy.update(trade)
        if position is None and not halted:
            pending = signal

    last_mark = mark_candles[measured[-1]].close
    ending_equity = equity(last_mark)
    open_position = None
    if position is not None:
        p = position
        direction = 1 if p.side == "long" else -1
        unrealized = direction * (last_mark - p.entry_price) * p.quantity
        open_position = {
            "side": p.side, "contracts": p.contracts, "quantity": p.quantity,
            "entry_time": p.entry_time, "entry_price": p.entry_price,
            "mark_price": last_mark, "stop_loss": p.stop_loss, "take_profit": p.take_profit,
            "gross_unrealized_pnl": unrealized, "entry_fee": p.entry_fee,
            "funding_pnl": p.funding_pnl,
            "net_pnl": unrealized - p.entry_fee + p.funding_pnl}
    wins = sum(1 for trade in trades if trade["net_pnl"] > 0)
    gross_wins = sum((trade["net_pnl"] for trade in trades if trade["net_pnl"] > 0), D(0))
    gross_losses = -sum((trade["net_pnl"] for trade in trades if trade["net_pnl"] < 0), D(0))
    report = {
        "start_ms": start_ms, "end_ms": end_ms, "initial_equity": starting_equity,
        "ending_equity": ending_equity, "ending_cash": cash,
        "net_pnl": ending_equity - starting_equity,
        "return_pct": (ending_equity / starting_equity - 1) * 100,
        "max_drawdown_pct": max_drawdown * 100,
        "entries": entries, "closed_trades": len(trades),
        "win_rate_pct": D(wins) / len(trades) * 100 if trades else None,
        "profit_factor": gross_wins / gross_losses if gross_losses else None,
        "fees": fees, "funding_pnl": funding_pnl,
        "funding_events": len(settlements), "applied_funding_events": applied_funding,
        "halted": halted, "halted_at_ms": halted_at,
        "halt_reason": "session_drawdown" if halted else None,
        "open_position": open_position, "trades": trades, "equity_curve": curve,
        "fee_rate": fee_rate, "slippage_rate": slip_rate,
        "assumptions": [
            "使用已收盘的15分钟成交价K线计算均线；预热期信号不下单，测量期的新信号在下一根K线开盘估算成交。",
            "最多持有一个1倍逐仓仓位；持仓期间忽略交叉信号，不反手或加仓。",
            "入场和跳空退出采用成交价K线开盘价，再叠加不利滑点。",
            "止盈止损由标记价格触发；盘中退出的参考价限制在该根成交价K线高低区间内，再叠加不利滑点。触发时刻的成交价与盘口未知，因此只是近似。",
            "同根标记价格K线同时触达止盈和止损时，按止损先发生处理；盘中退出时间以该根K线结束时间估计。",
            "仓位测算采用开盘成交价与标记价中较高者，并受现金、配置本金上限和当前合约数量粒度约束。",
            "资金费按提供的结算事件和结算时标记开盘价名义额计算；同一时点先结算已有仓位，再模拟新入场。",
            "回撤在K线开收盘和模拟成交事件采样；触发阈值后停止新开仓，已有止盈止损和资金费继续模拟。",
            "没有完整模拟强平、部分成交、延迟和市场深度；风险预算和止损不保证最大亏损。",
            "期末保留未平仓仓位，按最后标记收盘价估值，不人为平仓，也不预扣尚未发生的平仓手续费。",
            "使用当前合约规格与固定手续费、滑点假设，可能不同于历史实际交易条件。"
        ]}
    return _json_safe(report)
