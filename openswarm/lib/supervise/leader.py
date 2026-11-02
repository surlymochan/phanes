"""Fenced control-plane leader lease (Refs #1548)."""
from __future__ import annotations

import fcntl
import json
import os
import subprocess
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path


class FileStore:
    """JSON store protected by an advisory lock across processes."""
    def __init__(self, path):
        self.path = Path(path)
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")

    @contextmanager
    def locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.lock_path, "a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def get(self):
        try:
            with open(self.path, encoding="utf-8") as stream:
                value = json.load(stream)
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def put(self, value):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=self.path.name + ".", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(value, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)


class CommandStore:
    """Command-backed JSON store with an external cross-process lock.

    ``argv_lock`` must acquire the shared lock and print an opaque lock handle;
    ``argv_unlock`` receives that handle as its final argument. The lock must
    cover all clients using the same backing store, including remote hosts.
    """
    def __init__(self, argv_get, argv_put, argv_lock, argv_unlock):
        self.argv_get, self.argv_put = list(argv_get), list(argv_put)
        self.argv_lock, self.argv_unlock = list(argv_lock), list(argv_unlock)
        self._lock = threading.RLock()

    @contextmanager
    def locked(self):
        # The external lock serializes read-modify-write across hosts; this lock
        # also keeps the command sequence orderly for threads in this process.
        with self._lock:
            result = subprocess.run(self.argv_lock, check=True, text=True, capture_output=True)
            handle = result.stdout.strip()
            if not handle:
                raise RuntimeError("lock command must print a lock handle")
            try:
                yield
            finally:
                subprocess.run(self.argv_unlock + [handle], check=True, text=True, capture_output=True)

    def get(self):
        result = subprocess.run(self.argv_get, check=True, text=True, capture_output=True)
        value = json.loads(result.stdout or "{}")
        return value if isinstance(value, dict) else {}

    def put(self, value):
        subprocess.run(self.argv_put, input=json.dumps(value), check=True, text=True, capture_output=True)


class Lease:
    def __init__(self, store, holder, ttl_s, clock):
        if not holder or ttl_s <= 0:
            raise ValueError("holder must be non-empty and ttl_s positive")
        self.store, self.holder, self.ttl_s, self.clock = store, str(holder), float(ttl_s), clock
        self.token = None
        self.stepped_down = False

    def current(self):
        with self.store.locked():
            record = self.store.get().get("lease")
            return dict(record) if isinstance(record, dict) else None

    def acquire(self):
        with self.store.locked():
            data = self.store.get()
            old = data.get("lease") if isinstance(data.get("lease"), dict) else {}
            now = float(self.clock())
            if old and float(old.get("expires_at", 0)) > now and old.get("holder") != self.holder:
                return False
            if old.get("holder") == self.holder and float(old.get("expires_at", 0)) > now:
                self.token = int(old["token"])
            else:
                self.token = int(data.get("token", old.get("token", 0))) + 1
            data["token"] = self.token
            data["lease"] = {"holder": self.holder, "token": self.token, "expires_at": now + self.ttl_s}
            self.store.put(data)
            self.stepped_down = False
            return True

    def renew(self):
        with self.store.locked():
            data = self.store.get()
            record = data.get("lease") or {}
            if self.token is None or record.get("holder") != self.holder or record.get("token") != self.token or float(record.get("expires_at", 0)) <= float(self.clock()):
                self.stepped_down = True
                return False
            record["expires_at"] = float(self.clock()) + self.ttl_s
            data["lease"] = record
            self.store.put(data)
            return True

    def release(self):
        with self.store.locked():
            data = self.store.get()
            record = data.get("lease") or {}
            if record.get("holder") != self.holder or record.get("token") != self.token:
                return False
            data.pop("lease", None)
            self.store.put(data)
            self.token = None
            return True
