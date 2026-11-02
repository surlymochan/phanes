"""Hot-file dispatch guard: parse a card's hot files and bound their concurrency.

A card declares the files it is about to touch with a ``热文件：a, b`` line.  Two cards that
write the same file at the same time corrupt each other, so the dispatcher keeps one in-flight
count per hot file and refuses a card whose hot files already sit at their cap.  Documentation
files (``*.md`` and anything under ``docs/``) are exempt: they are read-mostly, and a conflict
there costs nothing.

This module owns no I/O and reads no configuration: the caps come from the caller so the
dispatcher can wire it in without this module knowing where the policy lives (Refs #1398).
"""
import posixpath
import re

# The card line that declares the hot files; both the Chinese and the ASCII colon are accepted.
HOT_FILE_LINE_RE = re.compile(r"^(?:热文件|Hot-files)\s*[:：]\s*(.*)$", re.I)
# Chinese and English commas, enumeration commas, semicolons and plain spaces all separate paths.
HOT_FILE_SEPARATOR_RE = re.compile(r"[\s,，、;；]+")
# Markdown list markers in front of the declaration (``- 热文件：a.py``).
HOT_FILE_BULLET_RE = re.compile(r"^(?:[-*+•]\s+|\d+[.)]\s+)")
# Documentation suffixes never block a dispatch.
DOC_SUFFIXES = (".md",)
# Directories whose files never block a dispatch.
DOC_DIRS = ("docs/",)


def _normalize(path):
    """Return the canonical form of one hot-file path (repo relative, forward slashes)."""
    text = str(path or "").strip()
    text = text.strip("`\"'").strip()
    text = text.rstrip("。．").strip()
    if not text:
        return ""
    text = text.replace("\\", "/")
    normalized = posixpath.normpath(text)
    if normalized == ".":
        return ""
    return normalized


def is_doc_file(path):
    """Return True for documentation files, which never block a dispatch."""
    text = _normalize(path).lower()
    if not text:
        return False
    if text.endswith(DOC_SUFFIXES):
        return True
    return any(text.startswith(directory) or ("/" + directory) in text for directory in DOC_DIRS)


def parse_hot_files(body):
    """Return the hot files a card declares in its ``热文件：`` line.

    Chinese and English commas as well as plain spaces separate the paths.  A path declared on
    several lines is reported once, in the order it appears.
    """
    files = []
    seen = set()
    for raw in str(body or "").splitlines():
        line = HOT_FILE_BULLET_RE.sub("", raw.strip())
        match = HOT_FILE_LINE_RE.match(line)
        if not match:
            continue
        for token in HOT_FILE_SEPARATOR_RE.split(match.group(1)):
            path = _normalize(token)
            if path and path not in seen:
                seen.add(path)
                files.append(path)
    return files


class HotLock:
    """Bound how many cards may work on the same hot file at the same time.

    ``caps`` maps a hot file to the number of cards allowed to work on it concurrently; files
    without an entry fall back to ``default_cap``.  Caps below one are raised to one, because a
    cap of zero would block the file forever.
    """

    def __init__(self, caps=None, default_cap=1):
        self.default_cap = self._coerce_cap(default_cap, 1)
        self.caps = {}
        for path, cap in (caps or {}).items():
            key = _normalize(path)
            if key:
                self.caps[key] = self._coerce_cap(cap, self.default_cap)
        # In-flight counts, one per hot file, owned by the dispatcher.
        self.counts = {}

    @staticmethod
    def _coerce_cap(value, fallback):
        """Return a usable cap; an unusable value falls back to ``fallback``."""
        try:
            cap = int(value)
        except (TypeError, ValueError):
            cap = int(fallback)
        return max(1, cap)

    def cap_for(self, path):
        """Return the concurrency cap of one hot file."""
        return self.caps.get(_normalize(path), self.default_cap)

    def inflight(self):
        """Return a copy of the in-flight counts."""
        return dict(self.counts)

    def count_for(self, path, inflight=None):
        """Return the in-flight count of one hot file."""
        counts = self.counts if inflight is None else (inflight or {})
        key = _normalize(path)
        for candidate in (key, str(path or "").strip()):
            if candidate in counts:
                try:
                    return int(counts[candidate])
                except (TypeError, ValueError):
                    return 0
        return 0

    def blocked(self, files, inflight=None):
        """Return True when any hot file of the card already sits at its cap.

        ``inflight`` is the per-file in-flight map to judge against; without it the counts this
        lock maintains are used.  Documentation files never block.
        """
        for path in files or []:
            key = _normalize(path)
            if not key or is_doc_file(key):
                continue
            if self.count_for(path, inflight) >= self.cap_for(path):
                return True
        return False

    def acquire(self, files):
        """Take the hot files; return False, and change nothing, when a cap is reached."""
        if self.blocked(files):
            return False
        for path in files or []:
            key = _normalize(path)
            if key:
                self.counts[key] = self.counts.get(key, 0) + 1
        return True

    def release(self, files):
        """Give the hot files back; the card may be dispatched again afterwards."""
        for path in files or []:
            key = _normalize(path)
            if not key:
                continue
            current = self.counts.get(key, 0)
            if current <= 1:
                self.counts.pop(key, None)
            else:
                self.counts[key] = current - 1
