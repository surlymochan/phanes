"""The persistent FOLLOW-UP queue: a follow-up the throttle dropped is not lost (Refs #1868).

A throttled FOLLOW-UP used to leave nothing but a log line: the text of the follow-up
was never written anywhere, so it was lost and no card was ever built for it.  This
module keeps the dropped items in a file, so the caller can card them later.

The module is storage only: it never runs ``gh``, never creates an issue and never
decides what a follow-up is.  The caller names the file (the path is a parameter, it is
never hardcoded here) and decides when to append and when to drain.

The file is a JSON Lines file: one follow-up per line, in the order they were
appended.  The dedup key of a follow-up is ``(pr, sha, text[:80])``: the same
follow-up written twice for the same head is one item, the same text on another head
is another item.

Every call holds the advisory lock of the file (``flock``) from its first read to its
last write, so an append and a drain of the same file cannot interleave: a follow-up
that was accepted is never thrown away with the file.  When the file cannot be read or
emptied, nothing is drained: a follow-up stays in the file instead of being reported as
taken.
"""

import fcntl
import json
import os
import time
from contextlib import contextmanager

# The dedup key only looks at the head of the text: a follow-up that is longer than
# this is still the same follow-up when its first 80 characters are the same.
DEDUP_TEXT_CHARS = 80


def dedup_key(pr, sha, text):
    """The dedup key of one follow-up: ``(pr, sha, text[:80])``."""
    return (str(pr if pr is not None else ""),
            str(sha if sha is not None else ""),
            str(text or "")[:DEDUP_TEXT_CHARS])


def pr_number(value):
    """The PR number of a follow-up (an int when the value is one, the raw text otherwise)."""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return str(value if value is not None else "")


def _parse(raw):
    """The follow-ups of the raw file text; a torn line is skipped, not fatal."""
    rows = []
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


@contextmanager
def _queue_lock(path, create=True):
    """The advisory lock of one queue file; it is held for the whole call.

    The lock is taken on the queue file itself, so an append and a drain of the same
    file wait for each other instead of interleaving.  Yields False when the file
    cannot be locked: the caller then leaves the file as it is.
    """
    fd = None
    try:
        name = os.fspath(path)
        if create:
            os.makedirs(os.path.dirname(os.path.abspath(name)), exist_ok=True)
        fd = os.open(name, os.O_RDWR | os.O_CREAT if create else os.O_RDONLY, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX)
    except OSError:
        if fd is not None:
            os.close(fd)
        yield False
        return
    try:
        yield True
    finally:
        os.close(fd)


class FollowupQueue:
    """The follow-up queue of one file (the path is the caller's choice)."""

    def __init__(self, path):
        self.path = os.fspath(path)

    def _read(self):
        """The raw text of the queue file, or None when it cannot be read."""
        try:
            with open(self.path, "r", encoding="utf-8") as stream:
                return stream.read()
        except FileNotFoundError:
            return ""
        except OSError:
            return None

    def _clear(self):
        """Empty the queue file; False when the file cannot be emptied."""
        try:
            with open(self.path, "w", encoding="utf-8"):
                pass
        except OSError:
            return False
        return True

    def entries(self):
        """The queued follow-ups, in the order they were appended."""
        with _queue_lock(self.path, create=False) as locked:
            if not locked:
                return []
            raw = self._read()
            return _parse(raw) if raw is not None else []

    def append(self, pr, sha, run, text):
        """Append one follow-up; return True when it was written.

        A follow-up whose dedup key ``(pr, sha, text[:80])`` is already queued is not
        written twice: the throttled follow-up of a head is one item, however often the
        throttle sees it.  An empty text is not a follow-up and is not queued.
        """
        body = str(text or "").strip()
        if not body:
            return False
        row = {"pr": pr_number(pr), "sha": str(sha or ""), "run": str(run or ""),
               "text": body, "at": time.strftime("%F %T")}
        with _queue_lock(self.path) as locked:
            if not locked:
                return False
            raw = self._read()
            if raw is None:
                return False
            keys = {dedup_key(item.get("pr"), item.get("sha"), item.get("text"))
                    for item in _parse(raw)}
            if dedup_key(row["pr"], row["sha"], row["text"]) in keys:
                return False
            try:
                with open(self.path, "a+b") as stream:
                    size = os.fstat(stream.fileno()).st_size
                    prefix = b""
                    if size:
                        # a write that was cut short leaves a tail line without a
                        # newline: end that line first, so the new follow-up starts on
                        # a line of its own and stays readable
                        stream.seek(size - 1)
                        if stream.read(1) != b"\n":
                            prefix = b"\n"
                    line = (json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8")
                    stream.seek(0, os.SEEK_END)
                    stream.write(prefix + line)
                    stream.flush()
                    os.fsync(stream.fileno())
            except OSError:
                return False
        return True

    def drain(self):
        """Take every queued follow-up out of the file and return them.

        The file is emptied only when its entries could be read: when the file cannot
        be read or emptied, nothing is returned and the queue stays as it is, so no
        follow-up is lost.  The lock is held from the read to the emptying, so an
        append that runs at the same time is drained with the file or stays in the
        file: it is never lost.
        """
        with _queue_lock(self.path) as locked:
            if not locked:
                return []
            raw = self._read()
            if raw is None:
                return []
            rows = _parse(raw)
            if not self._clear():
                return []
            return rows

    def __len__(self):
        return len(self.entries())


def append(path, pr, sha, run, text):
    """Append one follow-up to the queue file ``path``."""
    return FollowupQueue(path).append(pr, sha, run, text)


def drain(path):
    """Take every queued follow-up out of the queue file ``path``."""
    return FollowupQueue(path).drain()
