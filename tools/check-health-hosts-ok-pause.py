#!/usr/bin/env python3
"""Offline contract for the health summary vs the PAUSE-HOST cooldown set (Refs #948).

The box runtime (``~/phanes-ctl/bin/health-check.py``) probes every fleet host
and writes the summary to ``~/phanes-ctl/autodispatch/health.json``.  A held
host has a ``PAUSE-HOST-<host>`` file next to it: health-loop writes those
bodies with an ``auto:`` prefix (automatic cooldown), while a manual
``touch PAUSE-HOST-<host>`` carries no ``auto:`` prefix but holds the host just
the same.

The contract asserted here:

* ``hosts_ok`` never contains a host with a ``PAUSE-HOST-<host>`` file;
  ``skipped_cooldown`` lists exactly the healthy hosts that were skipped
  because such a file exists, so the two fields partition the healthy probes.
* an ``auto:`` pause is released once it expired, mirroring ``clear_auto`` in
  ``tools/check-health-pause.py``: the pause must be older than ``PAUSE_TTL``
  and an ``auto:cooldown:until=<deadline>`` body must be past its deadline.
  There is no probe condition, so an expired pause is released even while the
  host still probes degraded.  A pause that has not expired is never cleared,
  and a manual pause (body not starting with ``auto:``) is never cleared at
  all.  The mem hard pauses (``auto:host_hard`` / ``auto:mem_hard``) are
  additionally removed by the health self-heal path as soon as the host
  recovers.

Everything runs on fixtures inside a temporary directory: a fake ``health.json``
and fake ``PAUSE-HOST-*`` files.  The real ``~/phanes-ctl/autodispatch/PAUSE-*``
files are never read, written or removed.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

# TTL of an automatic PAUSE file in seconds.  Mirrors the mirrored specs
# tools/check-health-pause-ttl.py (PAUSE_TTL) and tools/check-health-pause.py
# (QUOTA_HOLD): an auto pause is only released once it expired.
PAUSE_TTL = 3600

PAUSE_HOST_PREFIX = "PAUSE-HOST-"
AUTO_PREFIX = "auto:"
COOLDOWN_UNTIL_RE = re.compile(r"^auto:cooldown:until=(\S+)", re.MULTILINE)

# The live state dir this contract must never touch.
REAL_PAUSE_DIR = Path.home() / "phanes-ctl" / "autodispatch"


def cooldown_hosts(state_dir: Path) -> set[str]:
    """Hosts held by a pause file: every PAUSE-HOST-<host> file of the state dir.

    The body does not matter here: an automatic cooldown (``auto:`` body) and a
    manual hold both keep the host out of ``hosts_ok`` and land it in
    ``skipped_cooldown``.
    """
    return {
        path.name[len(PAUSE_HOST_PREFIX):]
        for path in state_dir.glob(f"{PAUSE_HOST_PREFIX}*")
        if path.is_file()
    }


def cooldown_deadline(body: str) -> float | None:
    """Deadline of an ``auto:cooldown:until=<deadline>`` pause body, if it has one."""
    match = COOLDOWN_UNTIL_RE.search(body)
    if not match:
        return None
    value = match.group(1)
    try:
        return float(value)
    except ValueError:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()


def summarize(probes: dict[str, str], cooldown: set[str]) -> dict[str, list[str]]:
    """Split the probed hosts into ok / skipped-because-cooling / degraded."""
    healthy = {host for host, status in probes.items() if status == "ok"}
    return {
        "hosts_ok": sorted(healthy - cooldown),
        "skipped_cooldown": sorted(healthy & cooldown),
        "hosts_degraded": sorted(host for host, status in probes.items() if status != "ok"),
    }


def release_expired(state_dir: Path, now: float) -> list[str]:
    """Release the automatic host pauses that expired.

    Mirrors ``clear_auto`` in ``tools/check-health-pause.py``: an ``auto:``
    pause is released once it is older than ``PAUSE_TTL``, and an
    ``auto:cooldown:until=<deadline>`` body must be past its deadline as well.
    Like the mirrored runtime, there is no probe condition here: an expired
    pause is released even while the host still probes degraded.
    """
    released: list[str] = []
    for pause in sorted(state_dir.glob(f"{PAUSE_HOST_PREFIX}*")):
        if not pause.is_file():
            continue
        body = pause.read_text()
        if not body.startswith(AUTO_PREFIX):
            continue  # a manual pause is never cleared
        host = pause.name[len(PAUSE_HOST_PREFIX):]
        deadline = cooldown_deadline(body)
        if deadline is not None and now < deadline:
            continue  # the cooldown deadline has not passed yet
        if now - pause.stat().st_mtime < PAUSE_TTL:
            continue  # the auto pause has not expired yet
        pause.unlink()
        released.append(host)
    return released


class Fixture:
    """Fake autodispatch state dir; every write is asserted to stay inside it."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.state = self.root / "autodispatch"
        self.state.mkdir(parents=True)
        assert not self.state.is_relative_to(REAL_PAUSE_DIR), "fixture must not be the live pause dir"
        assert not REAL_PAUSE_DIR.is_relative_to(self.state), "fixture must not live inside the live pause dir"

    def _guard(self, path: Path) -> Path:
        resolved = path.resolve()
        assert resolved.is_relative_to(self.root), f"refusing to touch {resolved} outside the fixture"
        return resolved

    def write_health(self, hosts: dict[str, str]) -> None:
        """Write the fake health.json holding the probe snapshot."""
        path = self._guard(self.state / "health.json")
        path.write_text(json.dumps({"hosts": hosts}, indent=1) + "\n")

    def write_pause(self, name: str, body: str, mtime: float) -> Path:
        """Write a fake PAUSE-* file with a fixed mtime (the cooldown clock)."""
        path = self._guard(self.state / name)
        path.write_text(body)
        os.utime(path, (mtime, mtime))
        return path

    def pause_exists(self, name: str) -> bool:
        return (self.state / name).exists()

    def tick(self, now: float) -> dict:
        """One health-loop tick: release expired cooldowns, then write the summary."""
        probes = json.loads((self.state / "health.json").read_text())["hosts"]
        released = release_expired(self.state, now)
        cooldown = cooldown_hosts(self.state)
        summary = summarize(probes, cooldown)
        payload = {
            "at": time.strftime("%F %T", time.localtime(now)),
            "hosts": probes,
            "hosts_ok": summary["hosts_ok"],
            "skipped_cooldown": summary["skipped_cooldown"],
            "hosts_degraded": summary["hosts_degraded"],
            "cooldown": sorted(cooldown),
            "released_cooldown": released,
        }
        self._guard(self.state / "health.json").write_text(json.dumps(payload, indent=1) + "\n")
        return payload


def health_of(fixture: Fixture) -> dict:
    """Read back the fake health.json the tick wrote."""
    return json.loads((fixture.state / "health.json").read_text())


def assert_consistent(health: dict) -> None:
    """hosts_ok and skipped_cooldown partition the healthy probes."""
    healthy = {host for host, status in health["hosts"].items() if status == "ok"}
    ok = set(health["hosts_ok"])
    skipped = set(health["skipped_cooldown"])
    assert not ok & skipped, health
    assert ok | skipped == healthy, health
    assert not ok & set(health["cooldown"]), health


def scratch_parent() -> str | None:
    """Parent dir of the fixtures: the checkout-local scratch dir when writable."""
    scratch_root = ROOT / ".openswarm-tmp"
    try:
        scratch_root.mkdir(exist_ok=True)
        probe = scratch_root / ".write-probe"
        probe.write_text("")
        probe.unlink()
    except OSError:
        return None  # fall back to $TMPDIR (or the system temp dir)
    return str(scratch_root)


def main() -> None:
    with tempfile.TemporaryDirectory(dir=scratch_parent()) as raw_dir:
        fixture = Fixture(Path(raw_dir))
        now = 1_800_000_000.0  # fixed clock: the check never depends on wall time

        # 1) a fresh PAUSE-HOST-ib3 cooldown is the only pause: ib3 must not count as ok
        fixture.write_health({"ib1": "ok", "ib2": "ok", "ib3": "ok", "ib4": "degraded"})
        fixture.write_pause("PAUSE-HOST-ib3", "auto: disk 97% used\n", now)
        fixture.write_pause("PAUSE-BACKEND-pi", "auto: quota 429\n", now)
        fixture.tick(now)
        health = health_of(fixture)
        assert health["hosts_ok"] == ["ib1", "ib2"], health
        assert health["skipped_cooldown"] == ["ib3"], health
        assert health["hosts_degraded"] == ["ib4"], health
        assert health["cooldown"] == ["ib3"], health
        assert health["released_cooldown"] == [], health
        assert fixture.pause_exists("PAUSE-HOST-ib3"), "a pause that has not expired must not be cleared"
        assert_consistent(health)
        print("OK: cooling host ib3 is not counted in hosts_ok, it is listed in skipped_cooldown")

        # 2) the cooldown expired -> released, and the host is ok again
        fixture.write_pause("PAUSE-HOST-ib3", "auto: disk 97% used\n", now - PAUSE_TTL - 1)
        fixture.tick(now)
        health = health_of(fixture)
        assert health["released_cooldown"] == ["ib3"], health
        assert not fixture.pause_exists("PAUSE-HOST-ib3"), "an expired cooldown must be released"
        assert health["hosts_ok"] == ["ib1", "ib2", "ib3"], health
        assert health["skipped_cooldown"] == [], health
        assert_consistent(health)
        print("OK: an elapsed cooldown is released and the host returns to hosts_ok")

        # 3) the cooldown expired but ib3 still probes degraded -> the expired pause is
        #    released (the mirrored clear_auto has no probe condition) and ib3 stays out
        #    of hosts_ok because it probes degraded, not because of a cooldown
        fixture.write_health({"ib1": "ok", "ib2": "ok", "ib3": "degraded"})
        fixture.write_pause("PAUSE-HOST-ib3", "auto: disk 97% used\n", now - PAUSE_TTL - 1)
        fixture.tick(now)
        health = health_of(fixture)
        assert health["released_cooldown"] == ["ib3"], health
        assert not fixture.pause_exists("PAUSE-HOST-ib3"), "an expired cooldown must be released"
        assert "ib3" not in health["hosts_ok"], health
        assert health["skipped_cooldown"] == [], health
        assert health["hosts_degraded"] == ["ib3"], health
        assert_consistent(health)
        print("OK: an expired cooldown is released even while the host still probes degraded")

        # 4) a manual pause (body not auto:) is never released and holds the host out of
        #    hosts_ok; like an automatic cooldown it lands in skipped_cooldown
        fixture.write_health({"ib1": "ok", "ib2": "ok", "ib3": "ok"})
        fixture.write_pause("PAUSE-HOST-ib3", "operator hold\n", now - PAUSE_TTL - 1)
        fixture.tick(now)
        health = health_of(fixture)
        assert health["released_cooldown"] == [], health
        assert fixture.pause_exists("PAUSE-HOST-ib3"), "a manual pause must never be cleared"
        assert health["hosts_ok"] == ["ib1", "ib2"], health
        assert health["skipped_cooldown"] == ["ib3"], health
        assert_consistent(health)
        print("OK: a manual PAUSE-HOST pause is never cleared and holds the host out of hosts_ok")

        # 5) only PAUSE-HOST-* files are cooldowns; other PAUSE-* kinds are ignored
        fixture.write_health({"ib1": "ok", "ib2": "ok", "ib3": "ok"})
        fixture.write_pause("PAUSE-HOST-ib3", "auto: disk 97% used\n", now)
        fixture.write_pause("PAUSE-M3", "auto: lane hold\n", now)
        fixture.write_pause("PAUSE-BACKEND-pi", "auto: quota 429\n", now)
        fixture.tick(now)
        health = health_of(fixture)
        assert health["cooldown"] == ["ib3"], health
        assert health["hosts_ok"] == ["ib1", "ib2"], health
        assert health["skipped_cooldown"] == ["ib3"], health
        assert_consistent(health)
        print("OK: only PAUSE-HOST-<host> files make a host cooldown")

        # 6) a cooldown deadline in the body outranks the file age: it is not cleared early
        fixture.write_health({"ib1": "ok", "ib2": "ok", "ib3": "ok"})
        fixture.write_pause("PAUSE-HOST-ib3", f"auto:cooldown:until={now + 600}\n", now - PAUSE_TTL - 1)
        fixture.tick(now)
        health = health_of(fixture)
        assert health["released_cooldown"] == [], health
        assert fixture.pause_exists("PAUSE-HOST-ib3"), "a cooldown before its deadline must not be cleared"
        assert health["hosts_ok"] == ["ib1", "ib2"], health
        assert health["skipped_cooldown"] == ["ib3"], health
        assert_consistent(health)
        fixture.tick(now + 601)
        health = health_of(fixture)
        assert health["released_cooldown"] == ["ib3"], health
        assert not fixture.pause_exists("PAUSE-HOST-ib3"), "a cooldown past its deadline must be released"
        assert health["hosts_ok"] == ["ib1", "ib2", "ib3"], health
        assert_consistent(health)
        print("OK: a cooldown body deadline outranks the file age and is never cleared early")

    print("health summary vs PAUSE-HOST cooldown: OK")


if __name__ == "__main__":
    main()
