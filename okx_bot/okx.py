"""Small OKX v5 client. Private API calls are permanently restricted to demo trading.

The transport deliberately never retries an order. A lost response may still mean
the exchange accepted it; callers must query by clOrdId before taking further action.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import ipaddress
import json
import os
import re
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping


class OkxError(RuntimeError):
    """A sanitized exchange, configuration or transport error."""

    _CATEGORIES = frozenset({"local", "api", "http", "redirect", "proxy", "rate_limit",
                             "server", "timeout", "tls", "dns", "transport", "invalid_response",
                             "credential_format"})

    def __init__(self, message: str, *, api_code: Any = None,
                 http_status: Any = None, category: str = "local",
                 request_ip: Any = None) -> None:
        super().__init__(message)
        # Only safe scalars are attached. Never retain a response, URL, headers,
        # body, remote message or original transport exception for diagnostics.
        self.api_code = _safe_api_code(api_code)
        self.http_status = _safe_http_status(http_status)
        self.category = category if isinstance(category, str) and category in self._CATEGORIES else "local"
        self.request_ip = _safe_request_ip(request_ip) if self.api_code == "50110" else None


class AmbiguousOrderError(OkxError):
    """An order request might have reached OKX. Reconcile; do not resend."""


def _safe_api_code(value: Any) -> str | None:
    if type(value) not in (str, int):
        return None
    text = str(value)
    return text if re.fullmatch(r"[0-9]{1,12}", text) else None


def _safe_http_status(value: Any) -> int | None:
    return value if type(value) is int and 100 <= value <= 599 else None


def _safe_request_ip(value: Any) -> str | None:
    """Validate a lone IP literal; never accept zones, CIDR, ports or context."""
    if type(value) is not str or not re.fullmatch(r"[0-9A-Fa-f:.]{2,45}", value):
        return None
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return str(address)


def _request_ip_from_message(api_code: Any, message: Any) -> str | None:
    """Accept only complete, known OKX 50110 English whitelist templates."""
    if api_code != "50110" or type(message) is not str or len(message) > 256:
        return None
    matched = re.fullmatch(
        r"Your IP(?: address)? ([0-9A-Fa-f:.]{2,45}) is not included in your API key's IP whitelist\.?",
        message)
    return _safe_request_ip(matched[1]) if matched else None


_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_MAX_ERROR_BYTES = 64 * 1024


def _http_diagnostics(status: Any, raw: Any) -> tuple[int | None, str | None, str, str | None]:
    """Extract numeric diagnostics from a bounded error body, without its msg."""
    http_status = _safe_http_status(status)
    api_code = None
    request_ip = None
    if isinstance(raw, (bytes, str)) and len(raw) <= _MAX_ERROR_BYTES:
        try:
            payload = json.loads(raw)
            if isinstance(payload, dict):
                candidate = _safe_api_code(payload.get("code"))
                # A body that resembles success never turns HTTP failure into
                # success, or presents zero as an error code to the operator.
                if candidate is not None and int(candidate) != 0:
                    api_code = candidate
                    request_ip = _request_ip_from_message(api_code, payload.get("msg"))
        except (ValueError, TypeError, RecursionError):
            pass
    category = "http"
    if http_status is not None:
        if 300 <= http_status <= 399:
            category = "redirect"
        elif http_status == 407:
            category = "proxy"
        elif http_status == 429:
            category = "rate_limit"
        elif http_status >= 500:
            category = "server"
    return http_status, api_code, category, request_ip


def _http_failure(status: Any, raw: Any, *, order: bool) -> OkxError:
    http_status, api_code, category, request_ip = _http_diagnostics(status, raw)
    if order:
        return AmbiguousOrderError("Order HTTP response failed; reconcile using client order ID before continuing",
                                   api_code=api_code, http_status=http_status, category=category,
                                   request_ip=request_ip)
    if api_code is not None:
        message = f"OKX rejected request (code {api_code})"
    elif http_status is not None:
        message = f"OKX HTTP request failed (HTTP {http_status})"
    else:
        message = "OKX HTTP request failed"
    return OkxError(message, api_code=api_code, http_status=http_status, category=category,
                    request_ip=request_ip)


def _transport_category(error: BaseException) -> str:
    # A URLError can wrap a typed socket exception or merely a string. Only
    # inspect types; a reason string can contain proxy URLs and credentials.
    reason = error.reason if isinstance(error, urllib.error.URLError) else error
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return "timeout"
    if isinstance(reason, ssl.SSLError):
        return "tls"
    if isinstance(reason, socket.gaierror):
        return "dns"
    return "transport"


@dataclass(frozen=True, repr=False)
class Credentials:
    api_key: str
    api_secret: str
    passphrase: str

    def __post_init__(self) -> None:
        if not all(isinstance(v, str) and v.strip() for v in
                   (self.api_key, self.api_secret, self.passphrase)):
            raise OkxError("Missing OKX demo API credentials")
        if any(not value.isascii() or any(ord(char) < 32 or ord(char) == 127 for char in value)
               for value in (self.api_key, self.api_secret, self.passphrase)):
            raise OkxError("OKX credential format is invalid; use ASCII values without control characters",
                           category="credential_format")

    def __repr__(self) -> str:
        return "Credentials(<redacted>)"

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "Credentials":
        source = os.environ if environ is None else environ
        return cls(*(source.get(name, "") for name in
                     ("OKX_API_KEY", "OKX_API_SECRET", "OKX_API_PASSPHRASE")))


@dataclass(frozen=True)
class Candle:
    ts: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal


@dataclass(frozen=True)
class Instrument:
    inst_id: str
    ct_val: Decimal
    lot_size: Decimal
    min_size: Decimal
    max_market_size: Decimal
    tick_size: Decimal = Decimal("0.1")
    ct_val_ccy: str = "BTC"
    settle_ccy: str = "USDT"
    state: str = "live"


def _decimal(value: Any, field: str, *, allow_zero: bool = False) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise OkxError(f"Invalid decimal field: {field}") from None
    if not result.is_finite() or result < 0 or (not allow_zero and result == 0):
        raise OkxError(f"Invalid decimal field: {field}")
    return result


def decimal_string(value: Any) -> str:
    return format(_decimal(value, "order size or price"), "f")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> None:
        # Authentication headers must never follow a redirect to another host.
        return None


class OkxClient:
    """Public market data + demo-only private OKX REST endpoints.

    There is intentionally no live-trading switch or configurable API host.
    ``opener`` is an injectable urllib-compatible callable for deterministic tests.
    """

    BASE_URL = "https://www.okx.com"
    _BARS = {"1m", "3m", "5m", "15m", "30m", "1H", "2H", "4H", "6H", "12H", "1D", "1Dutc"}

    def __init__(self, credentials: Credentials | None = None, timeout: float = 10,
                 opener: Callable[..., Any] | None = None) -> None:
        if not 0 < timeout <= 60:
            raise ValueError("HTTP timeout must be between 0 and 60 seconds")
        self.credentials = credentials
        self.timeout = timeout
        self._open = opener or urllib.request.build_opener(_NoRedirect()).open

    @staticmethod
    def sign(timestamp: str, method: str, path: str, body: str, secret: str) -> str:
        prehash = (timestamp + method.upper() + path + body).encode("utf-8")
        digest = hmac.new(secret.encode("utf-8"), prehash, hashlib.sha256).digest()
        return base64.b64encode(digest).decode("ascii")

    @staticmethod
    def _instrument_id(inst_id: str) -> str:
        if not re.fullmatch(r"[A-Z0-9]{2,20}-USDT-SWAP", inst_id):
            raise OkxError("Only linear USDT perpetual swaps are supported")
        return inst_id

    def _request(self, method: str, path: str, *, params: dict[str, Any] | None = None,
                 payload: dict[str, Any] | None = None, private: bool = False,
                 order: bool = False) -> list[dict[str, Any]] | list[Any]:
        if not path.startswith("/api/v5/") or any(c in path for c in ("?", "#", ":", "\\")):
            raise OkxError("Invalid OKX API path")
        if method not in {"GET", "POST"}:
            raise OkxError("Unsupported HTTP method")
        if method == "POST" and not private:
            raise OkxError("All mutation endpoints require demo authentication")
        query = "?" + urllib.parse.urlencode(params) if params else ""
        request_path = path + query
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=True) if payload is not None else ""
        # Public metadata/marks must use the same simulated environment as the
        # private orders; OKX's SDK applies this flag to unsigned requests too.
        headers = {"Content-Type": "application/json", "User-Agent": "okx-demo-bot/0.1",
                   "x-simulated-trading": "1"}
        if private:
            credentials = self.credentials
            if credentials is None:
                raise OkxError("Missing OKX demo API credentials")
            timestamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            headers.update({
                "OK-ACCESS-KEY": credentials.api_key,
                "OK-ACCESS-SIGN": self.sign(timestamp, method, request_path, body, credentials.api_secret),
                "OK-ACCESS-TIMESTAMP": timestamp,
                "OK-ACCESS-PASSPHRASE": credentials.passphrase,
                "x-simulated-trading": "1",
            })
        request = urllib.request.Request(self.BASE_URL + request_path,
                                         data=body.encode("utf-8") if body else None,
                                         headers=headers, method=method)
        # Assert the destination even if a subclass accidentally changes BASE_URL.
        parsed = urllib.parse.urlsplit(request.full_url)
        if parsed.scheme != "https" or parsed.netloc != "www.okx.com":
            raise OkxError("OKX API host must be https://www.okx.com")
        response_status = None
        try:
            with self._open(request, timeout=self.timeout) as response:
                response_status = _safe_http_status(getattr(response, "status", None))
                limit = _MAX_ERROR_BYTES if response_status is not None and not 200 <= response_status < 300 else _MAX_RESPONSE_BYTES
                raw = response.read(limit + 1)
        except urllib.error.HTTPError as error:
            # HTTPError inherits URLError/OSError. Handle it first so an HTTP
            # 400/401 JSON code is not erased as a generic connection failure.
            status = _safe_http_status(error.code)
            error_raw = None
            try:
                error_raw = error.read(_MAX_ERROR_BYTES + 1)
            except (OSError, http.client.HTTPException, ValueError, TypeError):
                pass
            finally:
                try:
                    error.close()
                except (OSError, ValueError):
                    pass
            raise _http_failure(status, error_raw, order=order) from None
        except (OSError, urllib.error.URLError, http.client.HTTPException, ValueError, TimeoutError) as error:
            category = _transport_category(error)
            if order:
                raise AmbiguousOrderError("Order response unavailable; reconcile using client order ID before continuing",
                                          category=category) from None
            raise OkxError("OKX HTTPS request failed; check network, time and demo credentials",
                           category=category) from None
        if response_status is not None and not 200 <= response_status < 300:
            raise _http_failure(response_status, raw, order=order)
        try:
            if not isinstance(raw, (bytes, str)) or len(raw) > _MAX_RESPONSE_BYTES:
                raise ValueError()
            result = json.loads(raw)
        except (ValueError, TypeError, RecursionError):
            if order:
                raise AmbiguousOrderError("Invalid order response; reconcile using client order ID",
                                          category="invalid_response") from None
            raise OkxError("Invalid OKX API response", category="invalid_response") from None
        if not isinstance(result, dict):
            if order:
                raise AmbiguousOrderError("Invalid order response; reconcile using client order ID", category="invalid_response")
            raise OkxError("Invalid OKX API response", category="invalid_response")
        code = _safe_api_code(result.get("code"))
        if code != "0":
            safe_code = code or "unknown"
            if order and (code == "50004" or code is None):
                # OKX documents 50004 as a timeout with an unknown order result.
                raise AmbiguousOrderError("OKX order result unknown; reconcile using client order ID",
                                          api_code=code, http_status=response_status,
                                          category="timeout" if code == "50004" else "invalid_response")
            # Do not echo the server's msg: it can contain request/credential data.
            raise OkxError(f"OKX rejected request (code {safe_code})", api_code=code,
                           http_status=response_status, category="api" if code else "invalid_response",
                           request_ip=_request_ip_from_message(code, result.get("msg")))
        if not isinstance(result.get("data"), list):
            if order:
                raise AmbiguousOrderError("Invalid order response; reconcile using client order ID", category="invalid_response")
            raise OkxError("Invalid OKX API response", category="invalid_response")
        return result["data"]

    def instrument(self, inst_id: str) -> Instrument:
        inst_id = self._instrument_id(inst_id)
        rows = self._request("GET", "/api/v5/public/instruments", params={"instType": "SWAP", "instId": inst_id})
        if len(rows) != 1 or not isinstance(rows[0], dict):
            raise OkxError("Instrument metadata unavailable")
        row = rows[0]
        base = inst_id.split("-")[0]
        if (row.get("instId") != inst_id or row.get("instType") != "SWAP"
                or row.get("ctType") != "linear" or row.get("settleCcy") != "USDT"
                or row.get("ctValCcy") != base or row.get("state") != "live"):
            raise OkxError("Instrument must be an active, linear USDT-settled perpetual swap")
        return Instrument(inst_id, _decimal(row.get("ctVal"), "ctVal"),
                          _decimal(row.get("lotSz"), "lotSz"),
                          _decimal(row.get("minSz"), "minSz"),
                          _decimal(row.get("maxMktSz"), "maxMktSz"),
                          _decimal(row.get("tickSz"), "tickSz"), base, "USDT", "live")

    def candles(self, inst_id: str, bar: str = "15m", limit: int = 100) -> list[Candle]:
        self._instrument_id(inst_id)
        if bar not in self._BARS or isinstance(limit, bool) or not 1 <= limit <= 300:
            raise OkxError("Unsupported candle bar or limit (1–300)")
        rows = self._request("GET", "/api/v5/market/candles", params={"instId": inst_id, "bar": bar, "limit": limit})
        closed: dict[int, Candle] = {}
        for row in rows:
            if not isinstance(row, list) or len(row) < 9:
                raise OkxError("Invalid candle data")
            if row[8] != "1":
                continue
            try:
                ts = int(row[0])
            except (ValueError, TypeError):
                raise OkxError("Invalid candle timestamp") from None
            prices = [_decimal(row[index], "candle price") for index in range(1, 5)]
            o, h, low, c = prices
            if ts < 0 or h < max(o, c, low) or low > min(o, c, h):
                raise OkxError("Invalid candle OHLC values")
            candle = Candle(ts, o, h, low, c)
            if ts in closed and closed[ts] != candle:
                raise OkxError("Conflicting duplicate candle data")
            closed[ts] = candle
        return [closed[ts] for ts in sorted(closed)]

    def server_time(self) -> int:
        rows = self._request("GET", "/api/v5/public/time")
        try:
            return int(rows[0]["ts"])
        except (IndexError, KeyError, ValueError, TypeError):
            raise OkxError("Invalid server time response") from None

    def mark_price(self, inst_id: str) -> tuple[Decimal, int]:
        self._instrument_id(inst_id)
        rows = self._request("GET", "/api/v5/public/mark-price",
                             params={"instType": "SWAP", "instId": inst_id})
        try:
            if len(rows) != 1 or rows[0].get("instId") != inst_id:
                raise ValueError()
            price = _decimal(rows[0].get("markPx"), "mark price")
            timestamp = int(rows[0]["ts"])
            if timestamp < 0:
                raise ValueError()
            return price, timestamp
        except (IndexError, KeyError, ValueError, TypeError):
            raise OkxError("Invalid mark price response") from None

    def account_config(self) -> dict[str, Any]:
        rows = self._request("GET", "/api/v5/account/config", private=True)
        if len(rows) != 1 or not isinstance(rows[0], dict):
            raise OkxError("Invalid account configuration response")
        return rows[0]

    def positions(self, inst_id: str) -> list[dict[str, Any]]:
        self._instrument_id(inst_id)
        return self._request("GET", "/api/v5/account/positions", params={"instId": inst_id}, private=True)

    def pending_orders(self, inst_id: str) -> list[dict[str, Any]]:
        self._instrument_id(inst_id)
        return self._request("GET", "/api/v5/trade/orders-pending", params={"instId": inst_id}, private=True)

    def pending_algos(self, inst_id: str) -> list[dict[str, Any]]:
        self._instrument_id(inst_id)
        rows: list[dict[str, Any]] = []
        for order_type in ("conditional", "oco", "trigger", "move_order_stop"):
            rows.extend(self._request("GET", "/api/v5/trade/orders-algo-pending",
                                      params={"instId": inst_id, "ordType": order_type}, private=True))
        return rows

    def pending_algo_orders(self, inst_id: str) -> list[dict[str, Any]]:
        """Alias used by execution preflight and protective-order reconciliation."""
        return self.pending_algos(inst_id)

    def get_leverage(self, inst_id: str) -> list[dict[str, Any]]:
        self._instrument_id(inst_id)
        return self._request("GET", "/api/v5/account/leverage-info",
                             params={"instId": inst_id, "mgnMode": "isolated"}, private=True)

    def balances(self) -> dict[str, Any]:
        rows = self._request("GET", "/api/v5/account/balance", params={"ccy": "USDT"}, private=True)
        try:
            detail = next(row for row in rows[0]["details"] if row.get("ccy") == "USDT")
        except (IndexError, KeyError, TypeError, StopIteration):
            raise OkxError("USDT account balance unavailable") from None
        # availEq is the available equity in margin account modes. Do not substitute
        # totalEq (which can include collateral in other currencies).
        available = detail.get("availEq") or detail.get("availBal")
        return {"currency": "USDT", "equity": _decimal(detail.get("eq"), "USDT equity", allow_zero=True),
                "available": _decimal(available, "USDT available equity", allow_zero=True)}

    def set_leverage(self, inst_id: str, leverage: str = "1") -> dict[str, Any]:
        self._instrument_id(inst_id)
        if str(leverage) != "1":
            raise OkxError("Only 1x demo leverage is supported")
        rows = self._request("POST", "/api/v5/account/set-leverage", private=True,
                             payload={"instId": inst_id, "lever": "1", "mgnMode": "isolated"})
        if len(rows) != 1 or not isinstance(rows[0], dict):
            raise OkxError("Invalid leverage configuration response")
        return rows[0]

    def place_market_order(self, inst_id: str, side: str, size: Any, client_id: str,
                           reduce_only: bool = False, stop_loss: Any | None = None,
                           take_profit: Any | None = None) -> dict[str, Any]:
        self._instrument_id(inst_id)
        if side not in {"buy", "sell"}:
            raise OkxError("Order side must be buy or sell")
        if not re.fullmatch(r"[A-Za-z0-9]{1,32}", client_id):
            raise OkxError("Client order ID must contain 1–32 ASCII letters or digits")
        if not isinstance(reduce_only, bool):
            raise OkxError("reduce_only must be a boolean")
        payload: dict[str, Any] = {"instId": inst_id, "tdMode": "isolated", "posSide": "net",
                                   "side": side, "ordType": "market", "sz": decimal_string(size),
                                   "clOrdId": client_id, "reduceOnly": reduce_only}
        if reduce_only and (stop_loss is not None or take_profit is not None):
            raise OkxError("Reduce-only exits cannot create attached orders")
        if not reduce_only:
            if stop_loss is None or take_profit is None:
                raise OkxError("Demo entries require both stop-loss and take-profit prices")
            sl, tp = _decimal(stop_loss, "stop loss"), _decimal(take_profit, "take profit")
            if (side == "buy" and sl >= tp) or (side == "sell" and tp >= sl):
                raise OkxError("Stop loss and take profit do not match the entry direction")
            payload["attachAlgoOrds"] = [{"attachAlgoClOrdId": client_id + "a" if len(client_id) < 32 else client_id,
                                          "slTriggerPx": format(sl, "f"), "slOrdPx": "-1",
                                          "slTriggerPxType": "mark", "tpTriggerPx": format(tp, "f"),
                                          "tpOrdPx": "-1", "tpTriggerPxType": "mark"}]
        rows = self._request("POST", "/api/v5/trade/order", payload=payload, private=True, order=True)
        if len(rows) != 1 or not isinstance(rows[0], dict):
            raise AmbiguousOrderError("Invalid order acknowledgement; reconcile using client order ID", category="invalid_response")
        row = rows[0]
        code = _safe_api_code(row.get("sCode"))
        if code != "0":
            if code == "50004" or code is None:
                raise AmbiguousOrderError("OKX order result unknown; reconcile using client order ID",
                                          api_code=code, category="timeout" if code == "50004" else "invalid_response")
            raise OkxError(f"OKX rejected demo order (code {code})", api_code=code, category="api")
        if not row.get("ordId") or row.get("clOrdId") != client_id:
            raise AmbiguousOrderError("Order acknowledgement identity mismatch; reconcile using client order ID", category="invalid_response")
        return row

    def query_order(self, inst_id: str, client_id: str) -> dict[str, Any]:
        self._instrument_id(inst_id)
        if not re.fullmatch(r"[A-Za-z0-9]{1,32}", client_id):
            raise OkxError("Invalid client order ID")
        rows = self._request("GET", "/api/v5/trade/order", params={"instId": inst_id, "clOrdId": client_id}, private=True)
        if len(rows) != 1 or not isinstance(rows[0], dict):
            raise OkxError("Order reconciliation response unavailable")
        return rows[0]
