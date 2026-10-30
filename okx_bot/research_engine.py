"""Offline four-market replay with one wallet, gross exposure and risk latch.

Only completed candles form signals. No credentials, network or exchange writes
are used. Intrabar execution is an OHLC approximation, not an order-book replay.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from statistics import median
from types import SimpleNamespace
from typing import Any

from .backtest_engine import _json_safe, _positive, _validate_candles
from .core import CrossStrategy, position_size, protection_prices
from .history import FUNDING_MAX_GAP_MS, HistoryDataset
from .portfolio_config import PortfolioConfig
from .research_signals import HourlyBreakoutStrategy, Signal


D = Decimal
BAR_MS = 300_000
DAY_MS = 86_400_000
WARMUP_BARS = 720
UTC8 = timezone(timedelta(hours=8))


class _SmaSignals:
    def __init__(self, config: Any) -> None:
        self.config = config
        self.strategy = CrossStrategy(config.fast_window, config.slow_window)

    def update(self, candle: Any) -> Signal | None:
        side = self.strategy.update(candle)
        if side is None:
            return None
        return Signal(side=side, stop_loss_pct=self.config.stop_loss_pct,
                      take_profit_pct=self.config.take_profit_pct, atr=None,
                      ready_at_ms=candle.ts + BAR_MS)


def _make_strategy(strategy_name: str, config: Any) -> Any:
    if strategy_name == "sma_cross":
        return _SmaSignals(config)
    if strategy_name == "trend_breakout_atr":
        return HourlyBreakoutStrategy()
    raise ValueError("Unknown research strategy")


@dataclass
class _Position:
    side: str
    contracts: Decimal
    quantity: Decimal
    entry_price: Decimal
    entry_time: int
    signal_time: int
    stop_loss: Decimal
    take_profit: Decimal
    stop_loss_pct: Decimal
    take_profit_pct: Decimal
    atr: Decimal | None
    entry_fee: Decimal
    margin: Decimal
    funding_pnl: Decimal = D(0)


def _signal_valid(signal: Any, ready_at: int) -> None:
    if signal.side not in {"long", "short"} or type(signal.ready_at_ms) is not int or signal.ready_at_ms != ready_at:
        raise ValueError("Signal must describe this completed candle only")
    _positive(signal.stop_loss_pct, "signal stop_loss_pct")
    _positive(signal.take_profit_pct, "signal take_profit_pct")
    if signal.stop_loss_pct >= 1 or signal.take_profit_pct >= 1:
        raise ValueError("Signal protection fractions must be below 100%")
    if signal.atr is not None:
        _positive(signal.atr, "signal atr")


def _prepare(dataset: HistoryDataset, instrument_id: str, start: int, end: int) -> tuple[list, list, dict]:
    if (not isinstance(dataset, HistoryDataset) or dataset.bar != "5m"
            or dataset.instrument.inst_id != instrument_id
            or type(dataset.start_ms) is not int or type(dataset.end_ms) is not int
            or not dataset.start_ms <= start < end <= dataset.end_ms
            or type(dataset.warmup_bars) is not int or dataset.warmup_bars < WARMUP_BARS):
        raise ValueError("Research requires matching 5m histories with 720 preceding candles")
    instrument = dataset.instrument
    if (instrument.settle_ccy != "USDT" or instrument.ct_val_ccy != instrument_id.split("-")[0]
            or instrument.state != "live"):
        raise ValueError("Research supports active linear USDT contracts only")
    for field in ("ct_val", "lot_size", "min_size", "max_market_size", "tick_size"):
        _positive(getattr(instrument, field), "instrument " + field)
    lower = start - WARMUP_BARS * BAR_MS
    def select(series: list, label: str) -> list:
        # Future OHLC cannot affect a training segment, including validation.
        result = []
        for candle in series:
            if type(getattr(candle, "ts", None)) is not int:
                raise ValueError("History candle timestamps must be integer milliseconds")
            if lower <= candle.ts < end:
                result.append(candle)
        _validate_candles(result, label, BAR_MS)
        if (len(result) != WARMUP_BARS + (end - start) // BAR_MS
                or result[0].ts != lower or result[-1].ts != end - BAR_MS):
            raise ValueError("History does not fully cover research warm-up and measurement")
        return result
    trade, mark = select(dataset.trade_candles, "trade"), select(dataset.mark_candles, "mark")
    if [c.ts for c in trade] != [c.ts for c in mark]:
        raise ValueError("Trade and mark timestamps must match exactly")
    events: dict[int, Decimal] = {}
    previous = None
    preceding = None
    rows = ([dataset.funding_before] if dataset.funding_before is not None else []) + list(dataset.funding_rates)
    for row in rows:
        if not isinstance(row, (tuple, list)) or len(row) != 2 or type(row[0]) is not int:
            raise ValueError("Funding must contain timestamp/rate pairs")
        ts, rate = row
        if ts >= end:
            continue
        if ts < 0 or ts % BAR_MS or (previous is not None and ts <= previous):
            raise ValueError("Funding events must be ordered, unique and 5m aligned")
        if not isinstance(rate, Decimal) or not rate.is_finite() or abs(rate) > 1:
            raise ValueError("Funding rate must be a finite fraction")
        if ts < start:
            preceding = ts
        else:
            if previous is not None and ts - previous > FUNDING_MAX_GAP_MS:
                raise ValueError("Funding coverage has a gap above eight hours")
            events[ts] = rate
        previous = ts
    if preceding is None or start - preceding > FUNDING_MAX_GAP_MS or previous is None or end - previous > FUNDING_MAX_GAP_MS:
        raise ValueError("Funding coverage requires a preceding anchor and no gap above eight hours")
    return trade, mark, events


def run_portfolio_backtest(profile: PortfolioConfig, datasets: dict[str, HistoryDataset], *,
                           starting_equity: Decimal, start_ms: int, end_ms: int,
                           strategy_name: str, fee_multiplier: Decimal = D(1),
                           slippage_multiplier: Decimal = D(1), daily_target: Decimal = D(200)) -> dict:
    """Replay all four instruments on a common clock and a single 1x wallet."""
    if not isinstance(profile, PortfolioConfig) or profile.bar != "5m":
        raise ValueError("A validated four-market 5m profile is required")
    if strategy_name not in {"sma_cross", "trend_breakout_atr"}:
        raise ValueError("Unknown research strategy")
    if (type(start_ms) is not int or type(end_ms) is not int or start_ms < WARMUP_BARS * BAR_MS
            or start_ms >= end_ms or start_ms % BAR_MS or end_ms % BAR_MS):
        raise ValueError("Research interval must be nonempty and aligned to 5m candles")
    _positive(starting_equity, "starting_equity")
    _positive(fee_multiplier, "fee_multiplier", zero=True)
    _positive(slippage_multiplier, "slippage_multiplier", zero=True)
    _positive(daily_target, "daily_target")
    fee_rate, slip_rate = profile.fee_rate * fee_multiplier, profile.slippage_rate * slippage_multiplier
    if fee_rate >= 1 or slip_rate >= 1:
        raise ValueError("Multiplied fee/slippage rates must remain below 100%")
    symbols = profile.instruments
    if not isinstance(datasets, dict) or set(datasets) != set(symbols):
        raise ValueError("Exactly the four configured histories are required")
    configs = {config.instrument: config for config in profile.market_configs()}
    prepared = {symbol: _prepare(datasets[symbol], symbol, start_ms, end_ms) for symbol in symbols}
    strategies = {symbol: _make_strategy(strategy_name, configs[symbol]) for symbol in symbols}
    for symbol in symbols:
        for candle in prepared[symbol][0][:WARMUP_BARS]:
            strategies[symbol].update(candle)

    cash, peak = starting_equity, starting_equity
    max_drawdown = D(0)
    halted, halted_at = False, None
    positions: dict[str, _Position] = {}
    pending: dict[str, Any] = {}
    trades: list[dict] = []
    curve: list[dict] = []
    fees, funding_pnl = D(0), D(0)
    entries, applied_funding = 0, 0
    rotation = 0
    per_market = {symbol: {"entries": 0, "closed_trades": 0, "fees": D(0),
                           "funding_pnl": D(0), "net_pnl": D(0)} for symbol in symbols}
    marks = {symbol: prepared[symbol][1][WARMUP_BARS].open for symbol in symbols}
    daily: dict[str, dict] = {}

    def equity() -> Decimal:
        return cash + sum(((1 if p.side == "long" else -1) * (marks[symbol] - p.entry_price) * p.quantity
                           for symbol, p in positions.items()), D(0))

    def gross_notional() -> Decimal:
        return sum((marks[symbol] * p.quantity for symbol, p in positions.items()), D(0))

    def available() -> Decimal:
        # Do not finance another isolated leg from unrealized gains. Also keep
        # equity sufficient for the gross current 1x mark-price requirement.
        reserved = sum((p.margin for p in positions.values()), D(0))
        return max(D(0), min(cash - reserved, equity() - gross_notional()))

    def sample(ts: int, event: str) -> None:
        nonlocal peak, max_drawdown, halted, halted_at
        value = equity()
        peak = max(peak, value)
        drawdown = (peak - value) / peak
        max_drawdown = max(max_drawdown, drawdown)
        if not halted and drawdown >= profile.max_drawdown_pct:
            halted, halted_at = True, ts
        curve.append({"ts": ts, "equity": value, "event": event,
                      "gross_notional": gross_notional(), "available": available()})

    def close(symbol: str, reference: Decimal, ts: int, reason: str, at_open: bool) -> None:
        nonlocal cash, fees
        p = positions.pop(symbol)
        direction = 1 if p.side == "long" else -1
        fill = reference * (1 - direction * slip_rate)
        gross = direction * (fill - p.entry_price) * p.quantity
        exit_fee = fill * p.quantity * fee_rate
        cash += gross - exit_fee
        fees += exit_fee
        net = gross - p.entry_fee - exit_fee + p.funding_pnl
        per_market[symbol]["closed_trades"] += 1
        per_market[symbol]["fees"] += exit_fee
        per_market[symbol]["net_pnl"] += net
        trades.append({"instrument": symbol, "side": p.side, "contracts": p.contracts,
                       "quantity": p.quantity, "signal_time": p.signal_time,
                       "entry_time": p.entry_time, "exit_time": ts if at_open else ts + BAR_MS,
                       "exit_bar_ts": ts, "exit_time_precision": "open" if at_open else "bar",
                       "entry_price": p.entry_price, "exit_reference_price": reference, "exit_price": fill,
                       "stop_loss": p.stop_loss, "take_profit": p.take_profit,
                       "stop_loss_pct": p.stop_loss_pct, "take_profit_pct": p.take_profit_pct,
                       "atr_at_signal": p.atr, "gross_pnl": gross, "fees": p.entry_fee + exit_fee,
                       "funding_pnl": p.funding_pnl, "net_pnl": net, "reason": reason})

    sample(start_ms, "initial")
    previous_close_equity = starting_equity
    count = (end_ms - start_ms) // BAR_MS
    for offset in range(count):
        index, ts = WARMUP_BARS + offset, start_ms + offset * BAR_MS
        trade = {symbol: prepared[symbol][0][index] for symbol in symbols}
        mark = {symbol: prepared[symbol][1][index] for symbol in symbols}
        marks = {symbol: mark[symbol].open for symbol in symbols}
        for symbol, p in positions.items():
            settlements = prepared[symbol][2]
            if ts in settlements:
                charge = -(1 if p.side == "long" else -1) * marks[symbol] * p.quantity * settlements[ts]
                cash += charge
                p.funding_pnl += charge
                funding_pnl += charge
                per_market[symbol]["funding_pnl"] += charge
                applied_funding += 1
        sample(ts, "open_after_funding")
        # All opening exits release margin before any competing new candidate.
        for symbol in symbols:
            if symbol not in positions:
                continue
            p, price = positions[symbol], mark[symbol].open
            gap_sl = price <= p.stop_loss if p.side == "long" else price >= p.stop_loss
            gap_tp = price >= p.take_profit if p.side == "long" else price <= p.take_profit
            if gap_sl or gap_tp:
                close(symbol, trade[symbol].open, ts, "gap_stop_loss" if gap_sl else "gap_take_profit", True)
        sample(ts, "after_opening_exits")
        if pending:
            order = symbols[rotation:] + symbols[:rotation]
            rotation = (rotation + 1) % len(symbols)
            for symbol in order:
                signal = pending.get(symbol)
                if signal is None or symbol in positions or halted:
                    continue
                if signal.ready_at_ms != ts:
                    raise ValueError("Pending signal must execute at the immediately next candle open")
                instrument = datasets[symbol].instrument
                remaining = profile.max_total_notional - gross_notional()
                budget = min(profile.initial_equity, equity(), available())
                if remaining <= 0 or budget <= 0:
                    continue
                direction = 1 if signal.side == "long" else -1
                sl, tp = protection_prices(signal, signal.side, mark[symbol].open, instrument.tick_size)
                # A known opening trade/mark basis can make the entry-to-stop
                # loss larger than the frozen ATR percentage. Include that
                # distance before the core helper adds fees and adverse slip.
                # Using the raw trade open avoids counting entry slip twice.
                effective_stop = max(signal.stop_loss_pct,
                                     direction * (trade[symbol].open - sl) / trade[symbol].open)
                sizing = SimpleNamespace(stop_loss_pct=effective_stop, fee_rate=fee_rate,
                                         slippage_rate=slip_rate,
                                         max_notional=min(profile.max_notional, remaining),
                                         max_position_fraction=profile.max_position_fraction,
                                         risk_per_trade=profile.risk_per_trade)
                contracts = position_size(sizing, budget, max(trade[symbol].open, mark[symbol].open),
                                          instrument.ct_val, instrument.lot_size, instrument.min_size,
                                          instrument.max_market_size)
                if contracts <= 0:
                    continue
                fill = trade[symbol].open * (1 + direction * slip_rate)
                quantity = contracts * instrument.ct_val
                entry_fee = fill * quantity * fee_rate
                cash -= entry_fee
                fees += entry_fee
                positions[symbol] = _Position(signal.side, contracts, quantity, fill, ts,
                                               signal.ready_at_ms, sl, tp, signal.stop_loss_pct,
                                               signal.take_profit_pct, signal.atr, entry_fee, fill * quantity)
                entries += 1
                per_market[symbol]["entries"] += 1
                per_market[symbol]["fees"] += entry_fee
                sample(ts, "entry")
        pending = {}
        for symbol in symbols:
            if symbol not in positions:
                continue
            p, candle = positions[symbol], mark[symbol]
            hit_sl = candle.low <= p.stop_loss if p.side == "long" else candle.high >= p.stop_loss
            hit_tp = candle.high >= p.take_profit if p.side == "long" else candle.low <= p.take_profit
            if hit_sl or hit_tp:
                trigger = p.stop_loss if hit_sl else p.take_profit
                reference = min(max(trigger, trade[symbol].low), trade[symbol].high)
                close(symbol, reference, ts, "stop_loss" if hit_sl else "take_profit", False)
        marks = {symbol: mark[symbol].close for symbol in symbols}
        sample(ts + BAR_MS, "close")
        for symbol in symbols:
            signal = strategies[symbol].update(trade[symbol])
            if signal is not None:
                _signal_valid(signal, ts + BAR_MS)
                if symbol not in positions and not halted:
                    pending[symbol] = signal
        date = datetime.fromtimestamp(ts / 1000, UTC8).date().isoformat()
        if date not in daily:
            daily[date] = {"date": date, "start_ms": ts, "end_ms": ts + BAR_MS,
                           "start_equity": previous_close_equity, "end_equity": equity()}
        daily[date]["end_ms"], daily[date]["end_equity"] = ts + BAR_MS, equity()
        previous_close_equity = equity()

    open_positions = {}
    for symbol, p in positions.items():
        unrealized = (1 if p.side == "long" else -1) * (marks[symbol] - p.entry_price) * p.quantity
        net = unrealized - p.entry_fee + p.funding_pnl
        open_positions[symbol] = {"instrument": symbol, "side": p.side, "contracts": p.contracts,
                                  "quantity": p.quantity, "signal_time": p.signal_time,
                                  "entry_time": p.entry_time, "entry_price": p.entry_price,
                                  "mark_price": marks[symbol], "stop_loss": p.stop_loss,
                                  "take_profit": p.take_profit, "stop_loss_pct": p.stop_loss_pct,
                                  "take_profit_pct": p.take_profit_pct, "atr_at_signal": p.atr,
                                  "entry_margin": p.margin, "gross_unrealized_pnl": unrealized,
                                  "entry_fee": p.entry_fee, "funding_pnl": p.funding_pnl, "net_pnl": net}
        per_market[symbol]["net_pnl"] += net
    for symbol in symbols:
        per_market[symbol]["open_position"] = open_positions.get(symbol)
    daily_rows = list(daily.values())
    for row in daily_rows:
        day_start = int(datetime.fromisoformat(row["date"]).replace(tzinfo=UTC8).timestamp()) * 1000
        row["complete"] = row["start_ms"] == day_start and row["end_ms"] == day_start + DAY_MS
        row["net_pnl"] = row["end_equity"] - row["start_equity"]
    complete = [row["net_pnl"] for row in daily_rows if row["complete"]]
    target_days = sum(value >= daily_target for value in complete)
    wins = [row["net_pnl"] for row in trades if row["net_pnl"] > 0]
    losses = [-row["net_pnl"] for row in trades if row["net_pnl"] < 0]
    report = {"strategy_name": strategy_name, "instruments": list(symbols), "bar": "5m",
              "warmup_bars": WARMUP_BARS, "start_ms": start_ms, "end_ms": end_ms,
              "initial_equity": starting_equity, "ending_equity": equity(), "ending_cash": cash,
              "ending_available": available(), "ending_gross_notional": gross_notional(),
              "net_pnl": equity() - starting_equity, "return_pct": (equity() / starting_equity - 1) * 100,
              "max_drawdown_pct": max_drawdown * 100, "entries": entries, "closed_trades": len(trades),
              "win_rate_pct": D(len(wins)) / len(trades) * 100 if trades else None,
              "profit_factor": sum(wins, D(0)) / sum(losses, D(0)) if losses else None,
              "fees": fees, "funding_pnl": funding_pnl,
              "funding_events": sum(len(prepared[symbol][2]) for symbol in symbols),
              "applied_funding_events": applied_funding, "halted": halted, "halted_at_ms": halted_at,
              "halt_reason": "session_drawdown" if halted else None, "open_positions": open_positions,
              "per_market": per_market, "trades": trades, "equity_curve": curve,
              "fee_rate": fee_rate, "slippage_rate": slip_rate, "daily_target": daily_target,
              "max_notional": profile.max_notional, "max_total_notional": profile.max_total_notional,
              "daily_pnl": daily_rows,
              "daily_stats": {"complete_days": len(complete), "partial_days": len(daily_rows) - len(complete),
                              "mean_net_pnl": sum(complete, D(0)) / len(complete) if complete else None,
                              "median_net_pnl": median(complete) if complete else None,
                              "loss_days": sum(value < 0 for value in complete), "target_days": target_days,
                              "target_day_ratio_pct": D(target_days) / len(complete) * 100 if complete else None},
              "assumptions": [
                  "四个币种共用一份本金、权益、可用余额、手续费、资金费和回撤闸门；并非四份独立本金回测相加。",
                  "使用720根已收盘5分钟K线预热；预热信号忽略。测量期收盘信号在紧接下一根成交价K线开盘估算成交，ATR止损比例在信号收盘时冻结。",
                  f"基线使用SMA{profile.fast_window}/{profile.slow_window}交叉和固定{profile.stop_loss_pct * 100}%止损、{profile.take_profit_pct * 100}%止盈；候选使用已完成整点小时趋势、5分钟突破和ATR保护，参数事先固定，不按结果寻优。",
                  "候选趋势为已完成小时SMA20/50且小时收盘在SMA50同侧；突破为5分钟收盘首次越过此前20根高低点，ATR14用Wilder平滑，止损为1.5ATR/收盘且至少0.5%，原始比例超过3%则跳过，止盈为止损的2倍。",
                  "每币最多一个1倍逐仓仓位；不加仓、不反手，持仓期间信号忽略。新信号批次轮换四币优先顺序，不补追未执行信号。",
                  "可用余额保守取现金减入场1倍保证金、以及当前权益减标记总名义额的较小值；未实现盈利不用于再次开仓。合约数量按当前规格向下取整。",
                  "开仓同时受每币和共享绝对名义额上限约束，多空不抵消。数量测算使用成交开盘价和标记开盘价较高者及不利滑点；价格变化超限不强制减仓。",
                  "仓位风险测算包含入场时已知的成交价至标记止损距离、手续费和滑点；未来基差变化和跳空仍可能使实际损失超过风险预算。",
                  "同一时点先按标记开盘价结算已有仓位资金费，再处理全部开盘跳空退出，之后才模拟新入场；新仓不承担入场前的该次资金费。",
                  "止盈止损由标记价触发；开盘跳空按成交开盘价估算退出，盘中触发参考价限制在本根成交价高低范围，再叠加不利滑点。同根双触发按止损先发生。",
                  "OHLC无法还原触发时刻基差、盘口和多个市场的盘中先后路径；盘中退出记在该根结束时，释放的余额至下一根才可再使用。",
                  "回撤按四币同步开盘、开盘阶段成交后及同步收盘权益采样，无法表示完整盘中最大回撤；触发后只停止新开，已有止盈止损和资金费继续模拟。",
                  "期末保留仓位，以最后标记收盘价计入未实现盈亏，不人为平仓或预扣未来平仓费；未模拟强平、部分成交、延迟和深度。",
                  "日盈亏按UTC+8自然日权益差计算，包含零交易日；不完整首尾日单列且不计入日目标命中率，命中率仅描述已观察样本，不代表未来概率。",
                  "使用当前合约规格和固定费率、滑点假设；历史规格、费率及实际成交条件可能不同。"
              ]}
    return _json_safe(report)
