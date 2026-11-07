"""Persistent, scoped dispatch pauses (Refs #1402, part of #1366).

Pause records live as JSON files in ``<home>/pauses``.  The module has no
knowledge of the dispatch loop; callers pass the clock explicitly and decide
when a matching pause should affect scheduling.

``$OPENSWARM_HOME`` is the canonical home of the pause store (see
``default_home()``); the dispatcher keeps its own copy under its state
directory, so both are read.

The file name of a record is derived from its scope, and the derivation is
injective: two different scopes never share a file (Refs #1829).  A scope that
only uses letters, digits, ``_``, ``.`` and ``-`` keeps its name; every other
byte is escaped reversibly (``lane:C1`` becomes ``lane~3aC1``) and the stem
ends in the digest of the scope.  The digest keeps two scopes apart even when
their stems differ in case only, which a case-insensitive file system maps
onto one file.  A record is read by the scope it carries, so a file whose
``scope`` field names another scope is not that scope's record.

Migration (Refs #1829): the records written before #1829 keep their pause.
They are read by the scope they carry, wherever they live, and the next write
of that scope moves the record to the current name.  A file that several
scopes shared (the old name mapped every unsafe byte to ``_``) holds one
record: the scope it carries wins, and every other scope gets a file of its
own.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional


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
    """The normalized scope string.

    A scope is ``all``, ``mint``, ``<kind>:<value>`` or the underscore form
    ``<kind>_<value>``.  The underscore form is a scope of its own: it is the
    name the store gave a scope before #1829, and it must not share a file
    with the colon form, or one scope would clear the other (Refs #1829).
    """
    if not isinstance(value, str):
        raise ValueError("scope must be a string, got {!r}".format(value))
    value = value.strip()
    if value in ("all", "mint"):
        return value
    match = re.fullmatch(r"(lane|host|backend):([^/\\]+)", value)
    if match and match.group(2).strip():
        return "{}:{}".format(match.group(1), match.group(2).strip())
    if re.fullmatch(r"(lane|host|backend)_[A-Za-z0-9_.-]+", value):
        return value
    raise ValueError("invalid pause scope: {!r}".format(value))


# The bytes a scope file name may carry verbatim; every other byte of a scope
# is escaped, so the scope -> file name mapping stays injective (Refs #1829).
_SAFE_NAME_BYTES = frozenset(
    b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-")

# A file name holds 255 bytes at most; the stem keeps room for the ".json"
# suffix and for the digest of the scope.
_MAX_STEM_BYTES = 200

# The length of the digest that ends the stem of an escaped scope: two scopes
# whose stems differ in case only must not land on one file, which a
# case-insensitive file system would do (Refs #1829).
_DIGEST_LENGTH = 12


def _digest(scope: str) -> str:
    """A short digest of one scope; it tells two scopes apart."""
    return hashlib.sha256(scope.encode("utf-8")).hexdigest()[:_DIGEST_LENGTH]


def _escaped(scope: str) -> str:
    """The scope with every byte a file name may not carry escaped.

    An unsafe byte becomes ``~`` plus two hex digits (``~`` itself as ``~7e``),
    so the escaping is reversible.
    """
    name = []
    for byte in scope.encode("utf-8"):
        if byte in _SAFE_NAME_BYTES:
            name.append(chr(byte))
        else:
            name.append("~{:02x}".format(byte))
    return "".join(name)


def _scope_key(scope: str) -> str:
    """The file name stem of one scope; the mapping is injective (Refs #1829).

    A scope that only uses letters, digits, ``_``, ``.`` and ``-`` keeps its
    name, so the records written before #1829 stay where they are.  Every
    other scope is escaped (``lane:C1`` becomes ``lane~3aC1``) and its stem
    ends in the digest of the scope: the digest keeps two scopes apart even
    when their stems differ in case only, which a case-insensitive file
    system maps onto one file.  A stem too long for a file name is cut short;
    the digest still tells the scopes apart.
    """
    escaped = _escaped(scope)
    if escaped == scope and len(scope.encode("utf-8")) <= _MAX_STEM_BYTES:
        return scope
    stem = escaped[:_MAX_STEM_BYTES].rstrip("~")
    return "{}~{}".format(stem, _digest(scope))


def _own_key(scope: str) -> str:
    """A file name stem that belongs to one scope alone (Refs #1829).

    It is the fallback for a scope whose own name is taken by another scope:
    a case-insensitive file system maps two safe names that differ in case
    only onto one file.
    """
    stem = _escaped(scope)[:_MAX_STEM_BYTES].rstrip("~")
    return "{}-{}".format(stem, _digest(scope))


def _legacy_scope_key(scope: str) -> str:
    """The file name stem the store used before #1829 (every unsafe char ``_``).

    That mapping was not injective (``lane:X`` and ``lane_X`` shared a file),
    so a record written before the upgrade may still live under this name; the
    store reads it by the scope it carries.
    """
    return re.sub(r"[^A-Za-z0-9_.-]", "_", scope)


class PauseStore:
    """Read and write pauses under ``home/pauses``."""

    def __init__(self, home: Any) -> None:
        self.dir = Path(home) / "pauses"

    def _path(self, scope: str) -> Path:
        return self.dir / (_scope_key(scope) + ".json")

    def _legacy_path(self, scope: str) -> Path:
        return self.dir / (_legacy_scope_key(scope) + ".json")

    def _write_path(self, scope: str) -> Path:
        """The file the record of one scope is written to.

        The name of a scope is its own.  A case-insensitive file system maps
        two safe names that differ in case only onto one file, so the scope
        that finds its name taken by another scope gets a name of its own
        (Refs #1829).
        """
        path = self._path(scope)
        record = self._read(path)
        if record is None or record["scope"] == scope:
            return path
        own = self.dir / (_own_key(scope) + ".json")
        other = self._read(own)
        if other is None or other["scope"] == scope:
            return own
        for attempt in range(1, 16):
            candidate = self.dir / ("{}-{}.json".format(_own_key(scope), attempt))
            other = self._read(candidate)
            if other is None or other["scope"] == scope:
                return candidate
        return path

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

    def _read_scope(self, path: Path, scope: str) -> Optional[Dict[str, Any]]:
        """The record of one scope stored at ``path``.

        A record whose ``scope`` field names another scope is not the record of
        ``scope``: for that scope the file does not exist (Refs #1829).
        """
        record = self._read(path)
        if record is None or record["scope"] != scope:
            return None
        return record

    def _record_paths(self) -> List[Path]:
        """Every pause file of the store."""
        try:
            return sorted(self.dir.glob("*.json"))
        except OSError:
            return []

    def _drop_others(self, scope: str, keep: Path) -> None:
        """Remove the other copies of one scope's record.

        The old file name mapped several scopes onto one file, so a record
        written before the upgrade may still live under it; the record that
        survived belongs to the scope it carries, and the next write of that
        scope moves the record to the current name (Refs #1829).
        """
        for path in self._record_paths():
            if path == keep:
                continue
            if self._read_scope(path, scope) is None:
                continue
            try:
                path.unlink()
            except OSError:
                pass

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
        target = self._write_path(normalized)
        fd, temporary = tempfile.mkstemp(prefix=".pause-", dir=str(self.dir))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(record, stream, sort_keys=True)
                stream.write("\n")
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        self._drop_others(normalized, target)
        return dict(record)

    def resume(self, scope: str) -> bool:
        """Remove one scope's pause; return whether a record existed.

        Every copy of the record goes: the file of the current name and the
        leftovers of the old mapping (Refs #1829).
        """
        normalized = _scope(scope)
        removed = False
        for path in self._record_paths():
            if self._read_scope(path, normalized) is None:
                continue
            try:
                path.unlink()
            except OSError:
                continue
            removed = True
        return removed

    def _rank(self, scope: str, path: Path) -> int:
        """How current the file of one scope is; the lower the better.

        The file of the current name wins over a leftover of the old mapping
        (Refs #1829).
        """
        if path == self._path(scope):
            return 0
        if path == self._legacy_path(scope):
            return 2
        return 1

    def active(self, now: Any) -> List[Dict[str, Any]]:
        """Return unexpired pauses, sorted lexically by scope."""
        moment = _time(now, "now")
        records: Dict[str, Dict[str, Any]] = {}
        paths: Dict[str, Path] = {}
        for path in self._record_paths():
            record = self._read(path)
            if record is None:
                continue
            expiry = record["expires_at"]
            if expiry is not None and moment >= expiry:
                continue
            scope = record["scope"]
            # one record per scope: the file of the current mapping wins over a
            # leftover of the old one (Refs #1829)
            if scope not in records or self._rank(scope, path) < self._rank(scope, paths[scope]):
                records[scope] = record
                paths[scope] = path
        return sorted(records.values(), key=lambda row: row["scope"])

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


def marker_scope(name: str) -> Optional[str]:
    """The pause scope of a legacy marker file name, or None when it is none."""
    if name == "PAUSE":
        return "all"
    if name == "PAUSE-MINT":
        return "mint"
    for prefix, kind in (("PAUSE-HOST-", "host"), ("PAUSE-BACKEND-", "backend")):
        if name.startswith(prefix) and len(name) > len(prefix):
            return "{}:{}".format(kind, name[len(prefix):])
    if name.startswith("PAUSE-") and len(name) > len("PAUSE-"):
        return "lane:" + name[len("PAUSE-"):]
    return None


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
        scope = marker_scope(path.name)
        if scope is None:
            continue
        records.append({"scope": scope, "created_at": 0.0, "expires_at": None,
                        "reason": "legacy marker: " + path.name})
    return sorted(records, key=lambda row: row["scope"])


def default_home() -> str:
    """The canonical openswarm home: ``$OPENSWARM_HOME``, ``$XSWARM_HOME`` or ``~/.claude/openswarm``.

    The CLI writes the pause records into this home, so every reader (the
    dispatcher, the lane serve, the autodispatcher) must resolve it the same
    way: an unset ``$OPENSWARM_HOME`` still has a home, and the default
    install must see the pauses the CLI wrote (Refs #1366).
    """
    return (os.environ.get("OPENSWARM_HOME") or os.environ.get("XSWARM_HOME")
            or os.path.join(os.path.expanduser("~"), ".claude", "openswarm"))


def pause_homes(*extra: Any) -> List[str]:
    """The pause store homes: the given ones plus the canonical home.

    The canonical home of the pause records is ``default_home()``; a caller
    that keeps its own store (the dispatcher keeps one under its state
    directory) passes it as ``extra``.  The result is deduplicated and absolute.
    """
    homes: List[str] = []
    for home in list(extra) + [default_home()]:
        if not home:
            continue
        path = os.path.abspath(os.path.expanduser(str(home)))
        if path not in homes:
            homes.append(path)
    return homes


def active_scopes(homes: Any, now: Any) -> set:
    """The scopes that are paused right now, across every home."""
    moment = _time(now, "now")
    scopes = set()
    for home in homes or []:
        for record in PauseStore(home).active(moment):
            scopes.add(record["scope"])
    return scopes


def scope_paused(homes: Any, scope: str, now: Any) -> bool:
    """Whether one exact scope (``all``, ``mint``, ``lane:X`` ...) is paused."""
    return _scope(scope) in active_scopes(homes, now)


def paused_in(homes: Any, lane: Any = None, host: Any = None, backend: Any = None, now: Any = None) -> bool:
    """Whether any of the pause stores holds a pause that covers the target."""
    if now is None:
        raise ValueError("now is required")
    for home in homes:
        if PauseStore(home).is_paused(lane=lane, host=host, backend=backend, now=now):
            return True
    return False
