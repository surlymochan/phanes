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


# The host-level pause markers are the interchange format between the fleet
# health check and the dispatchers (Refs #1367): the health check writes
# ``PAUSE-HOST-<host>`` and every dispatcher reads it from the same place.
PAUSE_DIR_ENV = "OPENSWARM_PAUSE_DIR"
HOST_MARKER_PREFIX = "PAUSE-HOST-"
OPENSWARM_HOME_DEFAULT = "~/.claude/openswarm"
# The fleet health check writes its own host markers into this directory below
# the shared pause dir: a hold of the health check never collides with a
# marker another program owns in the shared dir (Refs #1367).
HEALTH_DIR_NAME = "health"
# A program that owns a hold of a dispatch target can keep it under a scope of
# its own: ``host:<host>@<program>`` (Refs #1367).  The fleet health check
# uses such a scope when another program already owns the scope of the host,
# so the health hold never overwrites that pause.  A scope of its own holds
# the same dispatch target as the scope it derives from, so a caller that asks
# about the target sees the hold.
SCOPE_SUFFIX_SEPARATOR = "@"


def pause_dir(home: Any = None) -> str:
    """The directory the host-level pause markers live in.

    ``OPENSWARM_PAUSE_DIR`` names it; without it the openswarm home
    (``$OPENSWARM_HOME``, default ``~/.claude/openswarm``) holds it.  The
    fleet health check writes the markers here and the dispatch engine and the
    lane autodispatcher read them from here, so one directory serves all of
    them.  ``home`` overrides the openswarm home (the tests use it).
    """
    override = os.environ.get(PAUSE_DIR_ENV) or os.environ.get("XSWARM_PAUSE_DIR")
    if override:
        return os.path.expanduser(override)
    default = os.environ.get("OPENSWARM_HOME") or os.environ.get("XSWARM_HOME") or OPENSWARM_HOME_DEFAULT
    return os.path.expanduser(str(home or default))


def health_pause_dir(home: Any = None) -> str:
    """The directory the fleet health check writes its own host markers into.

    It is a subdirectory of the shared pause dir (``<pause dir>/health``): the
    health hold of a host never collides with a marker another program owns
    in the shared dir, and that marker is never rewritten nor removed.
    """
    return os.path.join(pause_dir(home), HEALTH_DIR_NAME)


def pause_dirs(home: Any = None) -> List[str]:
    """Every directory a host-level pause marker can live in.

    The shared pause dir holds the markers the programs write there, the
    health marker directory below it holds the markers the fleet health check
    owns.  Every dispatcher reads all of them.
    """
    shared = pause_dir(home)
    return [shared, os.path.join(shared, HEALTH_DIR_NAME)]


def host_marker_path(directory: Any, host: Any) -> str:
    """The host-level pause marker of a host: ``<directory>/PAUSE-HOST-<host>``."""
    return os.path.join(str(directory), HOST_MARKER_PREFIX + str(host))


def host_paused(host: Any, directories: Any = None) -> bool:
    """Whether a host-level pause marker holds the host in one of the directories."""
    for directory in (directories if directories is not None else [pause_dir()]):
        try:
            if os.path.exists(host_marker_path(directory, host)):
                return True
        except OSError:
            continue
    return False


def parse_ttl(value: Any) -> float:
    """Parse a positive duration expressed in seconds, minutes, or hours."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        ttl = float(value)
    elif isinstance(value, str):
        match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smh]?)\s*", value, re.I)
        if not match:
            raise ValueError("invalid TTL: {!r}".format(value))
        ttl = float(match.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600}[match.group(2).lower()]
    else:
        raise ValueError("invalid TTL: {!r}".format(value))
    if not math.isfinite(ttl) or ttl <= 0:
        raise ValueError("TTL must be positive")
    return ttl


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


def holds_target(scopes: Any, kind: str, value: Any) -> bool:
    """Whether any of the scopes holds the dispatch target ``kind:value``.

    A scope that derives from the target (``host:<host>@health``) holds it as
    well: the hold of a program that must not overwrite another program's
    pause is still seen by the callers that ask about the target, so the
    pause-store interface reports the host as paused while the health check
    holds it (Refs #1367).
    """
    base = "{}:{}".format(kind, value)
    if base in scopes:
        return True
    prefix = base + SCOPE_SUFFIX_SEPARATOR
    return any(str(scope).startswith(prefix) for scope in scopes)


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
        """Whether any active pause applies to the supplied dispatch target.

        A hold of a program that keeps it under a scope of its own
        (``host:<host>@health``) applies to the target as well, so the host
        stays paused while the health check holds it, even when another
        program owned the scope of the host before (Refs #1367).
        """
        if now is None:
            raise ValueError("now is required")
        scopes = {record["scope"] for record in self.active(now)}
        if "all" in scopes:
            return True
        return any(
            value is not None and holds_target(scopes, kind, value)
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
        elif path.name.startswith("PAUSE-BACKEND-") and len(path.name) > len("PAUSE-BACKEND-"):
            scope = "backend:" + path.name[len("PAUSE-BACKEND-"):]
        elif path.name.startswith("PAUSE-") and len(path.name) > len("PAUSE-"):
            scope = "lane:" + path.name[len("PAUSE-"):]
        else:
            continue
        records.append({"scope": scope, "created_at": 0.0, "expires_at": None,
                        "reason": "legacy marker: " + path.name})
    return sorted(records, key=lambda row: row["scope"])
