"""Persistent, scoped dispatch pauses (Refs #1402, part of #1366).

Pause records live as JSON files in ``<home>/pauses``.  The module has no
knowledge of the dispatch loop; callers pass the clock explicitly and decide
when a matching pause should affect scheduling.

Every switch carries its provenance: ``source`` (who set it), ``created_at``
(when) and ``basis`` (on what authority).  A switch without that provenance,
or one that is older than the freshness window, cannot be told apart from a
snapshot restored from an older run: on 2026-11-02 a PAUSE-DISPATCH /
PAUSE-MINT pair was restored from a 2026-11-01 record ("was missing on gocc
ctl") and froze the dispatch for ten minutes against the current
authorisation (Refs #1669).  Such a switch is never honoured: the caller
warns about it and asks for a review instead.

The provenance of a marker file travels with the file: a marker records its
creator, its creation time and its authority in the marker itself, so a copy
of an old marker (which refreshes its mtime) cannot make it look fresh.  A
value nobody stated is never invented: a switch that names no creator is
recorded without one and reported for a review.
"""
from __future__ import annotations

import datetime
import json
import math
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

# An open-ended switch (no TTL) is trusted for this long.  A switch that
# carries a deadline is governed by that deadline instead.
DEFAULT_MAX_AGE_S = 24 * 3600
# The keys a marker file may carry to record the provenance of its switch.
PROVENANCE_KEYS = ("source", "created_at", "basis", "until")


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


def _text(value: Any) -> str:
    """A trimmed string field; ``None`` and blanks read as "not stated"."""
    return str(value or "").strip()


def marker_scope(name: Any) -> str:
    """The pause scope a legacy ``PAUSE*`` marker file name stands for.

    ``PAUSE`` is the whole loop, ``PAUSE-MINT`` the card minting, the
    ``PAUSE-HOST-`` / ``PAUSE-BACKEND-`` / ``PAUSE-`` prefixes the host, the
    backend and the lane.  An unknown name has no scope.
    """
    name = str(name or "")
    if name == "PAUSE":
        return "all"
    if name == "PAUSE-MINT":
        return "mint"
    if name.startswith("PAUSE-HOST-") and len(name) > len("PAUSE-HOST-"):
        return "host:" + name[len("PAUSE-HOST-"):]
    if name.startswith("PAUSE-BACKEND-") and len(name) > len("PAUSE-BACKEND-"):
        return "backend:" + name[len("PAUSE-BACKEND-"):]
    if name.startswith("PAUSE-") and len(name) > len("PAUSE-"):
        return "lane:" + name[len("PAUSE-"):]
    return ""


def marker_name(scope: Any) -> str:
    """The marker file name a pause scope is written as.

    The inverse of :func:`marker_scope`: ``all`` is ``PAUSE``, ``mint`` is
    ``PAUSE-MINT`` and the host, backend and lane scopes carry their own
    prefix.
    """
    normalized = _scope(scope)
    if normalized == "all":
        return "PAUSE"
    if normalized == "mint":
        return "PAUSE-MINT"
    kind, _, value = normalized.partition(":")
    if kind == "host":
        return "PAUSE-HOST-" + value
    if kind == "backend":
        return "PAUSE-BACKEND-" + value
    return "PAUSE-" + value


def parse_deadline(value: Any) -> Optional[float]:
    """A deadline: epoch seconds or an ISO-8601 timestamp."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        pass
    try:
        parsed = datetime.datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.timestamp()


def _deadline(body: Any) -> Optional[float]:
    """The ``until=`` deadline of a marker body (epoch seconds or ISO-8601)."""
    match = re.search(r"\buntil=(\S+)", str(body or ""))
    if not match:
        return None
    return parse_deadline(match.group(1))


def parse_marker_body(text: Any) -> tuple:
    """Split a marker file into its body and the provenance it records.

    The body is the text that is not a provenance line; the provenance lines
    are ``key=value`` pairs whose key is one of :data:`PROVENANCE_KEYS`.
    """
    body_lines: List[str] = []
    fields: Dict[str, str] = {}
    for raw in str(text or "").splitlines():
        line = raw.strip()
        key, sep, value = line.partition("=")
        key = key.strip().lower()
        if sep and key in PROVENANCE_KEYS:
            fields[key] = value.strip()
            continue
        body_lines.append(line)
    return "\n".join(body_lines).strip(), fields


def format_marker(body: Any = "", source: Any = None, basis: Any = None,
                  created_at: Any = None, until: Any = None) -> str:
    """Render a marker file: the body first, then the provenance of the switch.

    The body keeps the legacy convention (an ``auto:`` body was written by an
    automatic guard, any other body by the operator), so the readers that look
    at the first line keep working.  The provenance lines carry the creator,
    the creation time and the authority of the switch; they travel with the
    file, so a copy of an old marker does not make it look fresh (Refs #1669).
    """
    lines: List[str] = []
    text = str(body or "").strip()
    if text:
        lines.append(text)
    if source:
        lines.append("source=" + str(source).strip())
    if created_at is not None:
        lines.append("created_at={}".format(float(created_at)))
    if basis:
        lines.append("basis=" + str(basis).strip())
    if until is not None:
        lines.append("until={}".format(until))
    return "\n".join(lines) + "\n"


def marker_switch(path: Any, name: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Read a legacy ``PAUSE*`` marker file as a pause switch.

    The body of the marker names what it is: an ``auto:`` body was written by
    an automatic guard, any other body by the operator.  A marker may record
    the provenance of its switch in the lines after the body::

        source=operator:main
        created_at=1762100000
        basis=issue #1669
        until=1762103600

    The declared ``created_at`` is the creation time of the switch, so a copy
    of an old marker (which refreshes the file mtime) does not make it look
    fresh.  A marker that names no creator and no authority is unattributed:
    the dispatcher asks for a review instead of honouring it, and the file
    mtime is not taken for its creation time.
    """
    file_path = Path(path)
    try:
        text = file_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    body, fields = parse_marker_body(text)
    marker = str(name or file_path.name)
    expires_at = parse_deadline(fields["until"]) if fields.get("until") else _deadline(body)
    source = _text(fields.get("source"))
    basis = _text(fields.get("basis"))
    if body.startswith("auto:"):
        parts = body.split(":", 2)
        if not source:
            source = "auto:" + (parts[1].strip() if len(parts) > 1 else "")
        if not basis:
            basis = body
    created_at = None
    if fields.get("created_at"):
        try:
            created_at = float(fields["created_at"])
        except (TypeError, ValueError):
            created_at = None
    if created_at is None:
        # File mtime changes on copy and cannot prove the original creation time.
        created_at = 0.0
    return {"scope": marker_scope(marker), "created_at": created_at,
            "expires_at": expires_at, "reason": body,
            "source": source, "basis": basis, "marker": marker}


def switch_problems(switch: Any, now: Any, max_age_s: Any = DEFAULT_MAX_AGE_S) -> List[str]:
    """Why a pause switch may not be honoured (an empty list means it may).

    * ``expired``      - the TTL/deadline of the switch has passed
    * ``no-source``    - nobody signed the switch (unknown creator)
    * ``no-basis``     - the switch names no authority
    * ``no-timestamp`` - the switch carries no creation time
    * ``stale``        - an open-ended switch is older than ``max_age_s``
    """
    moment = _time(now, "now")
    switch = switch or {}
    problems: List[str] = []
    expires_at = switch.get("expires_at")
    if expires_at is not None:
        expires_at = _time(expires_at, "expires_at")
        if moment >= expires_at:
            problems.append("expired")
    if not _text(switch.get("source")):
        problems.append("no-source")
    if not _text(switch.get("basis")):
        problems.append("no-basis")
    try:
        created_at = float(switch.get("created_at"))
    except (TypeError, ValueError):
        created_at = 0.0
    if created_at <= 0:
        problems.append("no-timestamp")
    elif expires_at is None:
        try:
            limit = float(max_age_s)
        except (TypeError, ValueError):
            limit = DEFAULT_MAX_AGE_S
        if limit > 0 and moment - created_at > limit:
            problems.append("stale")
    return problems


def review_switch(switch: Any, now: Any, max_age_s: Any = DEFAULT_MAX_AGE_S) -> Dict[str, Any]:
    """The review row of one pause switch: its provenance and its problems."""
    switch = switch or {}
    problems = switch_problems(switch, now, max_age_s)
    row = {"scope": switch.get("scope"), "source": _text(switch.get("source")),
           "basis": _text(switch.get("basis")), "created_at": switch.get("created_at"),
           "expires_at": switch.get("expires_at"), "reason": str(switch.get("reason", "")),
           "problems": problems, "needs_review": bool(problems)}
    if switch.get("marker"):
        row["marker"] = switch["marker"]
    return row


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
        record["source"] = _text(record.get("source"))
        record["basis"] = _text(record.get("basis"))
        return record

    def pause(self, scope: str, ttl_s: Any = None, reason: str = "", now: Any = None,
              source: Any = None, basis: Any = None) -> Dict[str, Any]:
        """Create or replace a pause. ``now`` is required for deterministic expiry.

        ``source`` names the creator of the switch and ``basis`` the authority
        it is set on (an issue, a command, a probe result).  Both end up in the
        record as they are given: a value nobody stated is never invented, so
        a switch that names no creator is recorded without one and reported
        for a review -- it cannot be told apart from a snapshot restored from
        an older run (Refs #1669).  The reason is used as the basis when the
        caller names no basis.
        """
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
            "source": _text(source),
            "basis": _text(basis if basis is not None else reason),
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

    def records(self) -> List[Dict[str, Any]]:
        """Every pause record on disk, sorted lexically by scope."""
        records = []
        try:
            paths = list(self.dir.glob("*.json"))
        except OSError:
            return records
        for path in paths:
            record = self._read(path)
            if record is not None:
                records.append(record)
        return sorted(records, key=lambda row: row["scope"])

    def active(self, now: Any) -> List[Dict[str, Any]]:
        """Return unexpired pauses, sorted lexically by scope."""
        moment = _time(now, "now")
        records = []
        for record in self.records():
            expiry = record["expires_at"]
            if expiry is not None and moment >= expiry:
                continue
            records.append(record)
        return records

    def effective(self, now: Any, max_age_s: Any = DEFAULT_MAX_AGE_S) -> List[Dict[str, Any]]:
        """Return the pauses that may be honoured: unexpired and with sound provenance."""
        return [record for record in self.active(now)
                if not switch_problems(record, now, max_age_s)]

    def review(self, now: Any, max_age_s: Any = DEFAULT_MAX_AGE_S) -> List[Dict[str, Any]]:
        """Review row of every record on disk, expired ones included."""
        return [review_switch(record, now, max_age_s) for record in self.records()]

    def is_paused(self, lane: Any = None, host: Any = None, backend: Any = None, now: Any = None,
                  max_age_s: Any = DEFAULT_MAX_AGE_S) -> bool:
        """Whether any trustworthy active pause applies to the supplied dispatch target."""
        if now is None:
            raise ValueError("now is required")
        scopes = {record["scope"] for record in self.effective(now, max_age_s)}
        if "all" in scopes:
            return True
        return any(
            value is not None and "{}:{}".format(kind, value) in scopes
            for kind, value in (("lane", lane), ("host", host), ("backend", backend))
        )


def legacy_markers(directory: Any) -> List[Dict[str, Any]]:
    """Map old PAUSE marker files into pause records, sorted by scope.

    A marker carries no clock of its own, so the file mtime is the creation
    time of the switch and its body is the basis.
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
        if not scope:
            continue
        switch = marker_switch(path, name=path.name)
        if switch is None:
            continue
        records.append({"scope": scope, "created_at": switch["created_at"],
                        "expires_at": switch["expires_at"],
                        "reason": switch["reason"] or ("legacy marker: " + path.name),
                        "source": switch["source"], "basis": switch["basis"]})
    return sorted(records, key=lambda row: row["scope"])
