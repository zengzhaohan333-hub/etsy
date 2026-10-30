"""Three independent, unsigned public-time checks; no account or trading calls.

The ordinary OkxClient supplies the same fixed HTTPS host, simulated header,
user agent, proxy routing and verified TLS transport as the bot. This module
never loads credentials, reads bot configuration or creates runtime files.
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from typing import Any, Callable, TextIO

from .okx import OkxClient, OkxError, _safe_tls_failure


_CATEGORIES = frozenset({
    "local", "api", "http", "redirect", "proxy", "rate_limit", "server",
    "timeout", "tls", "dns", "transport", "invalid_response", "credential_format",
})
_MAX_TIMESTAMP_MS = 32_503_680_000_000
_FAILURE_LABELS = {
    "certificate": "HTTPS 证书验证失败",
    "unexpected_eof": "TLS 连接被提前关闭",
    "handshake": "TLS 握手失败",
    "timeout": "连接超时",
    "dns": "域名解析失败",
    "proxy": "代理拒绝连接或要求认证",
    "transport": "连接中断",
    "server": "欧易服务暂时不可用",
    "rate_limit": "请求受到限流",
    "invalid_response": "未收到有效公共接口数据",
    "redirect": "请求重定向已被拒绝",
    "http": "公共接口返回 HTTP 错误",
}


def _safe_details(error: OkxError) -> dict[str, Any]:
    """Revalidate safe scalar diagnostics; never inspect the exception message."""
    category = getattr(error, "category", None)
    category = category if type(category) is str and category in _CATEGORIES else "unknown"
    details: dict[str, Any] = {"category": category}
    status = getattr(error, "http_status", None)
    if type(status) is int and 100 <= status <= 599:
        details["http_status"] = status
    code = getattr(error, "api_code", None)
    if type(code) is int:
        code = str(code) if 0 < code < 10 ** 12 else None
    if type(code) is str and re.fullmatch(r"[0-9]{1,12}", code) and int(code) != 0:
        details["api_code"] = code
    tls_failure = _safe_tls_failure(getattr(error, "tls_failure", None), category)
    if tls_failure is not None:
        details["tls_failure"] = tls_failure
    return details


def _timestamp(value: Any) -> int | None:
    return value if type(value) is int and 0 <= value <= _MAX_TIMESTAMP_MS else None


def _read_clock(clock_ms: Callable[[], int]) -> int | None:
    try:
        return _timestamp(clock_ms())
    except Exception:
        return None


def run_diagnostics(*, attempts: int = 3, client: Any = None,
                    clock_ms: Callable[[], int] | None = None,
                    delay: Callable[[float], Any] | None = None,
                    output: TextIO | None = None) -> dict[str, Any]:
    """Run each requested public check once, continuing after a failed check.

    ``attempts`` is 1–3, with a one-second pause only between independent checks.
    Exit code zero means all requested public checks succeeded; it does not
    establish private API access, trading readiness or continuous connectivity.
    Dependencies are injectable so tests need no real network or waiting.
    """
    if type(attempts) is not int or not 1 <= attempts <= 3:
        raise ValueError("attempts 必须是 1 到 3 的整数")
    clock_ms = clock_ms if clock_ms is not None else lambda: time.time_ns() // 1_000_000
    delay = delay if delay is not None else time.sleep
    output = output if output is not None else sys.stdout
    report: dict[str, Any] = {"requested": attempts, "completed": 0,
                              "successes": 0, "failures": 0, "checks": [], "exit_code": 1}
    print("只读连接诊断：仅检查欧易公开时间接口，不读取账户、不需要 API 密钥。", file=output, flush=True)
    if client is None:
        try:
            client = OkxClient(credentials=None)
        except Exception:
            report["initialization_failed"] = True
            print("无法初始化公开连接；尚未发送请求，未展示原始错误内容。", file=output, flush=True)
            return report
    for attempt in range(1, attempts + 1):
        if attempt > 1:
            delay(1)
        check: dict[str, Any] = {"attempt": attempt, "stage": "public_time", "success": False}
        started = _read_clock(clock_ms)
        try:
            server_time = _timestamp(client.server_time())
            finished = _read_clock(clock_ms)
            if server_time is None:
                check.update(category="invalid_response")
            else:
                check.update(success=True, server_time_ms=server_time)
                if started is not None and finished is not None and finished >= started:
                    delta = server_time - (started + finished) // 2
                    check.update(round_trip_ms=finished - started, clock_delta_ms=delta,
                                 clock_warning=abs(delta) > 5_000)
                else:
                    check["clock_warning"] = True
        except OkxError as error:
            check.update(_safe_details(error))
        except Exception:
            # Never include raw error messages, representations, URLs or bodies.
            check.update(category="unknown")
        report["completed"] += 1
        report["checks"].append(check)
        if check["success"]:
            report["successes"] += 1
            timing = (f"；耗时 {check['round_trip_ms']} ms；服务器时间差 {check['clock_delta_ms']:+d} ms"
                      if "clock_delta_ms" in check else "；本机计时异常，未计算时差")
            print(f"第 {attempt}/{attempts} 次：成功{timing}。", file=output, flush=True)
        else:
            report["failures"] += 1
            fields = [f"category={check['category']}"]
            for name in ("http_status", "api_code", "tls_failure"):
                if name in check:
                    fields.append(f"{name}={check[name]}")
            label = _FAILURE_LABELS.get(check.get("tls_failure") or check["category"], "公共接口检查失败")
            print(f"第 {attempt}/{attempts} 次：{label}；" + "；".join(fields) + "。", file=output, flush=True)
    report["exit_code"] = 0 if report["successes"] == attempts else 1
    print(f"诊断结束：成功 {report['successes']}/{attempts}，失败 {report['failures']}/{attempts}。", file=output, flush=True)
    if any(check.get("clock_warning") for check in report["checks"]):
        print("存在本机计时或超过 5 秒的时差提示，请核对电脑时间。", file=output, flush=True)
    print("结果仅反映这几次公开请求，不代表账户授权或持续在线状态。", file=output, flush=True)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="无需 API 密钥的欧易公开连接诊断，不读取账户或提交订单")
    parser.add_argument("--attempts", type=int, choices=(1, 2, 3), default=3,
                        help="独立检查次数（1–3，默认 3）；每两次之间等待 1 秒")
    args = parser.parse_args(argv)
    try:
        return run_diagnostics(attempts=args.attempts)["exit_code"]
    except KeyboardInterrupt:
        print("诊断已取消；没有提交任何交易。", file=sys.stderr)
        return 130
    except Exception:
        print("诊断未完成，未展示原始错误内容；没有提交任何交易。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
