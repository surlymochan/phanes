"""LRU lane picking for the dispatcher (pure module, no CLI wiring).

The dispatcher has to decide *which* lane gets the next issue when several lanes are
idle.  Round-robin ignores how long a lane has been waiting; this picker keeps a small
JSON state file that remembers the last dispatch of every lane and returns the lane
that waited longest.

``pick()`` is read-only: it never writes the state file.  ``touch()`` records a
dispatch and writes the state back atomically (temp file + rename), so a crash or a
concurrent reader never sees a half written state file.

State file format (version 1)::

    {"version": 1, "counter": 7, "lanes": {"alpha": 5, "beta": 7}}

``lanes[lane]`` is the dispatch counter of the last dispatch of that lane; a bigger
number means a more recent dispatch.  A lane that is missing from ``lanes`` was never
dispatched and therefore waits longest.  The counter is a monotonic sequence instead
of a wall clock timestamp so the order stays stable across clock jumps and so two
lanes dispatched in the same second keep their real order.

A damaged state file is never trusted partially: one unusable lane name or counter
makes the whole file damaged, and the picker then starts over with a fresh state.
Keeping the readable half of a broken history would pick lanes by an order that is
only half true.

Refs #1396 (part of #1360).
"""
import json
import os
import tempfile

STATE_VERSION = 1


def fresh_state():
    """Return an empty state: no lane was dispatched yet."""
    return {"version": STATE_VERSION, "counter": 0, "lanes": {}}


def _is_counter(value):
    """A counter is a non-negative integer; booleans and floats are not counters."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_lane_name(value):
    """A lane name is a non-empty string; anything else means the state file is damaged."""
    return isinstance(value, str) and bool(value.strip())


def load_state(state_path):
    """Read the state file, falling back to a fresh state when it is missing or damaged.

    A damaged state file must not stop the dispatcher: the lanes simply look like they
    were never dispatched, so the picker starts over with a fresh state.  A single
    unusable entry (a lane name that is not a name, a counter that is not a counter)
    makes the whole file unusable, so the whole state is replaced by ``fresh_state()``
    instead of keeping the entries that happen to be readable.
    """
    try:
        with open(state_path, encoding="utf-8") as stream:
            raw = json.load(stream)
    except (OSError, ValueError):
        return fresh_state()
    if not isinstance(raw, dict):
        return fresh_state()
    if raw.get("version", STATE_VERSION) != STATE_VERSION:
        return fresh_state()
    lanes = raw.get("lanes")
    if not isinstance(lanes, dict):
        return fresh_state()
    counter = raw.get("counter")
    if not _is_counter(counter):
        return fresh_state()
    for lane, lane_counter in lanes.items():
        if not _is_lane_name(lane) or not _is_counter(lane_counter):
            return fresh_state()
    if lanes:
        counter = max(counter, max(lanes.values()))
    return {"version": STATE_VERSION, "counter": counter, "lanes": dict(lanes)}


def save_state(state_path, state):
    """Write the state atomically: temp file in the target directory, then rename."""
    directory = os.path.dirname(os.path.abspath(state_path))
    os.makedirs(directory, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=os.path.basename(state_path) + ".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(state, stream, indent=2, ensure_ascii=False, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, state_path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class LruPicker:
    """Pick the lane that waited longest for a dispatch.

    ``state_path`` is the JSON file that remembers the last dispatch of every lane.
    It is read on every call, so several dispatchers can share one state file.
    """

    def __init__(self, state_path):
        self.state_path = state_path

    def state(self):
        """The current state as read from disk (fresh state when missing or damaged)."""
        return load_state(self.state_path)

    def pick(self, candidates, paused=frozenset()):
        """Return the candidate lane that was dispatched longest ago.

        A lane that was never dispatched waits longest and therefore comes first.
        Lanes that tie are ordered by name, so the choice is stable and reproducible.
        Paused lanes are skipped.  ``None`` is returned when no candidate is left.
        """
        paused = set(paused or ())
        available = [lane for lane in candidates if lane not in paused]
        if not available:
            return None
        lanes = self.state()["lanes"]

        def sort_key(lane):
            counter = lanes.get(lane)
            if counter is None:
                return (0, 0, lane)
            return (1, counter, lane)

        return min(available, key=sort_key)

    def touch(self, lane):
        """Record a dispatch of ``lane`` and write the state back atomically."""
        state = self.state()
        counter = state["counter"] + 1
        state["counter"] = counter
        state["lanes"][lane] = counter
        save_state(self.state_path, state)
        return counter
