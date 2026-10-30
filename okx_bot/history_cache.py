"""Atomic checkpoints of accepted production-public history response pages.

This is deliberately not a dataset loader. The history paginator must validate
each page before saving it, reparse every loaded page, and validate the complete
final dataset. A checkpoint never certifies OHLC continuity or funding coverage.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid
from typing import Any

from .history import BASE_URL, ENDPOINTS, INTERVALS, MAX_TIMESTAMP_MS, HistoryError


MAX_PAGE_BYTES = 1024 * 1024
MAX_ROWS = 100
VERSION = 1
PROVENANCE = "production_public"
_ERROR = "公开历史分页缓存无效；已停止，不会覆盖损坏缓存或使用不完整数据。"


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def _reject_constant(_value: str) -> None:
    raise ValueError()


def _unique(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError()
        result[key] = value
    return result


def _scalar(value: Any) -> bool:
    return (value is None
            or (type(value) is str and len(value) <= 512)
            or (type(value) is int and -MAX_TIMESTAMP_MS <= value <= MAX_TIMESTAMP_MS))


def _rows_valid(rows: Any, kind: str) -> bool:
    if type(rows) is not list or not 1 <= len(rows) <= MAX_ROWS:
        return False
    for row in rows:
        if kind == "funding":
            if (type(row) is not dict or not 1 <= len(row) <= 32
                    or any(type(key) is not str or not 1 <= len(key) <= 64 for key in row)
                    or any(not _scalar(value) for value in row.values())):
                return False
        elif type(row) is not list or not 1 <= len(row) <= 16 or any(not _scalar(value) for value in row):
            return False
    return True


class HistoryPageCache:
    """A page cache bound to one exact instrument, interval, window and kind.

    ``load(cursor)`` returns raw rows or None; it never advances a paginator.
    ``save(cursor, rows)`` accepts only pages already approved by that paginator.
    Existing pages are immutable: identical saves are harmless, conflicts stop.
    """

    def __init__(self, cache_dir: Path, *, inst_id: str, bar: str,
                 start_ms: int, end_ms: int, kind: str) -> None:
        if (type(inst_id) is not str or re.fullmatch(r"[A-Z0-9]{2,20}-USDT-SWAP", inst_id) is None
                or type(bar) is not str or bar not in INTERVALS
                or type(kind) is not str or kind not in {"trade", "mark", "funding"}
                or type(start_ms) is not int or type(end_ms) is not int
                or not 0 <= start_ms < end_ms <= MAX_TIMESTAMP_MS
                or start_ms % INTERVALS[bar] or end_ms % INTERVALS[bar]):
            raise HistoryError(_ERROR)
        self._identity = {"inst_id": inst_id, "bar": bar, "start_ms": start_ms,
                          "end_ms": end_ms, "kind": kind}
        self._source = {"host": BASE_URL, "endpoint": ENDPOINTS[kind]}
        try:
            directory = Path(cache_dir)
            resolved = directory.resolve()
            # Research checkpoints belong outside account runtime state. No
            # state content, credentials or trading configuration is loaded.
            for candidate in (directory, *directory.parents, resolved, *resolved.parents):
                if (candidate.name in {".runtime", ".runtime-demo", "okxbot"}
                        or any((candidate / name).exists() for name in ("status.json", "demo-journal.json", "session.json"))):
                    raise HistoryError("公开历史分页缓存必须放在独立研究目录，不能写入机器人状态目录。")
        except HistoryError:
            raise
        except (OSError, RuntimeError, ValueError, TypeError):
            raise HistoryError(_ERROR) from None
        namespace = hashlib.sha256(_canonical(self._identity)).hexdigest()
        self._directory = directory / namespace

    def path_for(self, cursor: int) -> Path:
        if (type(cursor) is not int or not self._identity["start_ms"] <= cursor <= self._identity["end_ms"] + 1
                or (self._identity["kind"] != "funding" and cursor % INTERVALS[self._identity["bar"]])):
            raise HistoryError(_ERROR)
        return self._directory / (str(cursor) + ".json")

    def load(self, cursor: int) -> list | None:
        path = self.path_for(cursor)
        try:
            if path.is_symlink() or self._directory.is_symlink():
                raise HistoryError(_ERROR)
            try:
                descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            except FileNotFoundError:
                return None
            with os.fdopen(descriptor, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_PAGE_BYTES:
                    raise HistoryError(_ERROR)
                raw = stream.read(MAX_PAGE_BYTES + 1)
            if len(raw) > MAX_PAGE_BYTES:
                raise HistoryError(_ERROR)
            payload = json.loads(raw, parse_constant=_reject_constant, object_pairs_hook=_unique)
            if (type(payload) is not dict
                    or set(payload) != {"version", "provenance", "source", "request", "rows", "sha256"}
                    or type(payload["version"]) is not int or payload["version"] != VERSION
                    or payload["provenance"] != PROVENANCE or payload["source"] != self._source
                    or type(payload["source"]) is not dict
                    or payload["request"] != dict(self._identity, cursor=cursor)
                    or type(payload["request"]) is not dict
                    or type(payload["request"].get("cursor")) is not int
                    or type(payload["request"].get("start_ms")) is not int
                    or type(payload["request"].get("end_ms")) is not int
                    or type(payload["sha256"]) is not str
                    or re.fullmatch(r"[0-9a-f]{64}", payload["sha256"]) is None
                    or not _rows_valid(payload["rows"], self._identity["kind"])):
                raise HistoryError(_ERROR)
            digest = payload.pop("sha256")
            if hashlib.sha256(_canonical(payload)).hexdigest() != digest:
                raise HistoryError(_ERROR)
            return payload["rows"]
        except HistoryError:
            raise
        except (OSError, ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
            raise HistoryError(_ERROR) from None

    def save(self, cursor: int, rows: list) -> None:
        path = self.path_for(cursor)
        if not _rows_valid(rows, self._identity["kind"]):
            raise HistoryError(_ERROR)
        payload = {"version": VERSION, "provenance": PROVENANCE, "source": self._source,
                   "request": dict(self._identity, cursor=cursor), "rows": rows}
        payload["sha256"] = hashlib.sha256(_canonical(payload)).hexdigest()
        encoded = _canonical(payload)
        if len(encoded) > MAX_PAGE_BYTES:
            raise HistoryError(_ERROR)
        previous = self.load(cursor)
        if previous is not None:
            if _canonical(previous) != _canonical(rows):
                raise HistoryError(_ERROR)
            return
        temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
        try:
            self._directory.mkdir(parents=True, exist_ok=True)
            if self._directory.is_symlink() or path.is_symlink():
                raise HistoryError(_ERROR)
            with temporary.open("xb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            # Windows cannot open directories this way. On POSIX flush both
            # the file entry and its new namespace before claiming durability.
            if os.name != "nt":
                for directory in (self._directory, self._directory.parent):
                    descriptor = os.open(directory, os.O_RDONLY)
                    try:
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
        except HistoryError:
            raise
        except OSError:
            raise HistoryError("公开历史分页进度无法安全保存；请检查研究目录权限和磁盘空间后重试。") from None
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
