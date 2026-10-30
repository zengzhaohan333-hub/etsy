"""Durable shared-account risk state; callers already hold the account lock.

Only fingerprints and scalar risk facts are stored. A restart must not reset an
equity peak or clear a drawdown stop. Strategy/account changes require review.
"""
from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import time

from .portfolio_config import PortfolioConfig


class SessionError(RuntimeError):
    """A fixed, credential-free risk-state refusal."""


def profile_fingerprint(profile: PortfolioConfig) -> str:
    if not isinstance(profile, PortfolioConfig):
        raise SessionError("A validated portfolio profile is required for session binding")
    data = {key: format(value.normalize(), "f") if isinstance(value, Decimal) else value
            for key, value in asdict(profile).items()}
    raw = json.dumps(data, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(raw).hexdigest()


def _pairs(values):
    result = {}
    for key, value in values:
        if key in result:
            raise ValueError()
        result[key] = value
    return result


def _invalid_constant(_value):
    raise ValueError()


def _read(path: Path, maximum: int):
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o022):
            raise SessionError("Risk state must be a regular file owned by the service user")
        raw = stream.read(maximum + 1)
    if len(raw) > maximum:
        raise SessionError("Risk state exceeds its size limit")
    return json.loads(raw, object_pairs_hook=_pairs, parse_constant=_invalid_constant)


def _equity(value, *, allow_zero=False) -> Decimal:
    if (not isinstance(value, Decimal) or not value.is_finite()
            or value < 0 or (not allow_zero and value == 0) or value > Decimal("1e30")):
        raise SessionError("Session equity must be a valid finite decimal")
    return value


def _fingerprint(value):
    if type(value) is not str or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise SessionError("Invalid session binding fingerprint")
    return value


def _write(path: Path, value: dict):
    descriptor, temporary = tempfile.mkstemp(prefix=".session-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, allow_nan=False, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


class PortfolioSession:
    def __init__(self, path: Path, api_fingerprint: str, config_fingerprint: str,
                 peak: Decimal, risk_halted: bool):
        self.path = path
        self.api_fingerprint = api_fingerprint
        self.config_fingerprint = config_fingerprint
        self.peak = peak
        self.risk_halted = risk_halted
        self.latched_reason = "session_drawdown" if risk_halted else ""

    @classmethod
    def load_or_create(cls, state_dir: Path, api_fingerprint: str,
                       config_fingerprint: str, initial_equity: Decimal, *,
                       allow_initialize: bool = False):
        if type(allow_initialize) is not bool:
            raise SessionError("Session initialization policy must be explicit")
        _fingerprint(api_fingerprint)
        _fingerprint(config_fingerprint)
        _equity(initial_equity)
        directory = Path(state_dir)
        path = directory / "session.json"
        try:
            if directory.is_symlink() or not directory.is_dir():
                raise SessionError("Session state directory must already exist and cannot be a symlink")
            try:
                data = _read(path, 64 * 1024)
            except FileNotFoundError:
                if not allow_initialize:
                    raise SessionError("Risk session is missing; explicit read-only initialization is required") from None
                # This branch is reserved for an explicit operator action after
                # a flat-account check, never an automatic service restart.
                journal_path = directory / "demo-journal.json"
                try:
                    journal = _read(journal_path, 4 * 1024 * 1024)
                except FileNotFoundError:
                    journal = {"version": 1, "orders": []}
                    _write(journal_path, journal)
                if (not isinstance(journal, dict) or type(journal.get("version")) is not int or journal["version"] != 1
                        or not isinstance(journal.get("orders"), list)
                        or any(not isinstance(row, dict) or row.get("status") != "closed"
                               for row in journal["orders"])):
                    raise SessionError("Missing risk state with unresolved journal; reconcile before first service migration")
                result = cls(path, api_fingerprint, config_fingerprint, initial_equity, False)
                result._save()
                return result
            expected = {"version", "api_fingerprint", "config_fingerprint", "peak_equity",
                        "risk_halted", "latched_reason", "updated_at_ms"}
            if (not isinstance(data, dict) or set(data) != expected
                    or type(data.get("version")) is not int or data["version"] != 1
                    or type(data.get("risk_halted")) is not bool
                    or type(data.get("peak_equity")) is not str or len(data["peak_equity"]) > 128
                    or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", data["peak_equity"])
                    or type(data.get("updated_at_ms")) is not int
                    or not 0 <= data["updated_at_ms"] <= 32_503_680_000_000
                    or data.get("latched_reason") != ("session_drawdown" if data["risk_halted"] else "")):
                raise SessionError("Risk state is invalid; manual review is required")
            if (data.get("api_fingerprint") != api_fingerprint
                    or data.get("config_fingerprint") != config_fingerprint):
                raise SessionError("Risk state belongs to a different account or strategy; manual review is required")
            try:
                journal = _read(directory / "demo-journal.json", 4 * 1024 * 1024)
            except FileNotFoundError:
                raise SessionError("Order journal is missing for an existing service session; manual review is required") from None
            if (not isinstance(journal, dict) or type(journal.get("version")) is not int
                    or journal["version"] != 1 or not isinstance(journal.get("orders"), list)
                    or any(not isinstance(row, dict) for row in journal["orders"])):
                raise SessionError("Order journal is invalid for the existing service session")
            peak = _equity(Decimal(data["peak_equity"]))
            return cls(path, api_fingerprint, config_fingerprint, peak, data["risk_halted"])
        except SessionError:
            raise
        except (OSError, ValueError, TypeError, InvalidOperation, RecursionError):
            raise SessionError("Risk state could not be safely loaded or created") from None

    def _save(self):
        value = {"version": 1, "api_fingerprint": self.api_fingerprint,
                 "config_fingerprint": self.config_fingerprint, "peak_equity": format(self.peak, "f"),
                 "risk_halted": self.risk_halted, "latched_reason": self.latched_reason,
                 "updated_at_ms": time.time_ns() // 1_000_000}
        try:
            _write(self.path, value)
        except OSError:
            raise SessionError("Risk state could not be durably saved; new trading must stop") from None

    def observe(self, equity: Decimal, drawdown_fraction: Decimal) -> bool:
        """Persist a new peak/latched stop; return whether the stop is latched."""
        _equity(equity, allow_zero=True)
        if (not isinstance(drawdown_fraction, Decimal) or not drawdown_fraction.is_finite()
                or not 0 < drawdown_fraction <= Decimal("0.5")):
            raise SessionError("Invalid session drawdown threshold")
        peak = max(self.peak, equity)
        halted = self.risk_halted or (peak - equity) / peak >= drawdown_fraction
        if peak != self.peak or halted != self.risk_halted:
            self.peak, self.risk_halted = peak, halted
            self.latched_reason = "session_drawdown" if halted else ""
            self._save()
        return self.risk_halted
