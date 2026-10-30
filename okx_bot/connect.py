"""Local, interactive OKX demo connection wizard; credentials stay in memory.

Run ``python3 -m okx_bot.connect --check-only`` for read-only account checks.
Only an exact interactive ``DEMO`` acknowledgement can start demo execution.
"""

from __future__ import annotations

import argparse
import getpass
import ipaddress
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
from typing import Any, Callable

from .config import Config, load_config
from .demo import run_demo
from .okx import Credentials, OkxClient, OkxError, _safe_tls_failure
from .read_retry import ReadRetryClient


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


def check_demo(config: Config, client: Any, *,
               progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    """Read-only checks. Never change account settings, orders or leverage."""
    report = progress or (lambda _message: None)
    client = ReadRetryClient(client, threading.Event(),
        on_retry=lambda event: report(f"只读查询暂未成功，正在重试 {event['attempt']}/{event['max_attempts']}……"),
        on_recovered=lambda: report("只读查询已恢复，继续核对账户与挂单……"))
    client.begin_cycle(60)
    report("正在检查欧易公共接口与系统时间（此请求不发送凭证）……")
    server_ms = client.server_time()
    if type(server_ms) is not int or abs(server_ms - time.time_ns() // 1_000_000) > 5_000:
        raise ConnectionCheckError("电脑与欧易服务器的时间相差超过 5 秒，请先同步电脑时间。")
    report("公共接口及时间检查通过，正在检查模拟盘 API……")
    account = client.account_config()
    if account.get("posMode") != "net_mode":
        raise ConnectionCheckError("模拟账户需要使用单向持仓模式（net_mode）。请先在欧易模拟盘切换，向导不会替你修改账户。")
    if str(account.get("acctLv")) != "2":
        raise ConnectionCheckError("模拟账户需要使用单币种保证金模式（acctLv=2）。请先在欧易模拟盘设置。")
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


def _print_api_error(error: OkxError) -> None:
    """Display validated scalar diagnostics, never arbitrary remote messages."""
    code = getattr(error, "api_code", None)
    if not isinstance(code, str) or not re.fullmatch(r"[0-9]{1,12}", code):
        # Keep compatibility with older adapters and controlled custom clients.
        matched = re.fullmatch(r"OKX rejected request \(code ([0-9]{1,12})\)", str(error))
        code = matched[1] if matched else None
    status = getattr(error, "http_status", None)
    http = f"（HTTP {status}）" if type(status) is int and 100 <= status <= 599 else ""
    if code:
        print(f"欧易模拟盘 API 返回错误代码 {code}{http}。", file=sys.stderr)
        request_ip = None
        candidate = getattr(error, "request_ip", None)
        if code == "50110" and isinstance(candidate, str) and re.fullmatch(r"[0-9A-Fa-f:.]{2,45}", candidate):
            try:
                address = ipaddress.ip_address(candidate)
                if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
                    address = address.ipv4_mapped
                request_ip = str(address)
            except ValueError:
                pass
        if request_ip is not None:
            print(f"欧易在本次请求中报告的来源 IP：{request_ip}。", file=sys.stderr)
        elif code == "50110":
            print("欧易本次响应未提供可识别的来源 IP，程序没有猜测地址。", file=sys.stderr)
        hints = {
            "50101": "请确认使用的是在欧易【模拟交易】中创建的 API。",
            "50102": "请检查 Windows 和 WSL 时间是否已同步。",
            "50105": "请核对创建这组 API 时设置的 Passphrase；它不是欧易登录密码。",
            "50110": "请核对当前 API 的 IP 白名单与本次请求来源 IP。其他网站查询到的代理出口可能不同；并请在本机确认输入的 API Key 属于正在查看的这组 API。",
            "50111": "请重新核对模拟盘 API Key，并确认 API Key、Secret Key 来自同一组 API。",
            "50113": "签名校验失败。请确认 API Key 与 Secret Key 来自同一组，并检查复制时是否混入多余字符。",
            "50011": "请求过于频繁。请稍后再检查，避免同时打开多个连接向导。",
        }
        hint = hints.get(code, "请根据此错误代码核对欧易模拟盘 API 设置；不要发送密钥或完整响应。")
        if code == "50110" and request_ip is not None:
            hint = "请将上方来源 IP 与当前 API 白名单对照。若不同，请先核对代理出口；若相同，请在本机重新核对输入的 API Key 是否属于这组 API。"
    else:
        print(f"欧易模拟盘请求未完成{http}。", file=sys.stderr)
        hints = {
            "credential_format": "凭证包含无法用于请求的字符。请使用 API 页面各字段旁的复制按钮，避免复制中文标签、换行或整个说明段落。",
            "proxy": "代理服务器拒绝了连接或要求认证。请检查当前代理连接，暂时不要修改 API。",
            "redirect": "请求返回重定向，程序已拒绝跳转。请核对官方 API 访问是否正常。",
            "rate_limit": "请求过于频繁。请稍后重试连接检查，避免同时打开多个向导。",
            "server": "服务端暂时不可用。请稍后重新进行连接检查。",
            "timeout": "连接超时。请先检查当前代理是否仍连接，以及同一路径能否访问欧易公共接口。",
            "tls": "HTTPS/TLS 连接校验或握手失败。请核对系统时间与网络连接，保留证书校验。",
            "dns": "域名解析失败。请核对当前代理连接及公共接口访问情况。",
            "invalid_response": "未收到有效的欧易 API 响应。请检查当前代理连接及公共接口访问情况。",
            "http": "未收到可用的欧易错误码。请先检查当前代理访问公共接口的结果；仅凭此 HTTP 状态不能确认凭证是否正确。",
        }
        category = getattr(error, "category", None)
        fallback = "请检查当前代理连接和欧易公共接口访问；当前信息不足以判断密钥、权限或白名单是否正确。"
        hint = hints.get(category, fallback) if isinstance(category, str) else fallback
        tls_failure = _safe_tls_failure(getattr(error, "tls_failure", None), category)
        tls_hints = {
            "certificate": "HTTPS 证书验证失败。请核对系统时间和证书链，保留证书校验。",
            "unexpected_eof": "TLS 连接被提前关闭，有限读取重试后仍未恢复。请检查当前代理连接的稳定性。",
            "handshake": "TLS 握手失败。请检查当前代理连接；这不表示已确认是证书问题。",
        }
        if tls_failure is not None:
            hint = tls_hints[tls_failure]
    print(hint, file=sys.stderr)


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
        summary = check_demo(config, client, progress=print)
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
        _print_api_error(error)
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
