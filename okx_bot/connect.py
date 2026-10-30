"""Local, interactive OKX demo connection wizard; credentials stay in memory.

Run ``python3 -m okx_bot.connect --check-only`` for read-only account checks.
Only an exact interactive ``DEMO`` acknowledgement can start demo execution.
"""

from __future__ import annotations

import argparse
import getpass
import os
import re
import signal
import sys
import threading
import time
import warnings
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .config import Config, load_config
from .demo import run_demo
from .okx import Credentials, OkxClient, OkxError


class ConnectionCheckError(RuntimeError):
    """A locally generated, credential-free explanation of a refused check."""


_CREDENTIAL_FIELDS = (
    ("OKX_API_KEY", "请输入欧易【模拟盘】API Key（输入不会显示）："),
    ("OKX_API_SECRET", "请输入欧易【模拟盘】Secret Key（输入不会显示）："),
    ("OKX_API_PASSPHRASE", "请输入创建 API 时设置的 Passphrase（输入不会显示）："),
)


def _is_tty() -> bool:
    try:
        return bool(sys.stdin.isatty())
    except (AttributeError, OSError):
        return False


def collect_credentials() -> Credentials:
    """Reuse environment bindings and prompt only missing values without echo.

    No environment variables, process arguments or credential files are written.
    Turning GetPassWarning into an exception prevents getpass's visible fallback.
    """
    values = [os.environ.get(name, "") for name, _prompt in _CREDENTIAL_FIELDS]
    missing = [index for index, value in enumerate(values) if not value.strip()]
    if missing and not _is_tty():
        raise ConnectionCheckError("缺少模拟盘凭证，且当前不是交互终端。请在 WSL 终端直接运行向导，不要通过管道输入密钥。")
    for index in missing:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            try:
                values[index] = getpass.getpass(_CREDENTIAL_FIELDS[index][1], stream=sys.stderr)
            except getpass.GetPassWarning:
                raise ConnectionCheckError("当前终端无法关闭输入回显，已取消。请换用正常的 WSL 交互终端。") from None
        if not values[index].strip():
            raise ConnectionCheckError("凭证不能为空，已取消；没有保存凭证或启动机器人。")
    return Credentials(*values)


def _finite_decimal(value: Any, label: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ConnectionCheckError(f"模拟账户返回的{label}格式异常，未启动机器人。") from None
    if not number.is_finite():
        raise ConnectionCheckError(f"模拟账户返回的{label}格式异常，未启动机器人。")
    return number


def check_demo(config: Config, client: Any) -> dict[str, Any]:
    """Read-only checks. Never change account settings, orders or leverage."""
    account = client.account_config()
    if account.get("posMode") != "net_mode":
        raise ConnectionCheckError("模拟账户需要使用单向持仓模式（net_mode）。请先在欧易模拟盘切换，向导不会替你修改账户。")
    if str(account.get("acctLv")) != "2":
        raise ConnectionCheckError("模拟账户需要使用单币种保证金模式（acctLv=2）。请先在欧易模拟盘设置。")
    server_ms = client.server_time()
    if type(server_ms) is not int or abs(server_ms - time.time_ns() // 1_000_000) > 5_000:
        raise ConnectionCheckError("电脑与欧易服务器的时间相差超过 5 秒，请先同步电脑时间。")
    instrument = client.instrument(config.instrument)
    if (instrument.inst_id != config.instrument or instrument.settle_ccy != "USDT"
            or instrument.ct_val_ccy != config.instrument.split("-")[0] or instrument.state != "live"):
        raise ConnectionCheckError("当前合约不是可用的 USDT 永续合约，未启动机器人。")
    for label in ("ct_val", "lot_size", "min_size", "max_market_size", "tick_size"):
        if _finite_decimal(getattr(instrument, label), "合约规格") <= 0:
            raise ConnectionCheckError("合约规格必须为正数，未启动机器人。")
    balances = client.balances()
    equity = _finite_decimal(balances.get("equity"), "USDT 权益")
    available = _finite_decimal(balances.get("available"), "可用 USDT 权益")
    if balances.get("currency") != "USDT" or equity <= 0 or available <= 0:
        raise ConnectionCheckError("模拟账户没有足够的正数 USDT 权益和可用资金。请先在欧易模拟盘领取或补充模拟资金。")
    positions = client.positions(config.instrument)
    if not isinstance(positions, list):
        raise ConnectionCheckError("持仓响应格式异常，未启动机器人。")
    for row in positions:
        if not isinstance(row, dict) or _finite_decimal(row.get("pos"), "持仓数量") != 0:
            raise ConnectionCheckError("当前合约已有模拟持仓。请先在欧易模拟盘检查并处理；向导不会自动平仓。")
    if client.pending_orders(config.instrument):
        raise ConnectionCheckError("当前合约已有普通模拟挂单。请先在欧易模拟盘检查并处理。")
    if client.pending_algo_orders(config.instrument):
        raise ConnectionCheckError("当前合约已有模拟策略单或止盈止损单。请先在欧易模拟盘检查并处理。")
    # Only explicitly selected, validated scalar fields leave this function.
    return {"mode": "demo", "instrument": config.instrument,
            "equity": equity, "available": available, "flat": True}


@contextmanager
def _stop_signals(stop: threading.Event):
    previous: dict[Any, Any] = {}

    def request_stop(_signal: int, _frame: Any) -> None:
        stop.set()

    try:
        for name in (signal.SIGINT, signal.SIGTERM):
            old_handler = signal.getsignal(name)
            signal.signal(name, request_stop)
            previous[name] = old_handler
        yield
    finally:
        for name, old_handler in previous.items():
            signal.signal(name, old_handler)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="欧易模拟盘连接向导：密钥只在本机内存使用，无实盘功能")
    result.add_argument("--config", type=Path, default=Path("config.paper.toml"), help="不含密钥的策略配置")
    result.add_argument("--state-dir", type=Path, default=Path(".runtime-demo"), help="仅存交易状态和日志，不保存密钥")
    result.add_argument("--check-only", action="store_true", help="只读检查后退出，不询问启动、不下单")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        print("欧易（OKX）模拟盘连接向导：仅支持模拟交易，没有实盘入口。")
        print("请使用模拟盘创建的 API；凭证不会显示、写入文件或保存到环境变量。")
        print("沿用当前终端的代理设置；请使用专用模拟账户。")
        try:
            config = load_config(args.config)
        except (OSError, ValueError, TypeError):
            print("无法读取策略配置。请确认配置文件存在且格式正确；未启动机器人。", file=sys.stderr)
            return 1
        credentials = collect_credentials()
        client = OkxClient(credentials)
        summary = check_demo(config, client)
        print("只读检查通过：单向持仓、单币种保证金；当前合约无持仓、普通挂单或策略单。")
        print(f"合约：{summary['instrument']}；模拟 USDT 权益：{summary['equity']}；可用：{summary['available']}。")
        print(f"当前策略：{config.bar} K 线，均线 {config.fast_window}/{config.slow_window}；"
              f"单笔名义金额上限 {config.max_notional} USDT；"
              f"风险预算 {config.risk_per_trade * 100}%；会话回撤阈值 {config.max_drawdown_pct * 100}%。")
        print("运行器会再次检查并设置 1×逐仓。停止程序或触发回撤阈值不会自动平掉已有模拟仓位；"
              "请在欧易模拟盘检查持仓及交易所止盈止损单。")
        if args.check_only:
            print("本次仅检查，未启动机器人、未下单。")
            return 0
        if not _is_tty():
            print("当前不是交互终端，未启动机器人。需要只读检查时请使用 --check-only。", file=sys.stderr)
            return 1
        acknowledgement = input("如需开始自动模拟交易，请输入完整的 DEMO；直接回车退出：")
        if acknowledgement != "DEMO":
            print("已退出，未启动机器人、未下单。")
            return 0
        stop = threading.Event()
        print("开始自动模拟交易。按 Ctrl+C 请求停止；停止后请检查欧易模拟盘是否仍有持仓。")
        with _stop_signals(stop):
            result = run_demo(config, args.state_dir, stop, client=client)
        if result.get("halted"):
            print("模拟交易程序已停止或启动被拒绝。请查看本机状态目录中的 status.json，"
                  "并核对欧易模拟盘持仓、挂单和止盈止损单；不要直接删除恢复日志。", file=sys.stderr)
            return 1
        print("模拟交易程序已结束。请检查欧易模拟盘的剩余持仓及止盈止损单。")
        return 0
    except ConnectionCheckError as error:
        print(f"无法继续：{error}", file=sys.stderr)
        return 1
    except OkxError as error:
        # Only accept the adapter's exact numeric-code format, never echo an
        # arbitrary exception or OKX's raw message/request authentication data.
        code = re.fullmatch(r"OKX rejected request \(code ([0-9]{1,12})\)", str(error))
        if code:
            print(f"欧易模拟盘 API 返回错误代码 {code[1]}。", file=sys.stderr)
            if code[1] == "50101":
                print("请确认使用的是在欧易【模拟交易】中创建的 API。", file=sys.stderr)
            elif code[1] == "50102":
                print("请检查 Windows 和 WSL 时间是否已同步。", file=sys.stderr)
        else:
            print("欧易模拟盘请求未完成，请检查当前代理连接、模拟盘 API 权限和系统时间。", file=sys.stderr)
        return 1
    except (EOFError, KeyboardInterrupt):
        print("已取消。若此前已开始运行，请在欧易模拟盘检查是否仍有持仓及止盈止损单。")
        return 130
    except Exception:
        # Do not echo arbitrary exception strings: libraries/transports can embed
        # request headers or entered credential values in their diagnostics.
        print("连接检查或运行未完成。请检查代理、模拟盘 API 权限、账户设置和系统时间；"
              "若已开始运行，请核对欧易模拟盘持仓与挂单。错误详情未输出，以免泄露凭证。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
