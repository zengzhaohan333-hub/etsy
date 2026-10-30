"""Read-only production-public OKX history and validated, reusable snapshots.

This client intentionally does not use the demo client or any credentials. The
SDK's history-candles/funding parameters and CCXT's history-mark-price-candles
implementation were cross-checked; actual endpoint availability still needs a
successful public GET from the user's machine. Missing mark data never falls
back to traded prices. Funding checks detect gaps above eight hours, not every
possible omission when the exchange changes its settlement interval.
"""
from __future__ import annotations

import hashlib
import http.client
import errno
import json
import math
import os
import re
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable

from .config import Config
from .okx import Candle, Instrument


class HistoryError(RuntimeError):
    """Only locally generated messages and validated numeric codes are exposed."""

    CATEGORIES = frozenset({"local", "timeout", "connection", "dns", "tls",
                            "http", "api", "invalid_response"})

    def __init__(self, message: str, *, category: str = "local",
                 http_status: Any = None, api_code: Any = None,
                 tls_failure: Any = None) -> None:
        super().__init__(message)
        self.category = category if type(category) is str and category in self.CATEGORIES else "local"
        self.http_status = http_status if type(http_status) is int and 100 <= http_status <= 599 else None
        self.api_code = (str(api_code) if type(api_code) in (str, int)
                         and re.fullmatch(r"[0-9]{1,12}", str(api_code)) else None)
        valid_tls = (type(tls_failure) is str
                     and ((self.category == "tls" and tls_failure in {"certificate", "handshake"})
                          or (self.category == "connection" and tls_failure == "unexpected_eof")))
        self.tls_failure = tls_failure if valid_tls else None


BASE_URL = "https://www.okx.com"
ENDPOINTS = {
    "trade": "/api/v5/market/history-candles",
    "mark": "/api/v5/market/history-mark-price-candles",
    "funding": "/api/v5/public/funding-rate-history",
    "instrument": "/api/v5/public/instruments",
}
INTERVALS = {"1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000,
             "30m": 1_800_000, "1H": 3_600_000, "2H": 7_200_000, "4H": 14_400_000}
DAY_MS = 86_400_000
FUNDING_MAX_GAP_MS = 8 * 3_600_000
FUNDING_COVERAGE_RULE = "preceding_anchor_and_nonempty_events_with_gaps_at_most_8h"
MAX_CACHE_BYTES = 64 * 1024 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_BARS = 200_000
MAX_WARMUP_BARS = 1200
MAX_TIMESTAMP_MS = 32_503_680_000_000


@dataclass(frozen=True)
class HistoryDataset:
    trade_candles: list[Candle]
    mark_candles: list[Candle]
    funding_rates: list[tuple[int, Decimal]]
    instrument: Instrument
    start_ms: int
    end_ms: int
    retrieved_at_ms: int
    bar: str = "15m"
    warmup_bars: int = 21
    funding_before: tuple[int, Decimal] | None = None
    funding_at_end: tuple[int, Decimal] | None = None


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _timestamp(value: Any) -> int:
    if type(value) is str and re.fullmatch(r"[0-9]{1,16}", value):
        value = int(value)
    if type(value) is not int or not 0 <= value <= MAX_TIMESTAMP_MS:
        raise HistoryError("历史数据包含无效时间戳，已拒绝使用。")
    return value


def _decimal(value: Any, *, positive: bool = False, rate: bool = False) -> Decimal:
    if type(value) not in (str, Decimal) or len(str(value)) > 128:
        raise HistoryError("历史数据包含无效十进制数，已拒绝使用。")
    try:
        number = Decimal(value)
    except (InvalidOperation, ValueError):
        raise HistoryError("历史数据包含无效十进制数，已拒绝使用。") from None
    if (not number.is_finite() or (positive and number <= 0)
            or abs(number) > (Decimal(1) if rate else Decimal("1e30"))
            or (number != 0 and not -30 <= number.adjusted() <= 30)
            or not -30 <= number.as_tuple().exponent <= 30):
        raise HistoryError("历史数据包含非有限或超出范围的数值，已拒绝使用。")
    return number


def _inst_id(value: Any) -> str:
    if type(value) is not str or not re.fullmatch(r"[A-Z0-9]{2,20}-USDT-SWAP", value):
        raise HistoryError("历史数据仅支持 USDT 线性永续合约。")
    return value


def _reject_constant(_value: str) -> None:
    raise ValueError()


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError()
        result[key] = value
    return result


def _json(raw: bytes) -> Any:
    try:
        return json.loads(raw, parse_constant=_reject_constant, object_pairs_hook=_unique_object)
    except (ValueError, TypeError, RecursionError):
        raise HistoryError("未收到有效的历史数据 JSON；不会使用不完整数据。") from None


def _numeric_code(payload: Any) -> str | None:
    value = payload.get("code") if isinstance(payload, dict) else None
    if type(value) in (int, str) and re.fullmatch(r"[0-9]{1,12}", str(value)):
        return str(value)
    return None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> None:
        return None


def _transport_details(error: BaseException) -> tuple[str, str | None, bool]:
    """Classify one typed urllib reason without retaining or reading its text.

    Unknown OSErrors/string reasons are not proof of a transient connection
    failure. TLS verification and unknown SSL failures always fail closed.
    """
    cause = error.reason if isinstance(error, urllib.error.URLError) else error
    if isinstance(cause, ssl.SSLCertVerificationError):
        return "tls", "certificate", False
    if isinstance(cause, ssl.SSLEOFError):
        return "connection", "unexpected_eof", True
    if isinstance(cause, ssl.SSLError):
        return "tls", "handshake", False
    if isinstance(cause, (TimeoutError, socket.timeout)):
        return "timeout", None, True
    if isinstance(cause, socket.gaierror):
        return "dns", None, cause.errno == socket.EAI_AGAIN
    if isinstance(cause, (ConnectionResetError, ConnectionAbortedError,
                          BrokenPipeError, ConnectionRefusedError,
                          http.client.RemoteDisconnected, http.client.IncompleteRead)):
        return "connection", None, True
    if isinstance(cause, OSError):
        error_number = cause.errno if type(cause.errno) is int else None
        if error_number == errno.ETIMEDOUT:
            return "timeout", None, True
        if error_number in {errno.ECONNRESET, errno.ECONNABORTED, errno.ECONNREFUSED,
                            errno.EPIPE, getattr(errno, "ENETRESET", -1)}:
            return "connection", None, True
    if isinstance(cause, (ValueError, http.client.HTTPException)):
        return "invalid_response", None, False
    return "connection", None, False


def _transport_message(category: str, tls_failure: str | None, attempts: int) -> str:
    messages = {
        "timeout": "欧易公开历史请求连接超时",
        "connection": "欧易公开历史连接中断或不可用",
        "dns": "欧易公开历史域名解析未完成",
        "invalid_response": "欧易公开历史连接未返回有效的 HTTP 数据",
    }
    if tls_failure == "certificate":
        return "欧易公开历史 HTTPS 证书验证失败；请核对系统时间和证书链，保留证书校验。"
    if tls_failure == "handshake":
        return "欧易公开历史 TLS 握手失败；请检查网络和代理，保留证书校验。"
    if tls_failure == "unexpected_eof":
        return f"欧易公开历史 TLS 连接被提前关闭（已尝试 {attempts} 次）；请检查代理连接稳定性。"
    return f"{messages.get(category, messages['connection'])}（已尝试 {attempts} 次）；没有使用不完整行情。"


class PublicHistoryClient:
    """GET-only, fixed production host, no authentication or simulation headers."""

    def __init__(self, *, timeout: float = 15, max_attempts: int = 3,
                 opener: Callable[..., Any] | None = None,
                 sleep: Callable[[float], Any] = time.sleep,
                 progress: Callable[[str], Any] | None = None) -> None:
        if (type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 30
                or type(max_attempts) is not int or not 1 <= max_attempts <= 3):
            raise HistoryError("历史下载超时或重试次数设置无效。")
        self.timeout, self.max_attempts = timeout, max_attempts
        self._open = opener or urllib.request.build_opener(_NoRedirect()).open
        self._sleep, self._progress = sleep, progress
        self._last_request: float | None = None

    def get(self, endpoint: str, params: dict[str, Any]) -> list[Any]:
        if endpoint not in ENDPOINTS.values() or BASE_URL != "https://www.okx.com":
            raise HistoryError("历史下载仅允许固定欧易公开接口。")
        allowed = {"instId", "instType"} if endpoint == ENDPOINTS["instrument"] else {"instId", "after", "before", "limit"}
        if endpoint in (ENDPOINTS["trade"], ENDPOINTS["mark"]):
            allowed.add("bar")
        if not isinstance(params, dict) or set(params) - allowed or "instId" not in params:
            raise HistoryError("历史下载参数无效。")
        _inst_id(params["instId"])
        for key in ("after", "before"):
            if key in params:
                _timestamp(params[key])
        if ("bar" in params and params["bar"] not in INTERVALS) or ("limit" in params and str(params["limit"]) != "100"):
            raise HistoryError("历史下载周期或分页大小无效。")
        if endpoint == ENDPOINTS["instrument"] and params.get("instType") != "SWAP":
            raise HistoryError("历史下载仅允许永续合约元数据。")
        url = BASE_URL + endpoint + "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(url, method="GET", headers={"Accept": "application/json", "User-Agent": "okx-history-readonly/1"})
        for attempt in range(self.max_attempts):
            if self._last_request is not None:
                self._sleep(max(0, 0.5 - (time.monotonic() - self._last_request)))
            self._last_request = time.monotonic()
            status, raw, http_failed = None, b"", False
            transport = None
            try:
                with self._open(request, timeout=self.timeout) as response:
                    status = getattr(response, "status", None)
                    raw = response.read(MAX_RESPONSE_BYTES + 1)
            except urllib.error.HTTPError as error:
                http_failed = True
                status = error.code if type(error.code) is int and 100 <= error.code <= 599 else None
                try:
                    raw = error.read(MAX_RESPONSE_BYTES + 1)
                except (OSError, ValueError, http.client.HTTPException) as read_error:
                    transport = _transport_details(read_error)
                    raw = b""
                finally:
                    try:
                        error.close()
                    except (OSError, ValueError):
                        pass
            except (OSError, ValueError, http.client.HTTPException) as error:
                transport = _transport_details(error)
            if not isinstance(raw, bytes) or len(raw) > MAX_RESPONSE_BYTES:
                raise HistoryError("历史响应过大，已停止下载。", category="invalid_response")
            if http_failed and status is None:
                raise HistoryError("公开历史请求发生无法识别的 HTTP 错误。", category="invalid_response")
            if status is not None and (type(status) is not int or not 100 <= status <= 599):
                raise HistoryError("历史接口返回无效 HTTP 状态。", category="invalid_response")
            if transport is not None:
                category, tls_failure, transient = transport
                # A certificate/handshake failure cannot be relabelled as a
                # recoverable 503. Conversely a 401/403/redirect must never be
                # retried just because reading its error body disconnected.
                permanent_http = (type(status) is int and not 200 <= status < 300
                                  and not (status == 429 or status >= 500))
                if category == "tls" or not permanent_http:
                    if transient and attempt + 1 < self.max_attempts:
                        if self._progress:
                            self._progress(f"公开历史连接暂时中断，将进行第 {attempt + 2}/{self.max_attempts} 次只读重试。")
                        self._sleep(attempt + 1)
                        continue
                    raise HistoryError(_transport_message(category, tls_failure, attempt + 1),
                                       category=category, http_status=status, tls_failure=tls_failure) from None
            if type(status) is int and not 200 <= status < 300:
                transient = status == 429 or status >= 500
                error_code = None
                try:
                    error_code = _numeric_code(_json(raw))
                except HistoryError:
                    pass
                if error_code == "0":
                    error_code = None  # An HTTP error body cannot grant success.
                if error_code is not None and error_code not in {"50001", "50004", "50011", "50013"}:
                    transient = False
                if transient and attempt + 1 < self.max_attempts:
                    if self._progress:
                        self._progress("公开历史接口暂时限流或不可用，将有限重试。")
                    self._sleep(attempt + 1)
                    continue
                raise HistoryError(f"欧易公开历史请求返回 HTTP {status}；未使用响应中的任何行情。",
                                   category="http", http_status=status, api_code=error_code)
            try:
                payload = _json(raw)
            except HistoryError:
                raise HistoryError("未收到有效的历史数据 JSON；不会使用不完整数据。",
                                   category="invalid_response", http_status=status) from None
            code = _numeric_code(payload)
            if code != "0":
                if code in {"50001", "50004", "50011", "50013"} and attempt + 1 < self.max_attempts:
                    if self._progress:
                        self._progress("公开历史接口暂时限流或不可用，将有限重试。")
                    self._sleep(attempt + 1)
                    continue
                suffix = f"（代码 {code}）" if code else ""
                raise HistoryError(f"欧易公开历史接口拒绝请求{suffix}；不会用替代数据继续。",
                                   category="api" if code is not None else "invalid_response",
                                   http_status=status, api_code=code)
            if not isinstance(payload, dict) or not isinstance(payload.get("data"), list) or len(payload["data"]) > 100:
                raise HistoryError("历史接口返回的数据结构或分页大小无效。",
                                   category="invalid_response", http_status=status)
            return payload["data"]
        raise HistoryError("欧易公开历史重试次数已用完。")


def _instrument(row: Any) -> Instrument:
    if not isinstance(row, dict):
        raise HistoryError("合约元数据格式无效。")
    inst_id = _inst_id(row.get("instId"))
    base = inst_id.split("-")[0]
    if (row.get("instType") != "SWAP" or row.get("ctType") != "linear" or row.get("ctValCcy") != base
            or row.get("settleCcy") != "USDT" or row.get("state") != "live"):
        raise HistoryError("合约不是当前可用的 USDT 线性永续合约。")
    instrument = Instrument(inst_id, *(_decimal(row.get(name), positive=True) for name in
        ("ctVal", "lotSz", "minSz", "maxMktSz", "tickSz")), base, "USDT", "live")
    if instrument.max_market_size < instrument.min_size:
        raise HistoryError("合约最小和最大数量不一致。")
    return instrument


def _candle(row: Any, confirm_index: int, interval: int) -> Candle | None:
    if (not isinstance(row, list) or len(row) != confirm_index + 1
            or type(row[confirm_index]) is not str or row[confirm_index] not in {"0", "1"}):
        raise HistoryError("历史 K 线字段或收盘确认状态无效。")
    ts = _timestamp(row[0])
    if ts % interval:
        raise HistoryError("历史 K 线时间未对齐指定周期。")
    o, h, low, c = (_decimal(row[index], positive=True) for index in range(1, 5))
    if h < max(o, c, low) or low > min(o, c, h):
        raise HistoryError("历史 K 线 OHLC 价格关系无效。")
    if any(_decimal(value) < 0 for value in row[5:confirm_index]):
        raise HistoryError("成交量字段无效。")
    return Candle(ts, o, h, low, c) if row[confirm_index] == "1" else None


def _fetch_candles(client: PublicHistoryClient, endpoint: str, inst_id: str, bar: str,
                   first_ms: int, end_ms: int, progress: Callable[[str], Any] | None,
                   cache: Any = None) -> list[Candle]:
    interval = INTERVALS[bar]
    expected = (end_ms - first_ms) // interval
    collected: dict[int, Candle] = {}
    cursor = end_ms
    for _page in range((expected + 99) // 100 + 10):
        rows = cache.load(cursor) if cache is not None else None
        cached = rows is not None
        if not cached:
            rows = client.get(endpoint, {"instId": inst_id, "bar": bar, "after": str(cursor), "limit": "100"})
        if not isinstance(rows, list) or not 1 <= len(rows) <= 100:
            raise HistoryError("历史 K 线未覆盖所需区间，已停止；不会插值补齐。")
        timestamps: list[int] = []
        for row in rows:
            candle = _candle(row, 8 if endpoint == ENDPOINTS["trade"] else 5, interval)
            ts = _timestamp(row[0])
            if ts > cursor or (timestamps and ts > timestamps[-1]):
                raise HistoryError("历史接口分页顺序或方向无效。")
            timestamps.append(ts)
            if candle is None and first_ms <= ts < end_ms:
                raise HistoryError("所需历史 K 线尚未确认收盘，该页未保存；请稍后重新运行。")
            if candle is not None and first_ms <= ts < end_ms:
                if ts in collected and collected[ts] != candle:
                    raise HistoryError("相同时间的历史 K 线内容冲突，已拒绝。")
                collected[ts] = candle
        oldest = min(timestamps)
        if oldest >= cursor:
            raise HistoryError("历史分页游标没有向前推进，已停止。")
        # Each accepted page must extend the already validated newest prefix.
        # Never persist a cursor past a missing or unconfirmed wanted candle.
        if any(ts not in collected for ts in range(max(oldest, first_ms), min(cursor, end_ms), interval)):
            raise HistoryError("历史 K 线分页存在缺口，该页未保存；不会跳过或插值。")
        if cache is not None and not cached:
            cache.save(cursor, rows)
        if progress:
            kind = "成交价" if endpoint == ENDPOINTS["trade"] else "标记价格"
            action = "已校验缓存" if cached else "已下载并保存" if cache is not None else "已下载"
            progress(f"{action}{kind}历史 K 线 {len(collected)}/{expected} 根。")
        if oldest <= first_ms:
            break
        cursor = oldest
    candles = [collected[ts] for ts in sorted(collected)]
    if len(candles) != expected or [c.ts for c in candles] != list(range(first_ms, end_ms, interval)):
        raise HistoryError("历史 K 线存在缺口或未确认收盘，已拒绝使用；不会插值。")
    return candles


def _funding_row(row: Any, inst_id: str) -> tuple[int, Decimal]:
    if not isinstance(row, dict) or row.get("instId") != inst_id:
        raise HistoryError("历史资金费率的合约标识无效。")
    timestamp = _timestamp(row.get("fundingTime"))
    realized = row.get("realizedRate")
    rate = realized if realized is not None and realized != "" else row.get("fundingRate")
    return timestamp, _decimal(rate, rate=True)


def _fetch_funding(client: PublicHistoryClient, inst_id: str, start_ms: int, end_ms: int,
                   cache: Any = None) -> tuple[list[tuple[int, Decimal]], tuple[int, Decimal], tuple[int, Decimal] | None]:
    cursor = end_ms + 1
    collected: dict[int, Decimal] = {}
    # Even hourly settlement needs <= 2161 events for the 90-day maximum.
    for _page in range(100):
        rows = cache.load(cursor) if cache is not None else None
        cached = rows is not None
        if not cached:
            rows = client.get(ENDPOINTS["funding"], {"instId": inst_id, "after": str(cursor), "limit": "100"})
        if not isinstance(rows, list) or not 1 <= len(rows) <= 100:
            raise HistoryError("资金费率历史未覆盖测量起点；不能将缺失数据当作零。")
        timestamps: list[int] = []
        for row in rows:
            timestamp, rate = _funding_row(row, inst_id)
            if timestamp > cursor or (timestamps and timestamp > timestamps[-1]):
                raise HistoryError("资金费率历史分页顺序或方向无效。")
            timestamps.append(timestamp)
            if timestamp <= end_ms:
                if timestamp in collected and collected[timestamp] != rate:
                    raise HistoryError("相同时间的资金费率历史内容冲突。")
                collected[timestamp] = rate
        oldest = min(timestamps)
        if oldest >= cursor:
            raise HistoryError("资金费率历史分页没有推进。")
        if cache is not None and not cached:
            cache.save(cursor, rows)
        if oldest < start_ms:
            break
        cursor = oldest
    before_times = [ts for ts in collected if ts < start_ms]
    if not before_times:
        raise HistoryError("缺少测量起点之前的资金费率锚点，覆盖范围无法验证。")
    before_ts = max(before_times)
    events = [(ts, collected[ts]) for ts in sorted(collected) if start_ms <= ts < end_ms]
    at_end = (end_ms, collected[end_ms]) if end_ms in collected else None
    return events, (before_ts, collected[before_ts]), at_end


def _validate(dataset: HistoryDataset) -> None:
    if not isinstance(dataset, HistoryDataset) or type(dataset.bar) is not str or dataset.bar not in INTERVALS:
        raise HistoryError("历史快照的数据类型或周期无效。")
    interval = INTERVALS[dataset.bar]
    if any(type(value) is not int for value in (dataset.start_ms, dataset.end_ms, dataset.retrieved_at_ms)):
        raise HistoryError("历史快照时间元数据必须是整数毫秒。")
    start, end, retrieved = (_timestamp(value) for value in (dataset.start_ms, dataset.end_ms, dataset.retrieved_at_ms))
    if (start >= end or start % interval or end % interval or end > retrieved
            or not DAY_MS <= end - start <= 90 * DAY_MS or (end - start) % DAY_MS):
        raise HistoryError("历史快照测量区间或抓取时间无效。")
    if type(dataset.warmup_bars) is not int or not 3 <= dataset.warmup_bars <= MAX_WARMUP_BARS:
        raise HistoryError("历史快照预热 K 线数量无效。")
    expected = (end - start) // interval + dataset.warmup_bars
    if expected > MAX_BARS:
        raise HistoryError("历史快照 K 线数量超出上限。")
    first = start - dataset.warmup_bars * interval
    if first < 0 or not isinstance(dataset.instrument, Instrument):
        raise HistoryError("历史快照合约或预热起点无效。")
    # Reuse the same active/linear/contract-unit checks when loading a cache.
    validated = _instrument(_instrument_dict(dataset.instrument))
    if validated != dataset.instrument:
        raise HistoryError("历史快照合约元数据不一致。")
    for series in (dataset.trade_candles, dataset.mark_candles):
        if not isinstance(series, list) or len(series) != expected:
            raise HistoryError("成交价或标记价格历史未完整覆盖测量及预热区间。")
        for index, candle in enumerate(series):
            if not isinstance(candle, Candle) or type(candle.ts) is not int or candle.ts != first + index * interval:
                raise HistoryError("历史快照 K 线顺序、时间戳或连续性无效。")
            o, h, low, c = (_decimal(value, positive=True) for value in (candle.open, candle.high, candle.low, candle.close))
            if h < max(o, c, low) or low > min(o, c, h):
                raise HistoryError("历史快照 K 线 OHLC 价格关系无效。")
    if not isinstance(dataset.funding_rates, list) or not dataset.funding_rates:
        raise HistoryError("测量区间没有资金费率历史，不能按零资金费率回测。")
    if dataset.funding_before is None:
        raise HistoryError("历史快照缺少资金费率覆盖锚点。")
    all_events = [dataset.funding_before, *dataset.funding_rates]
    if dataset.funding_at_end is not None:
        all_events.append(dataset.funding_at_end)
    previous = None
    for index, event in enumerate(all_events):
        if not isinstance(event, (tuple, list)) or len(event) != 2 or type(event[0]) is not int:
            raise HistoryError("历史资金费率事件格式无效。")
        ts = _timestamp(event[0])
        _decimal(event[1], rate=True)
        if ((index == 0 and not 0 <= ts < start)
                or (index > 0 and event is not dataset.funding_at_end and not start <= ts < end)
                or (event is dataset.funding_at_end and ts != end)
                or (previous is not None and not 0 < ts - previous <= FUNDING_MAX_GAP_MS)):
            raise HistoryError("资金费率历史存在顺序、边界或超过 8 小时的覆盖缺口。")
        previous = ts
    if start - dataset.funding_before[0] > FUNDING_MAX_GAP_MS or end - all_events[-1][0] > FUNDING_MAX_GAP_MS:
        raise HistoryError("资金费率历史未覆盖测量区间边界。")


def fetch_dataset(config: Config, days: int = 30, now_ms: int | None = None,
                  progress: Callable[[str], Any] | None = None, *,
                  warmup_bars: int | None = None,
                  checkpoint_dir: Path | None = None) -> HistoryDataset:
    if type(days) is not int or not 1 <= days <= 90:
        raise HistoryError("历史回测天数必须是 1 到 90 的整数。")
    inst_id = _inst_id(config.instrument)
    if config.bar not in INTERVALS:
        raise HistoryError("历史回测 K 线周期不受支持。")
    interval = INTERVALS[config.bar]
    now = _timestamp(_now_ms() if now_ms is None else now_ms)
    end = now // interval * interval
    warmup = config.slow_window + 1 if warmup_bars is None else warmup_bars
    if type(warmup) is not int or not config.slow_window + 1 <= warmup <= MAX_WARMUP_BARS:
        raise HistoryError("历史预热 K 线数量必须至少满足策略窗口，且不超过 1200。")
    start = end - days * DAY_MS
    first = start - warmup * interval
    if first < 0 or (end - first) // interval > MAX_BARS:
        raise HistoryError("历史回测起点或数据量超出支持范围。")
    client = PublicHistoryClient(progress=progress)
    rows = client.get(ENDPOINTS["instrument"], {"instType": "SWAP", "instId": inst_id})
    if len(rows) != 1:
        raise HistoryError("无法取得唯一的当前合约元数据。")
    instrument = _instrument(rows[0])
    if instrument.inst_id != inst_id:
        raise HistoryError("公开接口返回了其他合约元数据。")
    from .history_cache import HistoryPageCache
    caches = {}
    if checkpoint_dir is not None:
        for kind in ("trade", "mark", "funding"):
            caches[kind] = HistoryPageCache(checkpoint_dir, inst_id=inst_id, bar=config.bar,
                start_ms=start if kind == "funding" else first, end_ms=end, kind=kind)
    trade = _fetch_candles(client, ENDPOINTS["trade"], inst_id, config.bar, first, end, progress, caches.get("trade"))
    mark = _fetch_candles(client, ENDPOINTS["mark"], inst_id, config.bar, first, end, progress, caches.get("mark"))
    funding, before, at_end = _fetch_funding(client, inst_id, start, end, caches.get("funding"))
    dataset = HistoryDataset(trade, mark, funding, instrument, start, end, _now_ms(), config.bar, warmup, before, at_end)
    _validate(dataset)
    if progress:
        progress("公开历史快照已完成连续性、成交价/标记价格对齐和资金费率覆盖检查。")
    return dataset


def _instrument_dict(instrument: Instrument) -> dict[str, str]:
    return {"instId": instrument.inst_id, "instType": "SWAP", "ctType": "linear", "ctValCcy": instrument.ct_val_ccy,
            "settleCcy": instrument.settle_ccy, "state": instrument.state, "ctVal": str(instrument.ct_val),
            "lotSz": str(instrument.lot_size), "minSz": str(instrument.min_size),
            "maxMktSz": str(instrument.max_market_size), "tickSz": str(instrument.tick_size)}


def _candle_data(candles: list[Candle]) -> list[list[Any]]:
    return [[c.ts, str(c.open), str(c.high), str(c.low), str(c.close)] for c in candles]


def _event_data(event: tuple[int, Decimal] | None) -> list[Any] | None:
    return [event[0], str(event[1])] if event is not None else None


def _canonical(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("utf-8")


def save_dataset(dataset: HistoryDataset, path: Path) -> None:
    _validate(dataset)
    payload = {"version": 1, "provenance": "production_public",
        "source": {"host": BASE_URL, "endpoints": dict(ENDPOINTS)},
        "snapshot": {"start_ms": dataset.start_ms, "end_ms": dataset.end_ms, "retrieved_at_ms": dataset.retrieved_at_ms,
            "bar": dataset.bar, "instId": dataset.instrument.inst_id, "warmup_bars": dataset.warmup_bars,
            "funding_before": _event_data(dataset.funding_before), "funding_at_end": _event_data(dataset.funding_at_end),
            "funding_coverage_rule": FUNDING_COVERAGE_RULE, "funding_rate_preference": "realizedRate_then_fundingRate"},
        "instrument": _instrument_dict(dataset.instrument), "trade_candles": _candle_data(dataset.trade_candles),
        "mark_candles": _candle_data(dataset.mark_candles), "funding_rates": [_event_data(event) for event in dataset.funding_rates]}
    payload["snapshot"]["sha256"] = hashlib.sha256(_canonical(payload)).hexdigest()
    encoded = _canonical(payload)
    if len(encoded) > MAX_CACHE_BYTES:
        raise HistoryError("历史快照超过 64 MiB，未保存。")
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with temporary.open("xb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError:
        raise HistoryError("无法写入历史快照文件；没有修改交易配置。") from None
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _cached_candles(rows: Any) -> list[Candle]:
    if not isinstance(rows, list) or len(rows) > MAX_BARS:
        raise HistoryError("缓存 K 线数据量或格式无效。")
    result = []
    for row in rows:
        if not isinstance(row, list) or len(row) != 5 or type(row[0]) is not int or any(type(v) is not str for v in row[1:]):
            raise HistoryError("缓存 K 线必须使用十进制字符串。")
        result.append(Candle(_timestamp(row[0]), *(_decimal(value, positive=True) for value in row[1:])))
    return result


def _cached_event(row: Any, *, optional: bool = False) -> tuple[int, Decimal] | None:
    if optional and row is None:
        return None
    if not isinstance(row, list) or len(row) != 2 or type(row[0]) is not int or type(row[1]) is not str:
        raise HistoryError("缓存资金费率必须使用时间戳与十进制字符串。")
    return _timestamp(row[0]), _decimal(row[1], rate=True)


def load_dataset(path: Path) -> HistoryDataset:
    try:
        with Path(path).open("rb") as stream:
            raw = stream.read(MAX_CACHE_BYTES + 1)
    except OSError:
        raise HistoryError("无法读取历史快照文件。") from None
    if len(raw) > MAX_CACHE_BYTES:
        raise HistoryError("历史快照超过 64 MiB，已拒绝加载。")
    payload = _json(raw)
    try:
        if (not isinstance(payload, dict) or set(payload) != {"version", "provenance", "source", "snapshot", "instrument", "trade_candles", "mark_candles", "funding_rates"}
                or type(payload["version"]) is not int or payload["version"] != 1 or payload["provenance"] != "production_public"
                or payload["source"] != {"host": "https://www.okx.com", "endpoints": ENDPOINTS}):
            raise HistoryError("历史快照版本或 production_public 来源声明无效。")
        snapshot = payload["snapshot"]
        expected = {"start_ms", "end_ms", "retrieved_at_ms", "bar", "instId", "warmup_bars", "funding_before", "funding_at_end", "funding_coverage_rule", "funding_rate_preference", "sha256"}
        if not isinstance(snapshot, dict) or set(snapshot) != expected:
            raise HistoryError("历史快照元数据格式无效。")
        digest = snapshot.pop("sha256")
        if type(digest) is not str or not re.fullmatch(r"[0-9a-f]{64}", digest) or hashlib.sha256(_canonical(payload)).hexdigest() != digest:
            raise HistoryError("历史快照摘要不一致，已拒绝加载。")
        if (snapshot["funding_coverage_rule"] != FUNDING_COVERAGE_RULE
                or snapshot["funding_rate_preference"] != "realizedRate_then_fundingRate"):
            raise HistoryError("历史快照资金费率来源或覆盖规则无效。")
        if not isinstance(payload["funding_rates"], list) or len(payload["funding_rates"]) > MAX_BARS:
            raise HistoryError("历史快照资金费率格式或数量无效。")
        instrument = _instrument(payload["instrument"])
        if instrument.inst_id != snapshot["instId"]:
            raise HistoryError("历史快照合约标识不一致。")
        dataset = HistoryDataset(_cached_candles(payload["trade_candles"]), _cached_candles(payload["mark_candles"]),
            [_cached_event(row) for row in payload["funding_rates"]], instrument,
            snapshot["start_ms"], snapshot["end_ms"], snapshot["retrieved_at_ms"], snapshot["bar"], snapshot["warmup_bars"],
            _cached_event(snapshot["funding_before"], optional=True), _cached_event(snapshot["funding_at_end"], optional=True))
        _validate(dataset)
        return dataset
    except HistoryError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError, ArithmeticError, RecursionError):
        raise HistoryError("历史快照格式或数值无效，已拒绝加载。") from None
