"""Detect long gaps in a supervisor loop that indicate the box was paused.

The 05:12 incident: the platform froze the whole box for about 26 minutes, so
the control loop and every lane process stopped together.  A supervisor that
wakes up later sees a huge gap between two ticks and has to tell its
neighbours that the box just resumed, so that no watcher mistakes the freeze
for a dead lane.

``PauseDetector(state_dir, threshold_s=60, clock=time.time, mono=time.monotonic)``
    ``tick()`` compares the wall clock of two calls.  A gap larger than
    ``threshold_s`` is a pause: the detector writes ``state_dir/box-resume``
    (the resume epoch only, the format the dispatch engine reads) and appends
    a record to ``state_dir/box-pauses.jsonl``.

``awake_seconds(start, end, pauses)``
    The wall seconds of ``[start, end]`` the box was awake: the stretches
    covered by a pause record are subtracted and clipped to the window.  A
    pause that never ended (no ``resumed``) covers everything up to ``end``.

``recent_resume(state_dir, within_s, now=None)``
    Whether the last resume happened within ``within_s`` seconds, so a
    watchdog can hand out a grace window.

Why the monotonic clock is kept: on Linux ``CLOCK_MONOTONIC`` does not run
while the guest is suspended, but the wall clock does.  A real freeze
therefore shows a wall gap far larger than the monotonic gap, and the
difference is the time the box was not running.  The decision itself follows
the wall clock gap (a gap above the threshold is a pause, a negative gap never
is); the monotonic gap refines the record with ``mono_gap_s`` and
``frozen_s``, and it shows that a backwards clock was a correction of a box
that kept running rather than a freeze.

Standard library only; Python 3.9+.
"""
import argparse
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone

__all__ = ["PauseDetector", "awake_seconds", "recent_resume", "main"]

RESUME_FILE = "box-resume"
PAUSE_FILE = "box-pauses.jsonl"
SOURCE = "pause_detect"

_STARTED_KEYS = ("started", "started_at", "start")
_RESUMED_KEYS = ("resumed", "resume", "resumed_at")


def _epoch(value):
    """Return a POSIX timestamp for a number or an ISO-8601 string, else None.

    The other producers of ``box-pauses.jsonl`` (the box control plane, the
    heartbeat probe) write ISO-8601 timestamps as well, so both are read.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            pass
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    return None


def _first_epoch(record, keys):
    """Return the first usable timestamp of ``keys`` in a mapping."""
    for key in keys:
        value = _epoch(record.get(key))
        if value is not None:
            return value
    return None


def _interval(record):
    """Return the ``(started, resumed)`` of a pause record, or None when unusable.

    ``resumed`` is None for a pause that is still open: it covers everything up
    to the end of the window.  A record whose resume precedes its start is
    broken and dropped, and so is a record that is not a pause at all (a clock
    adjustment carries no ``started``/``resumed``).
    """
    if not isinstance(record, dict):
        return None
    started = _first_epoch(record, _STARTED_KEYS)
    if started is None:
        return None
    resumed = _first_epoch(record, _RESUMED_KEYS)
    if resumed is not None and resumed < started:
        return None
    return started, resumed


def _local(moment):
    """Return the human readable local time of an epoch."""
    try:
        return datetime.fromtimestamp(moment).strftime("%Y-%m-%d %H:%M:%S")
    except (OverflowError, OSError, ValueError):
        return None


def awake_seconds(start, end, pauses):
    """Return the wall seconds of ``[start, end]`` the box was awake.

    The stretches the box spent paused are not awake time: a lane could not
    produce output then.  Pauses are clipped to the window and a pause that
    never ended reaches to the end of the window.
    """
    start, end = _epoch(start), _epoch(end)
    if start is None or end is None or end <= start:
        return 0.0
    intervals = []
    for record in pauses or []:
        pair = _interval(record)
        if pair is None:
            continue
        pause_start, pause_end = pair
        left = max(start, pause_start)
        stop = end if pause_end is None else min(pause_end, end)
        if stop <= left:
            continue
        intervals.append((left, stop))
    intervals.sort()
    paused = 0.0
    covered_until = start
    for left, right in intervals:
        if right <= covered_until:
            continue
        paused += right - max(left, covered_until)
        covered_until = right
    return max(0.0, (end - start) - paused)


def _read_records(path):
    """Yield the JSON objects of a JSONL file, skipping the broken lines."""
    try:
        with open(path, "r", encoding="utf-8") as stream:
            for line in stream:
                try:
                    item = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if isinstance(item, dict):
                    yield item
    except OSError:
        return


def recent_resume(state_dir, within_s, now=None):
    """Return whether the latest resume is no older than ``within_s``.

    Clock adjustments are not resumes and are skipped, so a backwards clock
    never hands out a grace window that never happened.
    """
    within = _epoch(within_s)
    moment = time.time() if now is None else _epoch(now)
    if within is None or moment is None or within < 0:
        return False
    latest = None
    for record in _read_records(os.path.join(os.fspath(state_dir), PAUSE_FILE)):
        if record.get("clock_adjustment"):
            continue
        resumed = _first_epoch(record, _RESUMED_KEYS)
        started = _first_epoch(record, _STARTED_KEYS)
        if resumed is None or started is None or resumed < started:
            continue
        latest = resumed if latest is None else max(latest, resumed)
    return latest is not None and 0 <= moment - latest <= within


class PauseDetector:
    """Watch the wall clock of a supervisor loop and record the pauses."""

    def __init__(self, state_dir, threshold_s=60, clock=time.time, mono=time.monotonic):
        self.state_dir = os.fspath(state_dir)
        self.threshold_s = float(threshold_s)
        self.clock = clock
        self.mono = mono
        self._last = None
        self._last_mono = None

    # ------------------------------------------------------------------ files
    def _record(self, record):
        """Append one JSON record to the pause log."""
        try:
            os.makedirs(self.state_dir, exist_ok=True)
            with open(os.path.join(self.state_dir, PAUSE_FILE), "a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, sort_keys=True) + "\n")
        except OSError as error:
            print("pause_detect: cannot write %s: %s" % (PAUSE_FILE, error), file=sys.stderr)

    def _write_resume(self, now):
        """Write the resume marker atomically: the epoch only.

        The dispatch engine reads it with ``float()`` and the control plane
        with ``timestamp()``; both need a bare number, so the readable time
        lives in the pause record instead.
        """
        try:
            os.makedirs(self.state_dir, exist_ok=True)
            target = os.path.join(self.state_dir, RESUME_FILE)
            handle, temporary = tempfile.mkstemp(prefix=".box-resume-", dir=self.state_dir)
            try:
                with os.fdopen(handle, "w", encoding="utf-8") as stream:
                    stream.write("%r\n" % float(now))
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, target)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            self._fsync_dir()
        except OSError as error:
            print("pause_detect: cannot write %s: %s" % (RESUME_FILE, error), file=sys.stderr)

    def _fsync_dir(self):
        """Flush the directory entry so the marker survives a crash."""
        try:
            handle = os.open(self.state_dir, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(handle)
        except OSError:
            pass
        finally:
            os.close(handle)

    # ------------------------------------------------------------------- tick
    def _mono_now(self):
        """Return the monotonic reading, or None when there is no usable one."""
        if self.mono is None:
            return None
        try:
            return float(self.mono())
        except (TypeError, ValueError):
            return None

    def tick(self):
        """Compare the wall clock with the previous tick and record a pause.

        Returns ``{"paused_s": gap, "resumed_at": now}`` for a pause and None
        otherwise.  A wall clock that went backwards is recorded as a clock
        adjustment, never as a pause.
        """
        try:
            now = float(self.clock())
        except (TypeError, ValueError):
            return None
        mono_now = self._mono_now()
        previous, previous_mono = self._last, self._last_mono
        self._last, self._last_mono = now, mono_now
        if previous is None:
            return None
        gap = now - previous
        mono_gap = None
        if mono_now is not None and previous_mono is not None:
            mono_gap = mono_now - previous_mono
        if gap < 0:
            # The wall clock went backwards: a clock correction, not a pause.
            record = {"clock_adjustment": True, "gap_s": gap,
                      "from": previous, "to": now, "source": SOURCE}
            if mono_gap is not None:
                record["mono_gap_s"] = mono_gap
                # The guest kept running, so the box did not freeze.
                record["frozen_s"] = 0.0
            self._record(record)
            return None
        if gap <= self.threshold_s:
            return None
        record = {"started": previous, "resumed": now, "gap_s": gap, "source": SOURCE}
        record["resumed_local"] = _local(now)
        if mono_gap is not None:
            record["mono_gap_s"] = mono_gap
            record["frozen_s"] = max(0.0, gap - mono_gap)
        # The record comes first: a resume marker without a pause record would
        # hand out a grace window that nobody can account for.
        self._record(record)
        self._write_resume(now)
        return {"paused_s": gap, "resumed_at": now}


def main(argv=None):
    parser = argparse.ArgumentParser(prog="pause_detect.py", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command")
    tail = commands.add_parser("tail", help="print the recent pause records")
    tail.add_argument("--state-dir", required=True)
    tail.add_argument("-n", type=int, default=5)
    args = parser.parse_args(argv)
    if args.command != "tail":
        parser.print_help()
        return 2
    if args.n < 0:
        parser.error("-n must be non-negative")
    records = list(_read_records(os.path.join(os.fspath(args.state_dir), PAUSE_FILE)))
    for record in records[-args.n:] if args.n else []:
        print(json.dumps(record, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
