"""One demo account coordinator for four independent 5m market strategies.

One key-wide lock, durable journal and account drawdown are shared. Exchange
positions and their native protective orders survive a coordinator stop; no
order is automatically resent or adopted after an uncertain result.
"""
from __future__ import annotations

import hashlib
import threading
import time
import uuid
from contextlib import ExitStack
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path
from typing import Any

from .config import Config
from .core import CrossStrategy, position_size, protection_prices
from .demo import (DemoHalt, _algos, _load, _lock, _number, _position_summary,
                   _positions, _save, _verify_fill, _verify_position)
from .demo import _verify_bracket
from .okx import Credentials, Instrument, OkxClient, OkxError, _safe_tls_failure
from .portfolio_config import DEFAULT_INSTRUMENTS, PortfolioConfig
from .read_retry import ReadRetryClient, ReadUnavailable, is_retryable_read_error
from .session import PortfolioSession, SessionError, profile_fingerprint


D = Decimal
INTERVAL_MS = 300_000
SIGNAL_MAX_AGE_MS = 120_000


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _configs(profile: PortfolioConfig) -> dict[str, Config]:
    configs = {config.instrument: config for config in profile.market_configs()}
    if set(configs) != set(DEFAULT_INSTRUMENTS) or any(config.bar != "5m" for config in configs.values()):
        raise DemoHalt("Portfolio requires BTC, ETH, SOL and XRP USDT swaps on 5m bars")
    return configs


def _journal_universe(journal: dict) -> None:
    if any(row.get("instrument") not in DEFAULT_INSTRUMENTS for row in journal["orders"]):
        raise DemoHalt("Journal contains an unknown instrument; manual inspection required")


def _bars(candles: list, minimum: int) -> None:
    if len(candles) < minimum:
        raise DemoHalt("Not enough closed candles to initialize portfolio strategy")
    for index, candle in enumerate(candles):
        if type(candle.ts) is not int or candle.ts < 0 or candle.ts % INTERVAL_MS:
            raise DemoHalt("Portfolio candle timestamps must align to 5m")
        if index and candle.ts - candles[index - 1].ts != INTERVAL_MS:
            raise DemoHalt("Portfolio candles are duplicated, out of order or have a gap")
        prices = (candle.open, candle.high, candle.low, candle.close)
        if any(not isinstance(value, Decimal) or not value.is_finite() or value <= 0 for value in prices):
            raise DemoHalt("Invalid portfolio candle price")
        if candle.high < max(prices) or candle.low > min(prices):
            raise DemoHalt("Invalid portfolio candle OHLC")
    if not -5_000 <= _now_ms() - (candles[-1].ts + INTERVAL_MS) <= INTERVAL_MS + 30_000:
        raise DemoHalt("Portfolio market data is stale or in the future")


def _fresh(timestamp: int, *, age: int = 30_000) -> bool:
    return type(timestamp) is int and -5_000 <= _now_ms() - timestamp <= age


@dataclass
class _Market:
    config: Config
    metadata: Instrument
    strategy: CrossStrategy
    last: Any
    entry: dict | None = None


class _RecoveryStopped(Exception):
    """A deliberate stop while paused; leave protected records unchanged."""


def _resume_entries(journal: dict) -> dict[str, dict]:
    result = {}
    for row in journal["orders"]:
        if row.get("status") == "closed":
            continue
        if row.get("status") != "protected":
            raise DemoHalt("Uncertain journal entries are quarantined; explicit reconciliation is required")
        inst = row.get("instrument")
        if inst in result or row.get("side") not in {"long", "short"}:
            raise DemoHalt("Protected journal ownership is invalid; manual inspection required")
        if not all(type(row.get(name)) is str and row[name] for name in ("client_id", "order_id")):
            raise DemoHalt("Protected journal lacks original order identity; manual inspection required")
        for name in ("size", "stop_loss", "take_profit"):
            if _number(row.get(name), name) <= 0:
                raise DemoHalt("Protected journal sizing or protection is invalid")
        result[inst] = row
    return result


def _original_order(client: Any, inst: str, entry: dict, *, holding: bool) -> dict:
    order = client.query_order(inst, entry["client_id"])
    if (not isinstance(order, dict) or order.get("instId") != inst
            or order.get("clOrdId") != entry["client_id"]
            or str(order.get("ordId", "")) != entry["order_id"]):
        raise DemoHalt("Original protected order identity could not be verified")
    size = _number(entry["size"], "original size")
    filled = _number(order.get("accFillSz"), "original filled size")
    if holding:
        if order.get("state") != "filled" or filled != size:
            raise DemoHalt("Protected position lacks the exact original fully filled order")
    elif order.get("state") not in {"filled", "canceled", "mmp_canceled"} or not 0 <= filled <= size:
        raise DemoHalt("Original protected order is not verified terminal")
    return dict(order)


def run_portfolio(profile: PortfolioConfig, state_dir: Path, stop: threading.Event,
                  poll_seconds: float = 15, client: Any | None = None,
                  max_cycles: int | None = None, *, resilient: bool = False,
                  resume: bool = False) -> dict:
    """Run four strategies in one serialized, demo-only account control loop."""
    report: dict[str, Any] = {
        "mode": "demo", "scope": "portfolio", "instruments": list(DEFAULT_INSTRUMENTS),
        "bar": "5m", "phase": "starting", "halted": False, "halt_reason": "",
        "entries": 0, "position": None, "skipped_signals": 0,
        "gross_notional": "0", "markets": {},
        "connection": {"state": "checking", "attempt": 0, "max_attempts": 3},
        "note": "Stopping does not close positions. Verify every market's exchange positions and TP/SL before restarting."}
    markets: dict[str, _Market] = {}
    journal: dict | None = None
    journal_path: Path | None = None
    status_path: Path | None = None
    writable = False
    current_intent: dict | None = None
    peak = D(0)
    session = None
    recovery_started = None
    recovery_round = 0
    snapshot_mode = resilient or resume
    suppress_next_signals = False

    def publish() -> None:
        report["updated_at_ms"] = _now_ms()
        if writable and status_path is not None:
            _save(status_path, report)

    def retrying(event: dict) -> None:
        report["connection"] = {**event, "state": "recovering"} if recovery_started is not None else event
        publish()

    def recovered() -> None:
        report["connection"] = {"state": "recovering" if recovery_started is not None else "checking", "attempt": 0, "max_attempts": 3}
        publish()

    def account_balance(*, update_report: bool = True) -> tuple[Decimal, Decimal, int]:
        nonlocal peak
        checked = _now_ms()
        balances = client.balances()
        if balances.get("currency") != "USDT":
            raise DemoHalt("Portfolio balance currency must be USDT")
        equity = _number(balances.get("equity"), "equity")
        available = _number(balances.get("available"), "available")
        if equity < 0 or available < 0:
            raise DemoHalt("Invalid portfolio account balance")
        if session is not None:
            session.observe(equity, profile.max_drawdown_pct)
            peak = session.peak
            report["risk_halted"] = session.risk_halted
        else:
            peak = max(peak, equity)
        if peak <= 0:
            raise DemoHalt("Portfolio account has no positive USDT equity")
        drawdown = (peak - equity) / peak
        if update_report:
            report.update(equity=str(equity), available=str(available), peak_equity=str(peak),
                          drawdown_pct=str(drawdown * 100), equity_checked_at_ms=checked)
        if (session is not None and session.risk_halted) or drawdown >= profile.max_drawdown_pct:
            raise DemoHalt("Portfolio session drawdown limit reached; all new entries stopped; existing positions remain open")
        return equity, available, checked

    def update_position(inst: str, *, protection: bool) -> tuple[list, int]:
        market = markets[inst]
        state = report["markets"][inst]
        checked = _now_ms()
        rows = _positions(client, inst)
        state["position"] = _position_summary(rows)
        state["position_known"] = True
        pending = client.pending_orders(inst)
        if pending:
            raise DemoHalt("Unexpected ordinary order in portfolio market; inspect required")
        if market.entry is not None:
            if rows:
                _verify_position(rows, market.entry, inst)
                if protection:
                    _verify_bracket(client, inst, market.entry)
            else:
                if _algos(client, inst):
                    raise DemoHalt("Portfolio position is flat but protective algos remain; inspect required")
                market.entry["status"] = "closed"
                market.entry["closed_at_ms"] = _now_ms()
                _save(journal_path, journal)
                market.entry = None
                state["notional"] = "0"
        elif rows:
            raise DemoHalt("Unexpected external portfolio position; inspect required")
        elif protection and _algos(client, inst):
            raise DemoHalt("Unexpected external portfolio algo order; inspect required")
        state["phase"] = "holding" if rows else "waiting"
        return rows, checked

    def exposure(target: str) -> tuple[Decimal, Decimal, int, list[int]]:
        """Fresh held-position notional, never requested-order notional."""
        total = D(0)
        candidate_price = None
        candidate_timestamp = None
        checked_times = []
        for inst in markets:
            rows, checked = update_position(inst, protection=False)
            checked_times.append(checked)
            state = report["markets"][inst]
            if rows or inst == target:
                mark, timestamp = client.mark_price(inst)
                if not isinstance(mark, Decimal) or not mark.is_finite() or mark <= 0 or not _fresh(timestamp):
                    raise DemoHalt("Portfolio mark price is stale or invalid")
                checked_times.append(timestamp)
                state["mark_price"] = str(mark)
                state["mark_timestamp"] = timestamp
                if inst == target:
                    candidate_price, candidate_timestamp = mark, timestamp
                notional = sum((abs(_number(row["pos"], "pos")) * markets[inst].metadata.ct_val * mark
                                for row in rows), D(0))
                state["notional"] = str(notional)
                total += notional
            else:
                state["notional"] = "0"
        # The candidate must be flat and have no pre-existing algorithmic entry
        # or protective order. All other markets were checked in this cycle.
        if markets[target].entry is not None or report["markets"][target]["position"] is not None:
            raise DemoHalt("Portfolio entry candidate is no longer flat")
        if _algos(client, target):
            raise DemoHalt("Unexpected algo on portfolio entry candidate")
        if snapshot_mode:
            checked_times.extend(client.snapshot_read_times)
        report["gross_notional"] = str(total)
        return total, candidate_price, candidate_timestamp, checked_times

    def skip(inst: str, reason: str) -> None:
        report["skipped_signals"] += 1
        state = report["markets"][inst]
        state["skipped_signals"] += 1
        state["last_event"] = reason

    def refresh_snapshot() -> None:
        if snapshot_mode:
            client.refresh()

    def pause_read(error: ReadUnavailable) -> None:
        nonlocal recovery_started, recovery_round
        if stop.is_set():
            raise _RecoveryStopped()
        if recovery_started is None:
            recovery_started = time.monotonic()
            report["recovery_started_at_ms"] = _now_ms()
        recovery_round += 1
        delay = (15, 30, 60)[min(recovery_round - 1, 2)]
        details = {"category": error.category, "api_code": error.api_code,
                   "http_status": error.http_status}
        tls = _safe_tls_failure(getattr(error, "tls_failure", None), error.category)
        if tls is not None:
            details["tls_failure"] = tls
        elapsed = max(0, int(time.monotonic() - recovery_started))
        report["phase"] = "recovering"
        report["connection"] = {"state": "recovering", "attempt": 0, "max_attempts": 3, **details}
        report["recovery"] = {"round": recovery_round, "delay_seconds": delay,
                              "started_at_ms": report["recovery_started_at_ms"],
                              "elapsed_seconds": elapsed, "warning": elapsed >= 600,
                              "last_error": details}
        for state in report["markets"].values():
            state["phase"] = "recovering"
        publish()
        if stop.wait(delay) or stop.is_set():
            raise _RecoveryStopped()

    def read_checkpoint(operation):
        nonlocal recovery_started, recovery_round
        while True:
            if stop.is_set():
                raise _RecoveryStopped()
            try:
                result = operation()
                if recovery_started is not None:
                    # Startup resumes initialization, not live trading. Leave
                    # recovering before any subsequent one-time leverage POST;
                    # connected still requires the complete normal cycle.
                    recovery_started, recovery_round = None, 0
                    report.update(phase="starting", recovery=None,
                                  recoveries=report.get("recoveries", 0) + 1,
                                  last_recovered_at_ms=_now_ms())
                    report["connection"] = {"state": "checking", "attempt": 0, "max_attempts": 3}
                    publish()
                return result
            except ReadUnavailable as error:
                if not resilient or current_intent is not None:
                    raise
                pause_read(error)

    def healthy_snapshot() -> None:
        """Stage all read facts before committing any ownership transition."""
        report["recovery_checked_markets"] = 0
        client.begin_cycle(60)
        refresh_snapshot()
        account = client.account_config()
        if account.get("posMode") != "net_mode" or str(account.get("acctLv")) != "2":
            raise DemoHalt("Portfolio account mode changed during recovery")
        if abs(client.server_time() - _now_ms()) > 5_000:
            raise DemoHalt("Local/exchange clock drift exceeds five seconds")
        staged = {}
        closing = []
        originals = {}
        gross = D(0)
        for inst, market in markets.items():
            client.begin_cycle(60)
            rows = _positions(client, inst)
            if client.pending_orders(inst):
                raise DemoHalt("Unexpected ordinary order during portfolio recovery")
            if market.entry is None:
                if rows or _algos(client, inst):
                    raise DemoHalt("Unowned position or algo during portfolio recovery")
            else:
                original = _original_order(client, inst, market.entry, holding=bool(rows))
                originals[inst] = original
                if rows:
                    _verify_position(rows, market.entry, inst)
                    average = _number(original.get("avgPx"), "original average fill price")
                    if average <= 0 or _number(rows[0].get("avgPx"), "position average price") != average:
                        raise DemoHalt("Protected position average differs from the original filled order")
                    _verify_bracket(client, inst, market.entry)
                elif _algos(client, inst):
                    raise DemoHalt("Flat recovered position still has protective algos")
                else:
                    closing.append(inst)
            leverage = client.get_leverage(inst)
            if not leverage or any(row.get("instId") != inst or row.get("mgnMode") != "isolated"
                                   or _number(row.get("lever"), "leverage") != 1 for row in leverage):
                raise DemoHalt("Recovery cannot verify isolated 1x leverage")
            candles = client.candles(inst, "5m", min(300, max(100, market.config.slow_window + 2)))
            _bars(candles, market.config.slow_window + 1)
            matched = [c for c in candles if c.ts == market.last.ts]
            if matched and matched[0] != market.last:
                raise DemoHalt("Previously closed recovery candle changed")
            if candles[-1].ts < market.last.ts:
                raise DemoHalt("Recovered portfolio data moved backwards")
            strategy = CrossStrategy(market.config.fast_window, market.config.slow_window)
            for candle in candles:
                strategy.update(candle)  # Every outage/history signal is discarded.
            state = dict(report["markets"][inst])
            state.update(position=_position_summary(rows), position_known=True,
                         phase="holding" if rows else "waiting", last_candle_ts=candles[-1].ts,
                         last_success_at_ms=_now_ms(), notional="0")
            if rows:
                mark, mark_ts = client.mark_price(inst)
                if not mark.is_finite() or mark <= 0 or not _fresh(mark_ts):
                    raise DemoHalt("Recovery mark price is stale or invalid")
                notional = sum((abs(_number(row["pos"], "pos")) * market.metadata.ct_val * mark for row in rows), D(0))
                gross += notional
                state.update(mark_price=str(mark), mark_timestamp=mark_ts, notional=str(notional),
                             protection_checked_at_ms=_now_ms())
            staged[inst] = (strategy, candles[-1], state)
            report["recovery_checked_markets"] = len(staged)
            publish()
        # Candle/order lookups can take longer than the private freshness limit.
        # Re-read the entire account view and revalidate ownership using stable,
        # already verified terminal parent-order facts before committing it.
        client.begin_cycle(60)
        refresh_snapshot()
        closing = []
        gross = D(0)
        for inst, market in markets.items():
            rows = _positions(client, inst)
            state = staged[inst][2]
            if client.pending_orders(inst):
                raise DemoHalt("Unexpected ordinary order in final recovery verification")
            if market.entry is None:
                if rows or _algos(client, inst):
                    raise DemoHalt("Unowned final recovery position or algo")
            elif rows:
                _verify_position(rows, market.entry, inst)
                original = originals[inst]
                average = _number(original.get("avgPx"), "original average fill price")
                if (original.get("state") != "filled"
                        or _number(original.get("accFillSz"), "original filled size") != D(market.entry["size"])
                        or average <= 0 or _number(rows[0].get("avgPx"), "position average price") != average):
                    raise DemoHalt("Final recovered position differs from original filled order")
                _verify_bracket(client, inst, market.entry)
            elif _algos(client, inst):
                raise DemoHalt("Final flat recovery position still has protective algos")
            else:
                closing.append(inst)
            state.update(position=_position_summary(rows), position_known=True,
                         phase="holding" if rows else "waiting", notional="0")
            if rows:
                mark, mark_ts = client.mark_price(inst)
                if not mark.is_finite() or mark <= 0 or not _fresh(mark_ts):
                    raise DemoHalt("Final recovery mark price is stale or invalid")
                value = sum((abs(_number(row["pos"], "pos")) * market.metadata.ct_val * mark for row in rows), D(0))
                gross += value
                state.update(mark_price=str(mark), mark_timestamp=mark_ts, notional=str(value))
        client.begin_cycle(60)
        equity, available, checked = account_balance(update_report=False)
        source_times = client.snapshot_read_times if snapshot_mode else (_now_ms(),)
        held_mark_times = tuple(state["mark_timestamp"] for _strategy, _last, state in staged.values()
                                if state["position"] is not None)
        if not all(_fresh(timestamp) for timestamp in (*source_times, checked, *held_mark_times)):
            raise ReadUnavailable(read_method="swap_positions", category="transport", budget_exhausted=True)
        for _strategy, _last, state in staged.values():
            state["last_success_at_ms"] = min(source_times)
            if state["position"] is not None:
                state["protection_checked_at_ms"] = min(source_times)
        if stop.is_set():
            raise _RecoveryStopped()
        # No local entry is closed unless every market and the final account
        # snapshot passed. There are no exchange writes anywhere in this pass.
        if closing:
            for inst in closing:
                markets[inst].entry.update(status="closed", closed_at_ms=_now_ms())
            _save(journal_path, journal)
        for inst, (strategy, last, state) in staged.items():
            markets[inst].strategy, markets[inst].last = strategy, last
            if inst in closing:
                markets[inst].entry = None
            report["markets"][inst] = state
        report.update(equity=str(equity), available=str(available), equity_checked_at_ms=checked,
                      peak_equity=str(peak), drawdown_pct=str((peak - equity) / peak * 100),
                      gross_notional=str(gross))

    def recover_health(error: ReadUnavailable) -> None:
        nonlocal recovery_started, recovery_round, suppress_next_signals
        while True:
            pause_read(error)
            try:
                healthy_snapshot()
            except ReadUnavailable as next_error:
                error = next_error
                continue
            recovery_started, recovery_round = None, 0
            suppress_next_signals = True
            report.update(phase="running", recovery=None, recoveries=report.get("recoveries", 0) + 1,
                          last_recovered_at_ms=_now_ms(), last_success_at_ms=_now_ms())
            report["connection"] = {"state": "connected", "attempt": 0, "max_attempts": 3}
            publish()
            return

    try:
        configs = _configs(profile)
        if not 0 < poll_seconds <= 60:
            raise DemoHalt("Polling interval must be greater than 0 and at most 60 seconds")
        if max_cycles is not None and (type(max_cycles) is not int or max_cycles < 0):
            raise DemoHalt("max_cycles must be a nonnegative integer")
        report.update(instruments=list(configs), max_total_notional=str(profile.max_total_notional),
                      max_notional_per_market=str(profile.max_notional))
        for inst in configs:
            report["markets"][inst] = {
                "instrument": inst, "phase": "starting", "position": None, "position_known": False,
                "entries": 0, "last_success_at_ms": None, "last_candle_ts": None,
                "skipped_signals": 0, "mark_price": None, "notional": None}
        client = client or OkxClient(Credentials.from_env())
        key = getattr(getattr(client, "credentials", None), "api_key", "")
        if not key:
            raise DemoHalt("Portfolio client requires an API key identity for concurrency protection")
        state_dir = Path(state_dir)
        state_dir.mkdir(parents=True, exist_ok=True)
        journal_path = state_dir / "demo-journal.json"
        status_path = state_dir / "status.json"
        fingerprint = hashlib.sha256(key.encode()).hexdigest()
        with ExitStack() as locks:
            locks.enter_context(_lock(Path("/tmp") / ("okx-demo-" + fingerprint + ".lock")))
            locks.enter_context(_lock(state_dir / "demo.lock"))
            writable = True
            journal = _load(journal_path, allow_unresolved=resume)
            _journal_universe(journal)
            adopted = _resume_entries(journal) if resume else {}
            publish()
            if stop.is_set():
                report["phase"] = "stopped"
                publish()
                return report
            client = ReadRetryClient(client, stop, on_retry=retrying, on_recovered=recovered, classify_exhausted=resilient)
            if snapshot_mode:
                from .snapshot import AccountSnapshotClient
                client = AccountSnapshotClient(client)
            def startup_account():
                client.begin_cycle(60)
                account = client.account_config()
                if account.get("posMode") != "net_mode" or str(account.get("acctLv")) != "2":
                    raise DemoHalt("Portfolio demo account must use net mode and single-currency margin")
                if abs(client.server_time() - _now_ms()) > 5_000:
                    raise DemoHalt("Local/exchange clock drift exceeds five seconds")
                equity, available, _checked = account_balance()
                if equity <= 0 or available <= 0:
                    raise DemoHalt("Portfolio demo account has no available USDT equity")
                return equity
            initial_equity = read_checkpoint(startup_account)
            if snapshot_mode:
                session = PortfolioSession.load_or_create(state_dir, fingerprint,
                    profile_fingerprint(profile), initial_equity)
                latched = session.observe(initial_equity, profile.max_drawdown_pct)
                peak = session.peak
                report.update(peak_equity=str(peak), risk_halted=latched,
                              drawdown_pct=str((peak - initial_equity) / peak * 100))
                if latched:
                    raise SessionError("Persistent session drawdown stop is latched; manual review is required")
                read_checkpoint(lambda: (client.begin_cycle(60), refresh_snapshot()))
            # Every market must pass read-only preflight before any leverage POST.
            for inst, config in configs.items():
                def preflight_market(inst=inst, config=config):
                    client.begin_cycle(60)
                    metadata = client.instrument(inst)
                    if (metadata.inst_id != inst or metadata.settle_ccy != "USDT" or metadata.state != "live"
                            or metadata.ct_val_ccy != inst.split("-")[0]):
                        raise DemoHalt("Unsupported portfolio instrument metadata")
                    history = client.candles(inst, "5m", min(300, max(100, config.slow_window + 2)))
                    _bars(history, config.slow_window + 1)
                    strategy = CrossStrategy(config.fast_window, config.slow_window)
                    for candle in history:
                        strategy.update(candle)
                    markets[inst] = _Market(config, metadata, strategy, history[-1], adopted.get(inst))
                    if not snapshot_mode:
                        update_position(inst, protection=True)
                    report["markets"][inst]["last_candle_ts"] = history[-1].ts
                    report["markets"][inst]["last_success_at_ms"] = _now_ms()
                    publish()
                read_checkpoint(preflight_market)
            if snapshot_mode:
                # Restarts only verify existing settings/ownership. They never
                # repeat a leverage write or add missing protective orders.
                read_checkpoint(healthy_snapshot)
                report["adopted_positions"] = sum(m.entry is not None for m in markets.values())
            else:
                for inst in markets:
                    if stop.is_set():
                        break
                    client.begin_cycle(60)
                    client.set_leverage(inst, "1")  # A mutation is never retried.
                    def verify_leverage(inst=inst):
                        client.begin_cycle(60)
                        leverage = client.get_leverage(inst)
                        if not leverage or any(row.get("instId") != inst or row.get("mgnMode") != "isolated"
                                               or _number(row.get("lever"), "leverage") != 1 for row in leverage):
                            raise DemoHalt("Unable to verify portfolio isolated 1x leverage")
                    read_checkpoint(verify_leverage)
                    report["markets"][inst]["leverage_verified"] = True
                    publish()
            cycles = 0
            batch_offset = 0
            def one_cycle() -> bool:
                nonlocal current_intent, batch_offset, recovery_started, recovery_round, suppress_next_signals
                client.begin_cycle(60)
                refresh_snapshot()
                account_balance()
                candidates = []
                for inst, market in markets.items():
                    client.begin_cycle(60)
                    rows, _position_checked = update_position(inst, protection=True)
                    state = report["markets"][inst]
                    if rows:
                        mark, mark_timestamp = client.mark_price(inst)
                        if not isinstance(mark, Decimal) or not mark.is_finite() or mark <= 0 or not _fresh(mark_timestamp):
                            raise DemoHalt("Portfolio mark price is stale or invalid")
                        state["mark_price"] = str(mark)
                        state["mark_timestamp"] = mark_timestamp
                        state["notional"] = str(sum((abs(_number(row["pos"], "pos")) * market.metadata.ct_val * mark
                                                     for row in rows), D(0)))
                    else:
                        state["notional"] = "0"
                    candles = client.candles(inst, "5m", min(300, max(100, market.config.slow_window + 2)))
                    _bars(candles, market.config.slow_window + 1)
                    previous = [c for c in candles if c.ts == market.last.ts]
                    if previous and previous[0] != market.last:
                        raise DemoHalt("Previously closed portfolio candle changed")
                    new = [c for c in candles if c.ts > market.last.ts]
                    if not new and candles[-1].ts < market.last.ts:
                        raise DemoHalt("Portfolio market data moved backwards")
                    if new:
                        if new[0].ts - market.last.ts != INTERVAL_MS:
                            raise DemoHalt("Portfolio missed history exceeds available candle continuity")
                        for candle in new:
                            signal = market.strategy.update(candle)
                            market.last = candle
                            if signal is not None and market.entry is None:
                                if signal not in {"long", "short"}:
                                    raise DemoHalt("Unsupported portfolio strategy signal")
                                if len(new) == 1 and not suppress_next_signals:
                                    candidates.append((inst, signal, candle.ts + INTERVAL_MS))
                                else:
                                    skip(inst, "historical_signal_skipped")
                    state["last_candle_ts"] = market.last.ts
                    state["last_success_at_ms"] = _now_ms()
                    if snapshot_mode:
                        sources = client.snapshot_read_times
                        verified_at = min(sources)
                        if rows:
                            verified_at = min(verified_at, state["mark_timestamp"])
                            state["protection_checked_at_ms"] = verified_at
                        state["last_success_at_ms"] = verified_at
                report["gross_notional"] = str(sum((D(state["notional"]) for state in report["markets"].values()), D(0)))
                if candidates:
                    order = list(markets)
                    candidates.sort(key=lambda candidate: (order.index(candidate[0]) - batch_offset) % len(order))
                    # Advance only when a new crossing batch exists. Polling
                    # 20 times per 5m bar must not accidentally fix priority.
                    batch_offset = (batch_offset + 1) % len(order)
                for inst, side, signal_time in candidates:
                    if stop.is_set():
                        break
                    client.begin_cycle(60)
                    refresh_snapshot()
                    equity, available, balance_checked = account_balance()
                    held, mark, mark_ts, position_times = exposure(inst)
                    if stop.is_set():
                        break
                    def fresh_entry() -> bool:
                        return (_fresh(signal_time, age=SIGNAL_MAX_AGE_MS) and _fresh(mark_ts)
                                and _fresh(balance_checked) and all(_fresh(ts) for ts in position_times))
                    if not fresh_entry():
                        skip(inst, "stale_signal_skipped")
                        continue
                    remaining = profile.max_total_notional - held
                    if remaining <= 0:
                        skip(inst, "portfolio_cap_reached")
                        continue
                    config = replace(markets[inst].config,
                                     max_notional=min(markets[inst].config.max_notional, remaining))
                    metadata = markets[inst].metadata
                    quantity = position_size(config, min(profile.initial_equity, equity, available), mark,
                                             metadata.ct_val, metadata.lot_size,
                                             metadata.min_size, metadata.max_market_size)
                    if quantity <= 0:
                        skip(inst, "order_below_minimum")
                        continue
                    sl, tp = protection_prices(config, side, mark, metadata.tick_size)
                    current_intent = {
                        "client_id": "p" + uuid.uuid4().hex[:23], "instrument": inst,
                        "side": side, "size": str(quantity), "stop_loss": str(sl),
                        "take_profit": str(tp), "status": "intent", "created_at_ms": _now_ms()}
                    journal["orders"].append(current_intent)
                    markets[inst].entry = current_intent
                    _save(journal_path, journal)
                    if stop.is_set() or not fresh_entry():
                        current_intent.update(status="closed", never_submitted=True, closed_at_ms=_now_ms())
                        _save(journal_path, journal)
                        markets[inst].entry = None
                        current_intent = None
                        if stop.is_set():
                            break
                        skip(inst, "stale_signal_skipped")
                        continue
                    acknowledgement = client.place_market_order(inst, "buy" if side == "long" else "sell",
                                                               quantity, current_intent["client_id"],
                                                               stop_loss=sl, take_profit=tp)
                    current_intent.update(status="accepted", order_id=str(acknowledgement["ordId"]))
                    _save(journal_path, journal)
                    client.begin_cycle(60)
                    fill_checked_at = _now_ms()
                    _verify_fill(client, config, current_intent, stop, poll_seconds)
                    current_intent["status"] = "protected"
                    _save(journal_path, journal)
                    report["entries"] += 1
                    state = report["markets"][inst]
                    state["entries"] += 1
                    state["position"] = _position_summary(_positions(client, inst))
                    state["position_known"] = True
                    state["phase"] = "holding"
                    state["last_success_at_ms"] = fill_checked_at if snapshot_mode else _now_ms()
                    if snapshot_mode:
                        state["protection_checked_at_ms"] = fill_checked_at
                    # This is the fresh pre-submit mark applied to confirmed
                    # filled contracts, not an unfilled requested-order budget.
                    confirmed = D(current_intent["size"]) * metadata.ct_val * mark
                    state["mark_price"] = str(mark)
                    state["notional"] = str(confirmed)
                    report["gross_notional"] = str(held + confirmed)
                    current_intent = None
                    publish()
                if stop.is_set():
                    return False
                # Four per-market read budgets can span much longer than one
                # account snapshot's freshness. Refresh shared equity after the
                # complete batch instead of stamping the old balance as new.
                client.begin_cycle(60)
                account_balance()
                if recovery_started is not None:
                    recovery_started, recovery_round = None, 0
                    report.update(recovery=None, last_recovered_at_ms=_now_ms(),
                                  recoveries=report.get("recoveries", 0) + 1)
                report["phase"] = "running"
                report["last_success_at_ms"] = _now_ms()
                report["connection"] = {"state": "connected", "attempt": 0, "max_attempts": 3}
                suppress_next_signals = False
                publish()

                return True

            while not stop.is_set() and (max_cycles is None or cycles < max_cycles):
                try:
                    if not one_cycle():
                        break
                    cycles += 1
                except ReadUnavailable as error:
                    if not resilient or current_intent is not None:
                        raise
                    recover_health(error)
                    continue
                if max_cycles is None or cycles < max_cycles:
                    stop.wait(poll_seconds)
            report["phase"] = "stopped"
            publish()
    except Exception as error:
        # Unexpected response shapes or local errors must also stop every leg.
        # Only our sanitized exchange/local errors may contribute display text.
        interrupted = (isinstance(error, _RecoveryStopped) or
                       (stop.is_set() and current_intent is None and isinstance(error, OkxError)
                        and (is_retryable_read_error(error) or isinstance(error, ReadUnavailable))))
        report["halted"] = not interrupted
        report["phase"] = "stopped" if interrupted else "halted"
        report["connection"] = {"state": "failed", "attempt": 0, "max_attempts": 3}
        report["halt_reason"] = (str(error) if isinstance(error, (DemoHalt, OkxError, SessionError)) else
                                 "Portfolio controller error; inspect journal and all exchange positions")
        if isinstance(error, _RecoveryStopped):
            report["halt_reason"] = ""
        if isinstance(error, OkxError):
            report.update(halt_category=error.category, halt_api_code=error.api_code,
                          halt_http_status=error.http_status)
            subtype = _safe_tls_failure(getattr(error, "tls_failure", None), error.category)
            if subtype is not None:
                report["halt_tls_failure"] = subtype
        for inst, market in markets.items():
            state = report["markets"][inst]
            state["phase"] = report["phase"]
            if market.entry is not None and not interrupted:
                market.entry.update(status="inspect_required", reason=report["halt_reason"])
                if state["position"] is None:
                    state["position"] = {"side": market.entry["side"], "contracts": market.entry["size"],
                                         "status": "unknown; inspect exchange"}
                    state["position_known"] = False
        if (not interrupted and journal is not None and journal_path is not None
                and any(m.entry is not None for m in markets.values())):
            try:
                _save(journal_path, journal)
            except OSError:
                report["halt_reason"] += "; journal update failed; previous durable intents block restart"
        try:
            publish()
        except OSError:
            pass
    return report


def reconcile_portfolio(profile: PortfolioConfig, state_dir: Path, client: Any | None = None) -> dict:
    """Verify all four markets flat, then close terminal local intents only."""
    report = {"mode": "demo", "scope": "portfolio", "reconciled": False, "closed": 0,
              "halted": False, "halt_reason": "", "markets": {}}
    try:
        configs = _configs(profile)
        client = client or OkxClient(Credentials.from_env())
        key = getattr(getattr(client, "credentials", None), "api_key", "")
        if not key:
            raise DemoHalt("Portfolio reconciliation requires an API key identity")
        state_dir = Path(state_dir)
        if not state_dir.is_dir():
            raise DemoHalt("Portfolio state directory does not exist")
        journal_path = state_dir / "demo-journal.json"
        fingerprint = hashlib.sha256(key.encode()).hexdigest()
        with ExitStack() as locks:
            locks.enter_context(_lock(Path("/tmp") / ("okx-demo-" + fingerprint + ".lock")))
            locks.enter_context(_lock(state_dir / "demo.lock"))
            journal = _load(journal_path, allow_unresolved=True)
            _journal_universe(journal)
            client = ReadRetryClient(client, threading.Event())
            unresolved = [row for row in journal["orders"] if row.get("status") != "closed"]
            def all_flat() -> None:
                for inst in configs:
                    client.begin_cycle(60)
                    rows = _positions(client, inst)
                    report["markets"][inst] = {"position": _position_summary(rows)}
                    if rows or client.pending_orders(inst) or _algos(client, inst):
                        raise DemoHalt("Portfolio reconciliation requires every market flat with no ordinary or algo orders")
            all_flat()
            for row in unresolved:
                inst, identity = row.get("instrument"), row.get("client_id")
                if inst not in configs or not identity:
                    raise DemoHalt("Portfolio journal instrument or client identity is invalid")
                client.begin_cycle(60)
                order = client.query_order(inst, identity)
                if (order.get("instId") != inst or order.get("clOrdId") != identity
                        or order.get("state") not in {"filled", "canceled", "mmp_canceled"}
                        or _number(order.get("accFillSz"), "filled size") < 0):
                    raise DemoHalt("Portfolio order is not verified terminal; journal remains blocked")
            all_flat()
            for row in unresolved:
                row.update(status="closed", reconciled_at_ms=_now_ms())
            _save(journal_path, journal)
            report.update(reconciled=True, closed=len(unresolved))
    except Exception as error:
        report["halted"] = True
        report["halt_reason"] = (str(error) if isinstance(error, (DemoHalt, OkxError)) else
                                 "Portfolio reconciliation failed; journal remains blocked")
    return report
