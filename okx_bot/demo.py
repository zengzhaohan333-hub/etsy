"""Conservative OKX demo execution with durable intents and fail-closed recovery.

No live trading is implemented. Stop/drawdown limits stop new entries; they do
not flatten an existing exchange position. Verified exchange TP/SL remains active.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import threading
import time
import uuid
from contextlib import ExitStack, contextmanager
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .core import CrossStrategy, position_size, protection_prices
from .okx import Credentials, OkxClient, OkxError


class DemoHalt(RuntimeError):
    pass


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _number(value: Any, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise DemoHalt(f"Invalid exchange field: {field}") from None
    if not result.is_finite():
        raise DemoHalt(f"Invalid exchange field: {field}")
    return result


def _bar_ms(bar: str) -> int:
    intervals = {"1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000,
                 "30m": 1_800_000, "1H": 3_600_000, "2H": 7_200_000,
                 "4H": 14_400_000, "6H": 21_600_000, "12H": 43_200_000,
                 "1D": 86_400_000, "1Dutc": 86_400_000}
    try:
        return intervals[bar]
    except KeyError:
        raise DemoHalt("Unsupported demo candle interval") from None


@contextmanager
def _lock(path: Path):
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise DemoHalt("Another demo process owns this API key or state directory") from None
        yield
    finally:
        os.close(fd)


def _save(path: Path, contents: dict[str, Any]) -> None:
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(contents, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def _load(path: Path, *, allow_unresolved: bool = False) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "orders": []}
    try:
        contents = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(contents, dict) or contents.get("version") != 1
                or not isinstance(contents.get("orders"), list)
                or any(not isinstance(row, dict) or (not allow_unresolved and row.get("status") != "closed")
                       for row in contents["orders"])):
            raise ValueError()
    except (ValueError, OSError):
        raise DemoHalt("Unresolved or invalid demo journal; manually reconcile exchange positions, orders and algos before recovery") from None
    return contents


def _validate_bars(candles: list[Any], interval: int, minimum: int) -> None:
    if len(candles) < minimum:
        raise DemoHalt("Not enough closed candles to initialize strategy")
    for index, candle in enumerate(candles):
        if index and candle.ts - candles[index - 1].ts != interval:
            raise DemoHalt("Closed candles are out of order, duplicated or have a gap")
        if candle.ts < 0 or candle.high < max(candle.open, candle.close, candle.low) or candle.low > min(candle.open, candle.close, candle.high):
            raise DemoHalt("Invalid closed candle values")
    age = _now_ms() - (candles[-1].ts + interval)
    if age < -5_000 or age > interval + 30_000:
        raise DemoHalt("Latest closed candle is stale or in the future")


def _positions(client: Any, inst_id: str) -> list[dict[str, Any]]:
    return [row for row in client.positions(inst_id) if _number(row.get("pos"), "pos") != 0]


def _algos(client: Any, inst_id: str) -> list[dict[str, Any]]:
    return client.pending_algo_orders(inst_id)


def _position_summary(rows: list[dict[str, Any]]) -> dict[str, str] | None:
    if not rows:
        return None
    return {"side": "long" if _number(rows[0].get("pos"), "pos") > 0 else "short",
            "contracts": str(abs(_number(rows[0].get("pos"), "pos"))),
            "entry_price": str(_number(rows[0].get("avgPx", "0"), "avgPx"))}


def _verify_position(rows: list[dict[str, Any]], entry: dict[str, Any], inst_id: str) -> None:
    expected = Decimal(entry["size"]) * (1 if entry["side"] == "long" else -1)
    if (len(rows) != 1 or rows[0].get("instId") != inst_id
            or rows[0].get("mgnMode") != "isolated" or rows[0].get("posSide") != "net"
            or _number(rows[0].get("pos"), "pos") != expected
            or _number(rows[0].get("lever"), "lever") != 1):
        raise DemoHalt("Position differs from expected isolated 1x filled contracts; inspect required")


def _verify_bracket(client: Any, inst_id: str, entry: dict[str, Any]) -> None:
    if client.pending_orders(inst_id):
        raise DemoHalt("Unexpected pending order while monitoring a position")
    rows = _algos(client, inst_id)
    # Attached TP+SL is one OCO/conditional algo. Multiple/already-triggered algos
    # require manual inspection rather than guessing which order protects us.
    if len(rows) != 1:
        raise DemoHalt("Expected one exchange protective TP/SL algo; inspect required")
    algo = rows[0]
    algo_id = entry["client_id"] + "a"
    if ((algo.get("algoClOrdId") or algo.get("attachAlgoClOrdId")) != algo_id
            or algo.get("instId") != inst_id or algo.get("tdMode") != "isolated"
            or algo.get("side") != ("sell" if entry["side"] == "long" else "buy")
            or algo.get("posSide") != "net"
            or _number(algo.get("sz"), "algo size") != Decimal(entry["size"])
            or _number(algo.get("slTriggerPx"), "stop trigger") != Decimal(entry["stop_loss"])
            or _number(algo.get("tpTriggerPx"), "take-profit trigger") != Decimal(entry["take_profit"])
            or str(algo.get("slOrdPx")) != "-1" or str(algo.get("tpOrdPx")) != "-1"
            or algo.get("slTriggerPxType") != "mark" or algo.get("tpTriggerPxType") != "mark"):
        raise DemoHalt("Protective algo ownership, quantity or stop/target does not match; inspect required")


def _verify_fill(client: Any, config: Any, entry: dict[str, Any], stop: threading.Event,
                 poll_seconds: float) -> None:
    for attempt in range(3):
        order = client.query_order(config.instrument, entry["client_id"])
        if (order.get("clOrdId") != entry["client_id"]
                or order.get("instId") != config.instrument):
            raise DemoHalt("Order reconciliation identity mismatch; inspect required")
        if order.get("state") == "filled":
            if _number(order.get("accFillSz"), "filled size") != Decimal(entry["size"]):
                raise DemoHalt("Unexpected fill quantity; inspect required")
            _verify_position(_positions(client, config.instrument), entry, config.instrument)
            _verify_bracket(client, config.instrument, entry)
            return
        if order.get("state") not in {"live", "partially_filled"}:
            raise DemoHalt("Order did not fully fill; inspect exchange order and position")
        if attempt < 2:
            stop.wait(min(poll_seconds, 1))
    raise DemoHalt("Order remains unfilled or partially filled; inspect exchange order and position")


def run_demo(config: Any, state_dir: Path, stop: threading.Event, poll_seconds: float = 15,
             client: Any | None = None, max_cycles: int | None = None) -> dict[str, Any]:
    """Run until stopped, a bounded test cycle count, or a fail-closed condition.

    Requires a dedicated OKX demo account in net/single-currency-margin mode and
    no existing position/orders/algos for this instrument. The caller is responsible
    for the explicit demo acknowledgement in its UI/CLI before invoking this function.
    """
    report: dict[str, Any] = {"mode": "demo", "halted": False, "halt_reason": "",
                              "entries": 0, "position": None,
                              "note": "Stopping does not close exchange positions. Inspect actual positions and TP/SL after any error."}
    journal: dict[str, Any] | None = None
    journal_path: Path | None = None
    entry: dict[str, Any] | None = None
    status_path: Path | None = None
    status_writable = False

    def publish_status() -> None:
        report["updated_at_ms"] = _now_ms()
        if status_path is not None and status_writable:
            _save(status_path, report)

    try:
        if not 0 < poll_seconds <= 60:
            raise DemoHalt("Polling interval must be greater than 0 and at most 60 seconds")
        if max_cycles is not None and (isinstance(max_cycles, bool) or max_cycles < 0):
            raise DemoHalt("max_cycles must be a nonnegative integer")
        client = client or OkxClient(Credentials.from_env())
        credentials = getattr(client, "credentials", None)
        key = getattr(credentials, "api_key", "")
        if not key:
            raise DemoHalt("Demo client requires an API key identity for concurrency protection")
        fingerprint = hashlib.sha256(key.encode("utf-8")).hexdigest()
        state_dir = Path(state_dir)
        state_dir.mkdir(parents=True, exist_ok=True)
        journal_path = state_dir / "demo-journal.json"
        status_path = state_dir / "status.json"
        with ExitStack() as locks:
            locks.enter_context(_lock(Path("/tmp") / ("okx-demo-" + fingerprint + ".lock")))
            locks.enter_context(_lock(state_dir / "demo.lock"))
            status_writable = True
            journal = _load(journal_path)
            account = client.account_config()
            if account.get("posMode") != "net_mode" or str(account.get("acctLv")) != "2":
                raise DemoHalt("Demo account must use net mode and single-currency margin (acctLv=2)")
            if abs(client.server_time() - _now_ms()) > 5_000:
                raise DemoHalt("Local/exchange clock drift exceeds five seconds")
            metadata = client.instrument(config.instrument)
            if metadata.inst_id != config.instrument or metadata.settle_ccy != "USDT" or metadata.state != "live":
                raise DemoHalt("Unsupported or inactive instrument")
            existing = _positions(client, config.instrument)
            report["position"] = _position_summary(existing)
            if existing or client.pending_orders(config.instrument) or _algos(client, config.instrument):
                raise DemoHalt("Demo startup requires no existing position, pending order or algo for this instrument")
            interval = _bar_ms(config.bar)
            limit = min(300, max(100, config.slow_window + 2))
            history = client.candles(config.instrument, config.bar, limit)
            _validate_bars(history, interval, config.slow_window + 1)
            strategy = CrossStrategy(config.fast_window, config.slow_window)
            for candle in history:
                strategy.update(candle)  # Never submit historical strategy signals.
            last = history[-1]
            initial = client.balances()
            starting_equity = _number(initial.get("equity"), "equity")
            if starting_equity <= 0 or _number(initial.get("available"), "available") <= 0:
                raise DemoHalt("Demo account has no positive available USDT equity")
            peak_equity = starting_equity
            if stop.is_set():
                publish_status()
                return report
            client.set_leverage(config.instrument, "1")
            leverage = client.get_leverage(config.instrument)
            if (not leverage or any(row.get("instId") != config.instrument
                                    or row.get("mgnMode") != "isolated"
                                    or _number(row.get("lever"), "leverage") != 1 for row in leverage)):
                raise DemoHalt("Unable to verify isolated 1x leverage")
            cycles = 0
            while not stop.is_set() and (max_cycles is None or cycles < max_cycles):
                cycles += 1
                balances = client.balances()
                equity = _number(balances.get("equity"), "equity")
                available = _number(balances.get("available"), "available")
                if equity < 0 or available < 0:
                    raise DemoHalt("Invalid demo balance")
                peak_equity = max(peak_equity, equity)
                report["equity"] = str(equity)
                rows = _positions(client, config.instrument)
                report["position"] = _position_summary(rows)
                if entry is not None:
                    if rows:
                        _verify_position(rows, entry, config.instrument)
                        _verify_bracket(client, config.instrument, entry)
                    else:
                        if client.pending_orders(config.instrument) or _algos(client, config.instrument):
                            raise DemoHalt("Position is flat but pending orders/algos remain; inspect required")
                        entry["status"] = "closed"
                        entry["closed_at_ms"] = _now_ms()
                        _save(journal_path, journal)
                        entry = None
                elif rows or client.pending_orders(config.instrument) or _algos(client, config.instrument):
                    raise DemoHalt("Unexpected external position/order/algo detected")
                if (peak_equity - equity) / peak_equity >= config.max_drawdown_pct:
                    raise DemoHalt("Session drawdown limit reached; new entries stopped; any reported position remains open")
                candles = client.candles(config.instrument, config.bar, limit)
                _validate_bars(candles, interval, config.slow_window + 1)
                matching = [c for c in candles if c.ts == last.ts]
                if matching and matching[0] != last:
                    raise DemoHalt("Previously closed candle changed; inspect data source")
                new = [c for c in candles if c.ts > last.ts]
                if not new:
                    if candles[-1].ts < last.ts:
                        raise DemoHalt("Market data moved backwards")
                else:
                    if len(new) != 1 or new[0].ts - last.ts != interval:
                        raise DemoHalt("Missed candle interval; restart safely instead of trading old signals")
                    last = new[0]
                    signal = strategy.update(last)
                    if signal is not None and entry is None and not stop.is_set():
                        if signal not in {"long", "short"}:
                            raise DemoHalt("Unsupported strategy signal")
                        if _now_ms() - (last.ts + interval) > max(30_000, int(poll_seconds * 2_000)):
                            raise DemoHalt("New strategy signal arrived too late to execute")
                        mark_price, mark_timestamp = client.mark_price(config.instrument)
                        if stop.is_set():
                            break  # stop may arrive during a blocking HTTPS read
                        mark_age = _now_ms() - mark_timestamp
                        if mark_age < -5_000 or mark_age > 30_000 or mark_price <= 0:
                            raise DemoHalt("Mark price is stale or invalid; no order submitted")
                        budget = min(config.initial_equity, equity, available)
                        quantity = position_size(config, budget, mark_price, metadata.ct_val,
                                                 metadata.lot_size, metadata.min_size, metadata.max_market_size)
                        if quantity > 0:
                            sl, tp = protection_prices(config, signal, mark_price, metadata.tick_size)
                            entry = {"client_id": "d" + uuid.uuid4().hex[:23], "instrument": config.instrument,
                                     "side": signal, "size": str(quantity), "stop_loss": str(sl),
                                     "take_profit": str(tp), "status": "intent", "created_at_ms": _now_ms()}
                            journal["orders"].append(entry)
                            _save(journal_path, journal)  # Durable BEFORE any submit.
                            if stop.is_set():
                                entry["status"] = "closed"
                                entry["never_submitted"] = True
                                _save(journal_path, journal)
                                entry = None
                                break
                            acknowledgement = client.place_market_order(config.instrument,
                                "buy" if signal == "long" else "sell", quantity, entry["client_id"],
                                stop_loss=sl, take_profit=tp)
                            entry["status"] = "accepted"
                            entry["order_id"] = str(acknowledgement["ordId"])
                            _save(journal_path, journal)
                            _verify_fill(client, config, entry, stop, poll_seconds)
                            entry["status"] = "protected"
                            _save(journal_path, journal)
                            report["entries"] += 1
                            report["position"] = _position_summary(_positions(client, config.instrument))
                publish_status()
                if max_cycles is None or cycles < max_cycles:
                    stop.wait(poll_seconds)
            if entry is not None:
                report["note"] = "Demo runner stopped with an open position; exchange TP/SL remains active. Restart is blocked until manual reconciliation."
            publish_status()
    except (DemoHalt, OkxError, OSError, ValueError, KeyError, TypeError, ArithmeticError):
        # Never include raw unexpected exception/HTTP data: transports may contain
        # credentials. Known local errors already carry sanitized messages.
        import sys
        error = sys.exc_info()[1]
        report["halted"] = True
        report["halt_reason"] = str(error) if isinstance(error, (DemoHalt, OkxError)) else "Demo controller error; inspect journal and exchange state before restarting"
        if entry is not None and journal is not None and journal_path is not None:
            entry["status"] = "inspect_required"
            entry["reason"] = report["halt_reason"]
            try:
                _save(journal_path, journal)
            except OSError:
                report["halt_reason"] += "; journal update failed (previous durable intent still blocks restart)"
            report["position"] = report["position"] or {"side": entry["side"], "contracts": entry["size"], "status": "unknown; inspect exchange"}
        try:
            publish_status()
        except OSError:
            report["halt_reason"] += "; status file update failed"
    return report


def reconcile_demo(config: Any, state_dir: Path, client: Any | None = None) -> dict[str, Any]:
    """Read-only exchange reconciliation, then close verified terminal local records.

    No exchange mutation is performed. A missing order, a partial/live order, an
    open position or any pending algo prevents journal recovery. Explicit operator
    acknowledgement belongs to the CLI/UI that calls this helper.
    """
    report: dict[str, Any] = {"mode": "demo", "reconciled": False, "closed": 0,
                              "halted": False, "halt_reason": "", "position": None}
    try:
        client = client or OkxClient(Credentials.from_env())
        key = getattr(getattr(client, "credentials", None), "api_key", "")
        if not key:
            raise DemoHalt("Demo client requires an API key identity for concurrency protection")
        state_dir = Path(state_dir)
        if not state_dir.is_dir():
            raise DemoHalt("Demo state directory does not exist")
        fingerprint = hashlib.sha256(key.encode("utf-8")).hexdigest()
        journal_path = state_dir / "demo-journal.json"
        with ExitStack() as locks:
            locks.enter_context(_lock(Path("/tmp") / ("okx-demo-" + fingerprint + ".lock")))
            locks.enter_context(_lock(state_dir / "demo.lock"))
            journal = _load(journal_path, allow_unresolved=True)
            rows = _positions(client, config.instrument)
            report["position"] = _position_summary(rows)
            if rows or client.pending_orders(config.instrument) or _algos(client, config.instrument):
                raise DemoHalt("Reconciliation requires a flat account with no pending orders or algos for this instrument")
            unresolved = [row for row in journal["orders"] if row.get("status") != "closed"]
            for row in unresolved:
                if row.get("instrument") != config.instrument or not row.get("client_id"):
                    raise DemoHalt("Journal instrument/identity mismatch; manual inspection required")
                order = client.query_order(config.instrument, row["client_id"])
                if (order.get("instId") != config.instrument or order.get("clOrdId") != row["client_id"]
                        or order.get("state") not in {"filled", "canceled", "mmp_canceled"}):
                    raise DemoHalt("Order is not verified terminal; journal remains blocked")
                if _number(order.get("accFillSz"), "filled size") < 0:
                    raise DemoHalt("Invalid terminal fill quantity")
            # Recheck after querying all orders: do not rely on an earlier flat snapshot.
            rows = _positions(client, config.instrument)
            report["position"] = _position_summary(rows)
            if rows or client.pending_orders(config.instrument) or _algos(client, config.instrument):
                raise DemoHalt("Exchange state changed during reconciliation; journal remains blocked")
            for row in unresolved:
                row["status"] = "closed"
                row["reconciled_at_ms"] = _now_ms()
            _save(journal_path, journal)
            report["closed"] = len(unresolved)
            report["reconciled"] = True
    except (DemoHalt, OkxError, OSError, ValueError, KeyError, TypeError, ArithmeticError):
        import sys
        error = sys.exc_info()[1]
        report["halted"] = True
        report["halt_reason"] = str(error) if isinstance(error, (DemoHalt, OkxError)) else "Demo reconciliation failed; journal remains blocked"
    return report
