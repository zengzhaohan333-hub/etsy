"""Local, read-only monitor for an already running OKX demo robot.

May be run as a file from a fresh download, while an older robot keeps running.
This module uses only the selected status/journal/config files. It never reads
credentials, starts a robot, submits orders, or modifies the selected directory.
"""
from __future__ import annotations

import argparse
from collections import deque
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import secrets
import threading
import time
import tomllib


MAX_BYTES = 4 * 1024 * 1024
MAX_AGE_MS = 90_000
_CONNECTION_CATEGORIES = frozenset({"local", "api", "http", "redirect", "proxy", "rate_limit",
                                    "server", "timeout", "tls", "dns", "transport", "invalid_response",
                                    "credential_format"})


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)) or len(str(value)) > 128:
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if (not number.is_finite() or abs(number) > Decimal("1e30")
            or not -30 <= number.as_tuple().exponent <= 30):
        return None
    return format(number, "f")


def _timestamp(value):
    return value if type(value) is int and 0 <= value <= 32_503_680_000_000 else None


def _tls_failure(category, value):
    """Only accept a known TLS subtype paired with its safe parent category."""
    if type(category) is not str or type(value) is not str:
        return None
    allowed = {"tls": {"certificate", "handshake"}, "transport": {"unexpected_eof"}}
    return value if value in allowed.get(category, set()) else None


def _connection(value):
    if (not isinstance(value, dict) or type(value.get("state")) is not str
            or value["state"] not in {"checking", "connected", "retrying", "failed", "blocked"}):
        return None
    result = {"state": value["state"]}
    for field in ("attempt", "max_attempts"):
        if type(value.get(field)) is int and 0 <= value[field] <= 3:
            result[field] = value[field]
    category = value.get("category")
    if type(category) is str and category in _CONNECTION_CATEGORIES:
        result["category"] = category
        subtype = _tls_failure(category, value.get("tls_failure"))
        if subtype is not None:
            result["tls_failure"] = subtype
    api_code = value.get("api_code")
    if type(api_code) in (str, int) and re.fullmatch(r"[0-9]{1,12}", str(api_code)):
        result["api_code"] = str(api_code)
    http_status = value.get("http_status")
    if type(http_status) is int and 100 <= http_status <= 599:
        result["http_status"] = http_status
    return result


def _halt_message(state):
    if not state.get("halted"):
        return None
    code = state.get("halt_api_code")
    if isinstance(code, str) and re.fullmatch(r"[0-9]{1,12}", code):
        meanings = {"50110": "请求来源 IP 未通过 API 白名单校验。", "50101": "API 与模拟盘环境不匹配。",
                    "50102": "请求时间校验失败，请同步电脑时间。", "50105": "API Passphrase 校验失败。",
                    "50111": "API Key 校验失败。", "50113": "请求签名校验失败。",
                    "50011": "读取请求持续受到限流。", "50004": "读取请求持续超时。",
                    "50013": "欧易服务暂时不可用，读取重试仍未成功。"}
        return meanings.get(code, f"欧易返回 API 错误代码 {code}。")
    category = state.get("halt_category")
    subtype = _tls_failure(category, state.get("halt_tls_failure"))
    tls_meanings = {
        "certificate": "HTTPS 证书验证失败；请检查电脑系统时间及证书链，保留证书校验。",
        "handshake": "TLS 握手失败；请检查代理连接和网络，保留证书校验。",
        "unexpected_eof": "TLS 连接被提前关闭，有限读取重试后已停止；请检查代理稳定性。订单结果未知时不会重发。",
    }
    if subtype is not None:
        return tls_meanings[subtype]
    meanings = {"timeout": "读取欧易数据持续超时，请检查代理和网络。",
                "dns": "域名解析失败，请检查代理和网络。",
                "transport": "读取欧易数据时连接失败，请检查代理和网络。",
                "tls": "HTTPS 证书校验或握手失败；请检查时间、证书和网络，保留证书校验。",
                "server": "欧易服务暂时不可用，有限读取重试后仍未恢复。",
                "rate_limit": "读取请求持续受到限流。",
                "invalid_response": "收到无效数据，机器人已停止，不能将其当作空仓。",
                "credential_format": "API 凭证格式无法用于请求，请在本机核对。"}
    if type(category) is str and category in meanings:
        return meanings[category]
    reason = state.get("halt_reason")
    known = {"OKX HTTPS request failed; check network, time and demo credentials": "旧版记录了 HTTPS 查询失败，具体网络类型未记录；这不表示已经确认密钥失效。",
             "Read verification time budget exceeded; inspect exchange state": "账户或订单读取核对超过时间预算，请检查网络并核对欧易状态。",
             "Session drawdown limit reached; new entries stopped; any reported position remains open": "会话权益触及回撤阈值，已停止新增交易；已有仓位不会自动平仓。",
             "Missed candle interval; restart safely instead of trading old signals": "行情 K 线出现跨周期缺口，机器人停止并保留核对要求。"}
    return known.get(reason, "机器人记录异常停止，请查看启动窗口并到欧易核对订单与保护单。") if type(reason) is str else "机器人记录异常停止，请到欧易核对订单与保护单。"


def _read(path):
    with path.open("rb") as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError()
    return json.loads(raw)


class DemoMonitor:
    def __init__(self, status_file: Path):
        self.status_file = Path(status_file)
        if self.status_file.name != "status.json":
            raise ValueError("请选择正在运行机器人 .runtime-demo 中的 status.json")
        self.curve = deque(maxlen=2000)
        self.lock = threading.Lock()

    def _profile(self):
        result = {}
        try:
            # Fixed sibling config filename only; unknown fields never leave.
            with (self.status_file.parent.parent / "config.paper.toml").open("rb") as stream:
                raw = stream.read(64 * 1024 + 1)
            if len(raw) > 64 * 1024:
                return result
            config = tomllib.loads(raw.decode("utf-8"))
            instrument = config.get("instrument")
            if isinstance(instrument, str) and re.fullmatch(r"[A-Z0-9]{2,20}-USDT-SWAP", instrument):
                result["instrument"] = instrument
            if config.get("bar") in {"1m", "3m", "5m", "15m", "30m", "1H", "2H", "4H"}:
                result["bar"] = config["bar"]
            for key in ("fast_window", "slow_window"):
                if type(config.get(key)) is int and 2 <= config[key] <= 200:
                    result[key] = config[key]
            for key in ("stop_loss_pct", "take_profit_pct", "max_drawdown_pct", "max_notional"):
                number = _number(config.get(key))
                if number is not None and Decimal(number) > 0:
                    result[key] = number
        except (OSError, ValueError, UnicodeError):
            pass
        return result

    def _journal(self):
        output = []
        try:
            journal = _read(self.status_file.with_name("demo-journal.json"))
            if not isinstance(journal, dict) or journal.get("version") != 1 or not isinstance(journal.get("orders"), list):
                raise ValueError()
            for row in reversed(journal["orders"][-20:]):
                if not isinstance(row, dict):
                    continue
                item = {}
                if row.get("side") in {"long", "short"}:
                    item["side"] = row["side"]
                for key in ("size", "stop_loss", "take_profit"):
                    item[key] = _number(row.get(key))
                for key in ("created_at_ms", "closed_at_ms"):
                    item[key] = _timestamp(row.get(key))
                status = row.get("status")
                item["status"] = status if status in {"intent", "accepted", "protected", "closed", "inspect_required"} else "unknown"
                if row.get("never_submitted") is True:
                    item["status"] = "never_submitted"
                order_id = row.get("order_id")
                if isinstance(order_id, str) and re.fullmatch(r"[0-9]{1,40}", order_id):
                    item["order_id"] = order_id
                output.append(item)
            return output, None
        except FileNotFoundError:
            return [], None
        except (OSError, ValueError, TypeError, RecursionError):
            return [], "订单日志暂不可读取，请以欧易模拟盘记录为准。"

    def snapshot(self, now_ms=None):
        now = time.time_ns() // 1_000_000 if now_ms is None else now_ms
        try:
            state = _read(self.status_file)
            if (not isinstance(state, dict) or state.get("mode") != "demo"
                    or type(state.get("halted")) is not bool
                    or _timestamp(state.get("updated_at_ms")) is None
                    or type(state.get("entries")) is not int or state["entries"] < 0):
                raise ValueError()
            updated = state["updated_at_ms"]
            age = now - updated
            stale = age > MAX_AGE_MS or age < -5_000
            connection = _connection(state.get("connection"))
            phase = state.get("phase") if state.get("phase") in {"starting", "running", "stopped", "halted"} else None
            last_success = _timestamp(state.get("last_success_at_ms"))
            position = state.get("position")
            clean_position = None
            if isinstance(position, dict):
                clean_position = {"side": position.get("side") if position.get("side") in {"long", "short"} else "unknown",
                                  "contracts": _number(position.get("contracts")),
                                  "entry_price": _number(position.get("entry_price")),
                                  "uncertain": bool(position.get("status"))}
            elif position is not None:
                raise ValueError()
            equity = _number(state.get("equity"))
            sample_time = last_success if last_success is not None else updated
            sample_age = now - sample_time
            sample_valid = (not state["halted"] and not stale and -5_000 <= sample_age <= MAX_AGE_MS
                            and phase != "stopped" and (connection is None or connection["state"] == "connected"))
            with self.lock:
                if equity is not None and sample_valid:
                    if not self.curve or sample_time > self.curve[-1]["ts"]:
                        self.curve.append({"ts": sample_time, "equity": equity})
                    elif sample_time < self.curve[-1]["ts"]:
                        self.curve.clear()
                        self.curve.append({"ts": sample_time, "equity": equity})
                curve = list(self.curve)
            orders, warning = self._journal()
            return {"ok": True, "mode": "demo", "halted": state["halted"],
                    "stale": stale, "updated_at_ms": updated, "age_ms": max(0, age),
                    "equity": equity, "entries": state["entries"], "position": clean_position,
                    "phase": phase, "connection": connection, "last_success_at_ms": last_success,
                    "halt_message": _halt_message(state),
                    "profile": self._profile(), "orders": orders, "warning": warning,
                    "equity_curve": curve,
                    "health": ("halted" if state["halted"] else "stopped" if phase == "stopped"
                               else "stale" if stale else connection["state"] if connection is not None
                               and connection["state"] in {"retrying", "checking"} else "updating")}
        except (OSError, ValueError, TypeError, RecursionError):
            return {"ok": False, "health": "unavailable", "error": "无法读取有效的欧易模拟盘状态。请确认选择了正在运行机器人目录中的 .runtime-demo/status.json。"}


def handler_for(monitor: DemoMonitor, report_file: Path | None = None):
    nonce = secrets.token_urlsafe(24)
    html = Path(__file__).with_name("monitor.html").read_text(encoding="utf-8").replace("__NONCE__", nonce).encode()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _allowed(self):
            port = self.server.server_address[1]
            hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
            origin = self.headers.get("Origin")
            return (self.headers.get("Host") in hosts
                    and (origin is None or origin in {"http://" + host for host in hosts})
                    and self.headers.get("Sec-Fetch-Site") not in {"cross-site", "same-site"})

        def _send(self, code, data, content_type):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            policy = f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
            if self.path == "/report":
                policy = "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
            self.send_header("Content-Security-Policy", policy)
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if not self._allowed():
                return self._send(403, b"Forbidden", "text/plain; charset=utf-8")
            if self.path == "/":
                return self._send(200, html, "text/html; charset=utf-8")
            if self.path == "/api/status":
                data = json.dumps(monitor.snapshot(), ensure_ascii=False, allow_nan=False).encode()
                return self._send(200, data, "application/json; charset=utf-8")
            if self.path == "/report" and report_file is not None:
                try:
                    with report_file.open("rb") as stream:
                        report = stream.read(16 * 1024 * 1024 + 1)
                    if len(report) > 16 * 1024 * 1024:
                        raise ValueError()
                    return self._send(200, report, "text/html; charset=utf-8")
                except (OSError, ValueError):
                    return self._send(404, "尚未生成回测报告。请先运行 backtest-windows.bat。".encode(), "text/plain; charset=utf-8")
            self._send(404, b"Not found", "text/plain; charset=utf-8")

        def do_POST(self):
            self._send(405, b"Read only", "text/plain; charset=utf-8")

    return Handler


def main(argv=None):
    parser = argparse.ArgumentParser(description="量化实验室：只读查看正在运行的欧易模拟盘，不启动新机器人")
    parser.add_argument("--status-file", type=Path, default=Path(".runtime-demo/status.json"))
    parser.add_argument("--report-file", type=Path, default=Path(".runtime-backtest/report.html"))
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args(argv)
    try:
        if not 1 <= args.port <= 65535:
            raise ValueError()
        monitor = DemoMonitor(args.status_file)
        with ThreadingHTTPServer(("127.0.0.1", args.port), handler_for(monitor, args.report_file)) as server:
            print(f"欧易模拟盘监控已启动，请在浏览器打开 http://127.0.0.1:{args.port}", flush=True)
            print("保持此窗口运行。关闭网页不影响机器人；关闭此监控窗口也不会停止机器人。", flush=True)
            print("本网页只读取已选状态文件，不需要密钥，不启动新机器人、不提交订单。", flush=True)
            server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        return 0
    except Exception:
        print("监控网页未启动。请检查状态文件选择、端口占用和 Python 版本；现有机器人不受影响。")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
