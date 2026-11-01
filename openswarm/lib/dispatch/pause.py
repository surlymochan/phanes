"""Persistent, scoped dispatch pauses (Refs #1402, part of #1366).

Pause records live as JSON files in ``<home>/pauses``.  The module has no
knowledge of the dispatch loop; callers pass the clock explicitly and decide
when a matching pause should affect scheduling.
"""
from __future__ import annotations

import json
import math
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional


def _time(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("{} must be a number, got {!r}".format(name, value))
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("{} must be finite, got {!r}".format(name, value))
    return number


def _scope(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("scope must be a string, got {!r}".format(value))
    value = value.strip()
    if value in ("all", "mint"):
        return value
    match = re.fullmatch(r"(lane|host|backend):([^/\\]+)", value)
    if not match or not match.group(2).strip():
        raise ValueError("invalid pause scope: {!r}".format(value))
    return "{}:{}".format(match.group(1), match.group(2).strip())


def _scope_key(scope: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", scope)


class PauseStore:
    """Read and write pauses under ``home/pauses``."""

    def __init__(self, home: Any) -> None:
        self.dir = Path(home) / "pauses"

    def _path(self, scope: str) -> Path:
        return self.dir / (_scope_key(scope) + ".json")

    def _read(self, path: Path) -> Optional[Dict[str, Any]]:
        try:
            with open(path, encoding="utf-8") as stream:
                record = json.load(stream)
        except (OSError, ValueError):
            return None
        if not isinstance(record, dict):
            return None
        try:
            record["scope"] = _scope(record.get("scope"))
            record["created_at"] = _time(record.get("created_at"), "created_at")
            if record.get("expires_at") is not None:
                record["expires_at"] = _time(record["expires_at"], "expires_at")
        except ValueError:
            return None
        record["reason"] = str(record.get("reason", ""))
        return record

    def pause(self, scope: str, ttl_s: Any = None, reason: str = "", now: Any = None) -> Dict[str, Any]:
        """Create or replace a pause. ``now`` is required for deterministic expiry."""
        normalized = _scope(scope)
        if now is None:
            raise ValueError("now is required")
        moment = _time(now, "now")
        ttl = None if ttl_s is None else _time(ttl_s, "ttl_s")
        if ttl is not None and ttl <= 0:
            raise ValueError("ttl_s must be positive, got {!r}".format(ttl_s))
        record = {
            "scope": normalized,
            "created_at": moment,
            "expires_at": None if ttl is None else moment + ttl,
            "reason": str(reason),
        }
        self.dir.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".pause-", dir=str(self.dir))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(record, stream, sort_keys=True)
                stream.write("\n")
            os.replace(temporary, self._path(normalized))
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return dict(record)

    def resume(self, scope: str) -> bool:
        """Remove one scope's pause; return whether a record existed."""
        try:
            self._path(_scope(scope)).unlink()
            return True
        except FileNotFoundError:
            return False

    def active(self, now: Any) -> List[Dict[str, Any]]:
        """Return unexpired pauses, sorted lexically by scope."""
        moment = _time(now, "now")
        records = []
        try:
            paths = self.dir.glob("*.json")
            for path in paths:
                record = self._read(path)
                if record is None:
                    continue
                expiry = record["expires_at"]
                if expiry is not None and moment >= expiry:
                    continue
                records.append(record)
        except OSError:
            return []
        return sorted(records, key=lambda row: row["scope"])

    def is_paused(self, lane: Any = None, host: Any = None, backend: Any = None, now: Any = None) -> bool:
        """Whether any active pause applies to the supplied dispatch target."""
        if now is None:
            raise ValueError("now is required")
        scopes = {record["scope"] for record in self.active(now)}
        if "all" in scopes:
            return True
        return any(
            value is not None and "{}:{}".format(kind, value) in scopes
            for kind, value in (("lane", lane), ("host", host), ("backend", backend))
        )


def legacy_markers(directory: Any) -> List[Dict[str, Any]]:
    """Map old PAUSE marker files into pause records, sorted by scope.

    Legacy markers have no clock or reason metadata, so they are indefinite
    records with a stable ``created_at`` of zero and an explanatory reason.
    """
    root = Path(directory)
    records = []
    try:
        names = list(root.iterdir())
    except OSError:
        return records
    for path in names:
        if not path.is_file():
            continue
        if path.name == "PAUSE":
            scope = "all"
        elif path.name == "PAUSE-MINT":
            scope = "mint"
        elif path.name.startswith("PAUSE-") and len(path.name) > len("PAUSE-"):
            scope = "lane:" + path.name[len("PAUSE-"):]
        else:
            continue
        records.append({"scope": scope, "created_at": 0.0, "expires_at": None,
                        "reason": "legacy marker: " + path.name})
    return sorted(records, key=lambda row: row["scope"])
