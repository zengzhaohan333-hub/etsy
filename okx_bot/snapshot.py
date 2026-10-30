"""Validated, cycle-scoped SWAP account reads for the demo coordinator.

Six private GETs form one complete view: positions, ordinary orders and four
explicit algorithm families. This is a sequential REST view, not an atomic
exchange transaction. Request-start times must remain part of entry freshness
checks. A failed refresh never exposes a partially assembled account view.
"""
from __future__ import annotations

import copy
import re
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from .okx import OkxError
from .portfolio_config import DEFAULT_INSTRUMENTS
from .read_retry import _READ_METHODS


ALGO_FAMILIES = ("conditional", "oco", "trigger", "move_order_stop")


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _invalid() -> None:
    raise OkxError("Account snapshot contains unsupported, conflicting or invalid records; inspect exchange state",
                   category="invalid_response")


def _decimal(value: Any, *, positive: bool = False) -> Decimal:
    if type(value) not in (str, int, Decimal) or len(str(value)) > 128:
        _invalid()
    try:
        number = Decimal(str(value))
    except (ValueError, InvalidOperation):
        _invalid()
    if (not number.is_finite() or (positive and number <= 0)
            or number.copy_abs() > Decimal("1e30")
            or not -30 <= number.as_tuple().exponent <= 30):
        _invalid()
    return number


def _timestamp(value: Any) -> int:
    if type(value) is str and re.fullmatch(r"[0-9]{1,16}", value):
        value = int(value)
    if type(value) is not int or not 0 <= value <= 32_503_680_000_000:
        _invalid()
    return value


def _identity(value: Any) -> str:
    if type(value) is not str or not re.fullmatch(r"[0-9]{1,40}", value):
        _invalid()
    return value


def _client_identity(value: Any) -> None:
    if value is not None and (type(value) is not str or (value and not re.fullmatch(r"[A-Za-z0-9]{1,32}", value))):
        _invalid()


@dataclass(frozen=True)
class _Snapshot:
    positions: dict[str, list[dict]]
    orders: dict[str, list[dict]]
    algos: dict[str, list[dict]]
    components: dict[str, dict[str, int]]


class AccountSnapshotClient:
    """Facade over a read-retrying adapter, with explicit refresh/invalidation.

    ``refresh`` does exactly six private reads and never resets the caller's
    budget. A first mark lookup in a valid account view makes one public bulk
    mark read, reused for the other three instruments. No TTL silently refreshes
    or extends these views. The coordinator explicitly refreshes each cycle and
    before every candidate, checking ``snapshot_read_times`` before POST.

    Mutations/unknown callable methods invalidate BEFORE delegation, including
    when they fail. While invalid, views delegate fresh per-instrument reads so
    existing fill/protection reconciliation cannot reuse pre-submit state.
    """

    def __init__(self, client: Any):
        self._client = client
        self._snapshot: _Snapshot | None = None
        self._marks: dict[str, tuple[Decimal, int]] | None = None
        self._mark_component: dict[str, int] | None = None

    @property
    def has_snapshot(self) -> bool:
        return self._snapshot is not None

    @property
    def snapshot_components(self) -> dict[str, dict[str, int]]:
        result = copy.deepcopy(self._snapshot.components) if self._snapshot is not None else {}
        if self._mark_component is not None:
            result["swap_mark_prices"] = dict(self._mark_component)
        return result

    @property
    def snapshot_read_times(self) -> tuple[int, ...]:
        if self._snapshot is None:
            raise OkxError("Account snapshot is unavailable; refresh before entry verification", category="local")
        return tuple(component["started_at_ms"] for component in self.snapshot_components.values())

    def invalidate(self) -> None:
        self._snapshot = None
        self._marks = None
        self._mark_component = None

    def begin_cycle(self, seconds: float = 60) -> None:
        self._client.begin_cycle(seconds)

    def refresh(self) -> None:
        self.invalidate()
        components: dict[str, dict[str, int]] = {}

        def fetch(tag: str, method: Any, *args: Any) -> list:
            started = _now_ms()
            rows = method(*args)
            finished = _now_ms()
            if (not isinstance(rows, list) or len(rows) > 10_000
                    or finished < started - 5_000):
                _invalid()
            components[tag] = {"started_at_ms": started, "finished_at_ms": finished}
            return rows

        positions = {inst: [] for inst in DEFAULT_INSTRUMENTS}
        orders = {inst: [] for inst in DEFAULT_INSTRUMENTS}
        algos = {inst: [] for inst in DEFAULT_INSTRUMENTS}
        raw_positions = fetch("swap_positions", self._client.swap_positions)
        raw_orders = fetch("swap_pending_orders", self._client.swap_pending_orders)
        if len(raw_orders) >= 100:
            _invalid()
        raw_algos: dict[str, list] = {}
        for family in ALGO_FAMILIES:
            rows = fetch("swap_algos_" + family, self._client.swap_pending_algos, family)
            if len(rows) >= 100:
                _invalid()
            raw_algos[family] = rows

        for row in raw_positions:
            if not isinstance(row, dict) or type(row.get("instId")) is not str:
                _invalid()
            size = _decimal(row.get("pos"))
            if size == 0:
                continue  # Inactive historical rows are not active exposure.
            inst = row["instId"]
            if (inst not in positions or positions[inst] or row.get("posSide") != "net"
                    or row.get("mgnMode") != "isolated"
                    or _decimal(row.get("lever"), positive=True) != 1):
                _invalid()
            _decimal(row.get("avgPx"), positive=True)
            positions[inst].append(copy.deepcopy(row))

        order_ids = set()
        for row in raw_orders:
            if not isinstance(row, dict) or type(row.get("instId")) is not str or row["instId"] not in orders:
                _invalid()
            identity = _identity(row.get("ordId"))
            _client_identity(row.get("clOrdId"))
            if identity in order_ids:
                _invalid()
            order_ids.add(identity)
            orders[row["instId"]].append(copy.deepcopy(row))

        algo_ids = set()
        for family, rows in raw_algos.items():
            for row in rows:
                if (not isinstance(row, dict) or type(row.get("instId")) is not str
                        or row["instId"] not in algos or row.get("ordType") != family):
                    _invalid()
                identity = _identity(row.get("algoId"))
                _client_identity(row.get("algoClOrdId"))
                if identity in algo_ids:
                    _invalid()
                algo_ids.add(identity)
                algos[row["instId"]].append(copy.deepcopy(row))
        self._snapshot = _Snapshot(positions, orders, algos, components)

    @staticmethod
    def _instrument(inst_id: str) -> str:
        if type(inst_id) is not str or inst_id not in DEFAULT_INSTRUMENTS:
            raise OkxError("Snapshot view supports only the four configured USDT swaps", category="local")
        return inst_id

    def positions(self, inst_id: str) -> list[dict]:
        self._instrument(inst_id)
        if self._snapshot is None:
            return self._client.positions(inst_id)
        return copy.deepcopy(self._snapshot.positions[inst_id])

    def pending_orders(self, inst_id: str) -> list[dict]:
        self._instrument(inst_id)
        if self._snapshot is None:
            return self._client.pending_orders(inst_id)
        return copy.deepcopy(self._snapshot.orders[inst_id])

    def pending_algo_orders(self, inst_id: str) -> list[dict]:
        self._instrument(inst_id)
        if self._snapshot is None:
            return self._client.pending_algo_orders(inst_id)
        return copy.deepcopy(self._snapshot.algos[inst_id])

    def pending_algos(self, inst_id: str) -> list[dict]:
        return self.pending_algo_orders(inst_id)

    def mark_price(self, inst_id: str) -> tuple[Decimal, int]:
        self._instrument(inst_id)
        if self._snapshot is None:
            return self._client.mark_price(inst_id)
        if self._marks is None:
            started = _now_ms()
            try:
                rows = self._client.swap_mark_prices()
                finished = _now_ms()
                if not isinstance(rows, list) or len(rows) > 10_000 or finished < started - 5_000:
                    _invalid()
                marks = {}
                for row in rows:
                    if not isinstance(row, dict) or type(row.get("instId")) is not str:
                        _invalid()
                    inst = row["instId"]
                    if inst not in DEFAULT_INSTRUMENTS:
                        continue  # Expected unrelated PUBLIC instrument marks.
                    if inst in marks:
                        _invalid()
                    marks[inst] = (_decimal(row.get("markPx"), positive=True), _timestamp(row.get("ts")))
                if set(marks) != set(DEFAULT_INSTRUMENTS):
                    _invalid()
            except Exception:
                self.invalidate()
                raise
            self._marks = marks
            self._mark_component = {"started_at_ms": started, "finished_at_ms": finished}
        return self._marks[inst_id]

    def __getattr__(self, name: str):
        method = getattr(self._client, name)
        if name in _READ_METHODS or not callable(method):
            return method

        def mutate(*args: Any, **kwargs: Any):
            self.invalidate()
            return method(*args, **kwargs)

        return mutate
