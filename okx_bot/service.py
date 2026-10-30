"""Noninteractive, demo-only Linux service entry point.

Credentials come from systemd LoadCredential (or an explicit private directory).
Trading requires the deployment's explicit flag. Safety stops never masquerade
as restartable network failures; transient reads recover inside the controller.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
import signal
import threading

from .connect import ConnectionCheckError, check_portfolio
from .demo import DemoHalt, _load, _lock, _number
from .okx import OkxClient, OkxError, _safe_tls_failure
from .portfolio import reconcile_portfolio, run_portfolio
from .portfolio_config import load_portfolio_config
from .read_retry import ReadRetryClient
from .service_credentials import ServiceCredentialError, load_service_credentials
from .session import PortfolioSession, SessionError, profile_fingerprint


EXIT_OK = 0
EXIT_BUG = 70
EXIT_TEMPORARY = 75
EXIT_BLOCKED = 78
_CATEGORIES = frozenset({"local", "api", "http", "redirect", "proxy", "rate_limit", "server",
                         "timeout", "tls", "dns", "transport", "invalid_response", "credential_format"})
_LOCAL_REASONS = {
    "Risk session is missing; explicit read-only initialization is required": "risk_session_initialization_required",
    "Existing drawdown stop remains latched; initialization cannot reset it": "risk_stop_latched",
    "Persistent session drawdown stop is latched; manual review is required": "risk_stop_latched",
    "All four demo instruments must already use isolated 1x leverage before service initialization": "configure_four_markets_isolated_1x_first",
    "Recovery cannot verify isolated 1x leverage": "configure_four_markets_isolated_1x_first",
    "Risk state belongs to a different account or strategy; manual review is required": "risk_binding_mismatch",
    "Order journal is missing for an existing service session; manual review is required": "paired_journal_missing",
}


def _event(name: str, **fields) -> None:
    """Write only caller-selected scalar fields, never reports or exceptions."""
    try:
        print(json.dumps({"event": name, **fields}, ensure_ascii=False, allow_nan=False), flush=True)
    except (OSError, ValueError):
        # Losing the console must not interrupt durable account control.
        pass


def _safe_failure(value) -> dict:
    result = {}
    category = getattr(value, "category", None)
    code = getattr(value, "api_code", None)
    status = getattr(value, "http_status", None)
    subtype = getattr(value, "tls_failure", None)
    if isinstance(value, dict):
        category, code, status, subtype = (value.get("halt_category"), value.get("halt_api_code"),
                                            value.get("halt_http_status"), value.get("halt_tls_failure"))
    if type(category) is str and category in _CATEGORIES:
        result["category"] = category
    if type(code) in (str, int) and re.fullmatch(r"[0-9]{1,12}", str(code)):
        result["api_code"] = str(code)
    if type(status) is int and 100 <= status <= 599:
        result["http_status"] = status
    tls_failure = _safe_tls_failure(subtype, category)
    if tls_failure is not None:
        result["tls_failure"] = tls_failure
    return result


def _local_reason(value, default="account_or_journal_review_required") -> str:
    return _LOCAL_REASONS.get(value, default) if type(value) is str else default


@contextmanager
def _signals(stop: threading.Event):
    previous = {}
    try:
        for name in (signal.SIGINT, signal.SIGTERM):
            previous[name] = signal.getsignal(name)
            signal.signal(name, lambda _number, _frame: stop.set())
        yield
    finally:
        for name, handler in previous.items():
            signal.signal(name, handler)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="欧易四币模拟盘 Linux 后台服务，无实盘功能")
    result.add_argument("--config", type=Path, default=Path("/etc/okxbot/config.portfolio.toml"))
    result.add_argument("--state-dir", type=Path, default=Path("/var/lib/okxbot"))
    result.add_argument("--credentials-dir", type=Path,
                        help="私有凭证目录；默认仅从 systemd CREDENTIALS_DIRECTORY 读取")
    actions = result.add_mutually_exclusive_group()
    actions.add_argument("--check-only", action="store_true", help="只读检查首次部署的四币空仓账户，不启动交易")
    actions.add_argument("--reconcile", action="store_true", help="只读核对交易所，安全更新已结束的本地日志，不启动交易")
    actions.add_argument("--initialize-session", action="store_true", help="首次部署：只读核对四币空仓和日志后初始化持久风控，不启动交易")
    result.add_argument("--ack-journal", action="store_true", help="允许 --reconcile 更新已核实终结的本地日志")
    result.add_argument("--ack-demo-orders", action="store_true", help="允许模拟盘订单；仍永久强制模拟盘")
    result.add_argument("--ack-session-init", action="store_true", help="明确允许首次初始化持久风控；不会清除已有回撤锁")
    result.add_argument("--poll-seconds", type=float, default=15)
    return result


def initialize_session(profile, state_dir: Path, client) -> PortfolioSession:
    """One explicit, locked initialization; never a service-start fallback."""
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = getattr(getattr(client, "credentials", None), "api_key", None)
    if type(key) is not str or not key:
        raise SessionError("Session initialization requires a validated account identity")
    fingerprint = hashlib.sha256(key.encode()).hexdigest()
    with _lock(Path("/tmp") / ("okx-demo-" + fingerprint + ".lock")), _lock(state_dir / "demo.lock"):
        _load(state_dir / "demo-journal.json")  # Unresolved intents cannot be reset.
        check_portfolio(profile, client)
        check_portfolio(profile, client)
        reads = ReadRetryClient(client, threading.Event())
        for instrument in profile.instruments:
            reads.begin_cycle(60)
            leverage = reads.get_leverage(instrument)
            if (not leverage or any(not isinstance(row, dict) or row.get("instId") != instrument
                                    or row.get("mgnMode") != "isolated" or _number(row.get("lever"), "leverage") != 1
                                    for row in leverage)):
                raise SessionError("All four demo instruments must already use isolated 1x leverage before service initialization")
        reads.begin_cycle(60)
        balances = reads.balances()
        equity = balances.get("equity")
        if balances.get("currency") != "USDT":
            raise SessionError("USDT account equity is required for service initialization")
        if not isinstance(equity, Decimal) or not equity.is_finite() or equity <= 0:
            raise SessionError("Fresh positive USDT equity is required to initialize risk state")
        session = PortfolioSession.load_or_create(state_dir, fingerprint, profile_fingerprint(profile),
                                                  equity, allow_initialize=True)
        if session.risk_halted:
            raise SessionError("Existing drawdown stop remains latched; initialization cannot reset it")
        return session


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if (not args.check_only and not args.reconcile and not args.initialize_session and not args.ack_demo_orders):
        _event("service_refused", reason="explicit_demo_ack_required")
        return EXIT_BLOCKED
    if args.reconcile and not args.ack_journal:
        _event("service_refused", reason="explicit_journal_ack_required")
        return EXIT_BLOCKED
    if args.ack_journal and not args.reconcile:
        _event("service_refused", reason="journal_flag_requires_reconcile")
        return EXIT_BLOCKED
    if args.initialize_session != args.ack_session_init:
        _event("service_refused", reason="session_initialization_requires_explicit_matching_flag")
        return EXIT_BLOCKED
    try:
        profile = load_portfolio_config(args.config)
        credentials = load_service_credentials(args.credentials_dir)
        client = OkxClient(credentials=credentials)
    except (ServiceCredentialError, OSError, ValueError, TypeError):
        _event("service_refused", reason="config_or_private_credentials_unavailable")
        return EXIT_BLOCKED
    except Exception:
        _event("service_failed", reason="initialization_error")
        return EXIT_BUG
    try:
        if args.initialize_session:
            initialize_session(profile, args.state_dir, client)
            _event("risk_session_initialized", mode="demo", trading_started=False)
            return EXIT_OK
        if args.check_only:
            # check_portfolio never sets leverage, writes journals or submits.
            check_portfolio(profile, client)
            _event("preflight_passed", mode="demo", markets=4, bar="5m")
            return EXIT_OK
        if args.reconcile:
            result = reconcile_portfolio(profile, args.state_dir, client=client)
            if result.get("halted") or not result.get("reconciled"):
                _event("reconciliation_blocked", **_safe_failure(result))
                return EXIT_BLOCKED
            closed = result.get("closed")
            _event("reconciliation_completed", closed=closed if type(closed) is int and closed >= 0 else 0)
            return EXIT_OK
        stop = threading.Event()
        _event("service_starting", mode="demo", markets=4, bar="5m", auto_recover_reads=True)
        with _signals(stop):
            result = run_portfolio(profile, args.state_dir, stop, poll_seconds=args.poll_seconds,
                                   client=client, resilient=True, resume=True)
        if not isinstance(result, dict) or type(result.get("halted")) is not bool:
            _event("service_failed", reason="invalid_controller_result")
            return EXIT_BUG
        if result["halted"]:
            _event("service_blocked", reason=_local_reason(result.get("halt_reason")), **_safe_failure(result))
            return EXIT_BLOCKED
        _event("service_stopped", mode="demo")
        return EXIT_OK
    except OkxError as error:
        # A restart could repeat a partially completed mutation. The controller
        # owns recovery; a top-level adapter error never grants restartability.
        _event("service_blocked", reason="exchange_check_failed", **_safe_failure(error))
        return EXIT_BLOCKED
    except ConnectionCheckError:
        _event("service_blocked", reason="account_preflight_failed")
        return EXIT_BLOCKED
    except (SessionError, DemoHalt) as error:
        _event("service_blocked", reason=_local_reason(str(error), "risk_session_or_journal_review_required"))
        return EXIT_BLOCKED
    except KeyboardInterrupt:
        _event("service_interrupted", reason="verify_exchange_positions")
        return EXIT_OK
    except Exception:
        _event("service_failed", reason="unexpected_controller_error")
        return EXIT_BUG


if __name__ == "__main__":
    raise SystemExit(main())
