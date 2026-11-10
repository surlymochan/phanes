"""Persistent dispatch pauses shared by fleet health and pause-store callers.

Migrated from openswarm's PauseStore API. Independent owner scopes such as
``host:worker@health`` hold the actual host until that owner resumes them.
"""

import json
import math
import os
import re
import tempfile
import time
from pathlib import Path
from urllib.parse import quote


def pause_dir(home=None):
    """Resolve the shared store, including the legacy environment aliases."""
    configured = os.environ.get("OPENSWARM_PAUSE_DIR") or os.environ.get("XSWARM_PAUSE_DIR")
    if configured:
        return os.path.expanduser(configured)
    home = (home or os.environ.get("OPENSWARM_HOME") or os.environ.get("XSWARM_HOME")
            or "~/.claude/swrm")
    return os.path.join(os.path.expanduser(str(home)), "pauses")


def _scope(value):
    if not isinstance(value, str):
        raise ValueError("scope must be a string")
    value = value.strip()
    if value not in ("all", "mint") and not re.fullmatch(r"(lane|host|backend):[^/\\\s]+", value):
        raise ValueError(f"invalid pause scope: {value!r}")
    return value


def _time(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    return value


class PauseStore:
    """Read and write scoped JSON records under ``home/pauses``.

    OPENSWARM_PAUSE_DIR overrides the directory for both readers and writers.
    A missing TTL means the pause lasts until its owner explicitly resumes it.
    """

    def __init__(self, home=None):
        self.dir = Path(pause_dir(home))

    def path(self, scope):
        return self.dir / (quote(_scope(scope), safe="") + ".json")

    def active(self, now):
        """Return unexpired records, including records from older file naming schemes."""
        moment = _time(now, "now")
        records = []
        try:
            paths = sorted(self.dir.glob("*.json"))
        except OSError:
            return records
        for path in paths:
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(record, dict):
                    continue
                scope = _scope(record.get("scope"))
            except (OSError, ValueError):
                continue
            expires = record.get("expires_at")
            if isinstance(expires, bool) or not isinstance(expires, (int, float)):
                expires = None
            elif expires <= moment:
                continue
            records.append({"scope": scope, "reason": str(record.get("reason") or ""),
                            "created_at": record.get("created_at"), "expires_at": expires})
        return sorted(records, key=lambda row: row["scope"])

    def pause(self, scope, ttl_s=None, reason="", now=None):
        """Create or replace one scope; the clock is explicit for deterministic expiry."""
        scope = _scope(scope)
        moment = _time(now, "now")
        ttl = None if ttl_s is None else _time(ttl_s, "ttl_s")
        if ttl is not None and ttl <= 0:
            raise ValueError("ttl_s must be positive")
        record = {"scope": scope, "created_at": moment,
                  "expires_at": None if ttl is None else moment + ttl, "reason": str(reason)}
        self.dir.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".pause-", dir=str(self.dir))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(record, stream, sort_keys=True)
                stream.write("\n")
            os.replace(temporary, self.path(scope))
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return dict(record)

    def resume(self, scope):
        """Remove only the requested scope, leaving independent owner holds intact."""
        scope = _scope(scope)
        # Older writers used underscore filenames; match the record's exact scope
        # so their pauses can be resumed without touching another owner's hold.
        removed = False
        for path in self.dir.glob("*.json"):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(record, dict) and record.get("scope") == scope:
                try:
                    path.unlink()
                    removed = True
                except FileNotFoundError:
                    pass
        return removed

    def is_paused(self, lane=None, host=None, backend=None, now=None):
        """Whether an active exact or independently owned scope holds this target."""
        if now is None:
            now = time.time()
        scopes = {record["scope"] for record in self.active(now)}
        if "all" in scopes:
            return True
        for kind, value in (("lane", lane), ("host", host), ("backend", backend)):
            if value is None:
                continue
            base = f"{kind}:{value}"
            if base in scopes or any(scope.startswith(base + "@") for scope in scopes):
                return True
        return False
