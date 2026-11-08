#!/usr/bin/env python3
"""Offline contract of the govpn2 disk watermark guard (Refs #1666).

The check drives the real guard with a fake ``ssh`` on ``PATH`` (the fake
answers the remote ``df`` probe), a scratch pause directory and a scratch brain
log: it never connects to the real lane host, it never starts the resident loop
and it never touches the live ``~/phanes-ctl`` state.

Covered contract:

* the probe ranks the directories by their allocated blocks, not by their
  apparent size, and the guard reads the usage numbers of the remote host;
* block -> hold -> recovered: the lane stays paused inside the hysteresis band
  and the guard releases its own marker at the recovery threshold;
* the guard restores its own marker when another sweep (the health sweep) has
  cleared it, and it never takes over or releases a foreign marker;
* a failed probe is never a healthy host: the guard fails closed;
* the dispatcher holds the lanes of a blocked host and opens the gate again
  after the recovery.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path


TOOLS = Path(__file__).resolve().parent
GUARD = TOOLS / "disk-watermark-guard.py"
PROBE = TOOLS / "disk-usage-probe.py"
AUTODISPATCH = TOOLS / "lane-autodispatch" / "autodispatch.py"
HOST = "govpn2"
MARKER = f"PAUSE-HOST-{HOST}"

DF_HEADER = "Filesystem 1024-blocks Used Available Capacity Mounted on\n"
DF_BLOCKED = DF_HEADER + "/dev/sda1 1000000 950000 50000 95% /\n"
DF_HELD = DF_HEADER + "/dev/sda1 1000000 910000 90000 91% /\n"
DF_RECOVERED = DF_HEADER + "/dev/sda1 1000000 870000 130000 87% /\n"


def load(name, path):
    """Import a script of this directory (the file names carry dashes)."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fake_ssh(base, df_text=DF_BLOCKED, failing=False):
    """A fake ``ssh`` that answers the remote df probe of the guard."""
    bin_dir = Path(base) / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    df_file = Path(base) / "df.out"
    df_file.write_text(df_text)
    script = bin_dir / "ssh"
    script.write_text(
        "#!/bin/sh\n"
        f'if [ -f "{Path(base) / "ssh.fail"}" ]; then exit 255; fi\n'
        f'cat "{df_file}"\n'
    )
    script.chmod(0o755)
    if failing:
        (Path(base) / "ssh.fail").write_text("fail\n")
    elif (Path(base) / "ssh.fail").exists():
        (Path(base) / "ssh.fail").unlink()      # a stale failure must not leak into the next probe
    return f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"


def run_guard(base, pause_dir, brain_log, env, host=HOST):
    """Run the guard once against the fake remote host."""
    return subprocess.run(
        [sys.executable, str(GUARD), "--pause-dir", str(pause_dir),
         "--brain-log", str(brain_log), "--path", "/", "--host", host],
        capture_output=True, text=True, env=env, timeout=120)


def brain_rows(path):
    """The non-empty lines of the brain log."""
    path = Path(path)
    if not path.is_file():
        return []
    return [row for row in path.read_text(errors="replace").splitlines() if row.strip()]


def check_probe(base):
    """The probe ranks the directories by their allocated blocks."""
    probe = load("disk_usage_probe_check", PROBE)
    root = Path(base) / "tree"
    sparse, allocated = root / "sparse", root / "allocated"
    sparse.mkdir(parents=True)
    allocated.mkdir(parents=True)
    with (sparse / "hole").open("wb") as handle:
        handle.truncate(64 * 1024 * 1024)          # a sparse file: huge, but empty
    with (allocated / "real").open("wb") as handle:
        handle.write(b"x" * (8 * 1024 * 1024))      # a real file: 8 MiB on the disk
    sizes = {Path(path).name: size for path, size in probe.directory_usage(root).items()}
    assert "sparse" in sizes and "allocated" in sizes, sizes
    assert sizes["allocated"] > sizes["sparse"], (
        f"the ranking must use the allocated blocks, not the apparent size: {sizes}")
    usage = probe.parse_df(DF_BLOCKED, "/")
    assert usage["used_pct"] == 95, usage
    assert usage["used_bytes"] == 950000 * 1024, usage
    assert usage["free_bytes"] == 50000 * 1024, usage
    print("OK: the probe ranks by allocated blocks and parses the usage of the remote host")


def check_hysteresis(base):
    """block -> hold -> recovered, with the own and the foreign marker."""
    pause_dir = Path(base) / "pause"
    brain_log = Path(base) / "brain.log"
    marker = pause_dir / MARKER
    env = dict(os.environ)
    env["PATH"] = fake_ssh(base, DF_BLOCKED)

    done = run_guard(base, pause_dir, brain_log, env)
    assert done.returncode == 0, done.stderr
    assert marker.exists(), f"a 95% watermark must pause {HOST}: {done.stdout}"
    assert marker.read_text().startswith("auto:"), marker.read_text()
    rows = brain_rows(brain_log)
    assert any("disk-watermark" in row and "block" in row for row in rows), rows

    # 91% is inside the hysteresis band: the lane stays paused, nothing is released.
    env["PATH"] = fake_ssh(base, DF_HELD)
    done = run_guard(base, pause_dir, brain_log, env)
    assert done.returncode == 0, done.stderr
    assert marker.exists(), f"the host must stay paused above the recovery threshold: {done.stdout}"
    assert not any("recovered" in row for row in brain_rows(brain_log)), brain_rows(brain_log)

    # the health sweep cleared the marker: the guard restores it while it holds.
    marker.unlink()
    done = run_guard(base, pause_dir, brain_log, env)
    assert done.returncode == 0, done.stderr
    assert marker.exists(), "the guard must restore its own marker while the disk is too full"

    # a marker of another owner is neither taken over nor released.
    marker.write_text("auto:other-guard\n")
    env["PATH"] = fake_ssh(base, DF_BLOCKED)
    run_guard(base, pause_dir, brain_log, env)
    assert marker.read_text() == "auto:other-guard\n", marker.read_text()

    # At the recovery threshold a foreign marker still belongs to its owner.
    env["PATH"] = fake_ssh(base, DF_RECOVERED)
    done = run_guard(base, pause_dir, brain_log, env)
    assert done.returncode == 0, done.stderr
    assert marker.read_text() == "auto:other-guard\n", marker.read_text()

    # Restore our own marker, then check release exactly at 87%.
    marker.unlink()
    env["PATH"] = fake_ssh(base, DF_HELD)
    done = run_guard(base, pause_dir, brain_log, env)
    assert done.returncode == 0, done.stderr
    assert marker.exists(), "the hold must restore our own marker before recovery"
    env["PATH"] = fake_ssh(base, DF_RECOVERED)
    done = run_guard(base, pause_dir, brain_log, env)
    assert done.returncode == 0, done.stderr
    assert not marker.exists(), f"the recovery must release the pause of {HOST}: {done.stdout}"
    print("OK: block/hold/recovered holds the gate and releases only the own marker")


def check_failed_probe(base):
    """A failed probe is never a healthy host: the guard fails closed."""
    pause_dir = Path(base) / "pause-failed"
    brain_log = Path(base) / "brain-failed.log"
    env = dict(os.environ)
    env["PATH"] = fake_ssh(base, DF_BLOCKED, failing=True)
    done = run_guard(base, pause_dir, brain_log, env)
    assert "recovered" not in (done.stdout + done.stderr).lower(), done.stdout
    rows = brain_rows(brain_log)
    assert any("disk-watermark" in row for row in rows), rows
    assert (pause_dir / MARKER).exists() or done.returncode != 0, (
        f"a failed probe must fail closed: {done.stdout} {done.stderr}")
    print("OK: a failed probe fails closed and leaves a brain log row")


def check_dispatch_gate(base):
    """The dispatcher holds the lanes of a host that the guard blocks."""
    home = Path(base) / "home"
    (home / "phanes-ctl" / "autodispatch").mkdir(parents=True, exist_ok=True)
    pause_dir = home / "phanes-ctl" / "autodispatch"
    brain_log = home / "phanes-ctl" / "brain.log"
    env = dict(os.environ)
    env["PATH"] = fake_ssh(base, DF_BLOCKED)
    done = run_guard(base, pause_dir, brain_log, env)
    assert done.returncode == 0, done.stderr

    previous = {name: os.environ.get(name) for name in ("HOME", "OPENSWARM_HOME")}
    os.environ["HOME"] = str(home)
    os.environ["OPENSWARM_HOME"] = str(home / "state")
    try:
        dispatcher = load("autodispatch_disk_watermark_check", AUTODISPATCH)
        assert f"host:{HOST}" in dispatcher.paused_scopes(), dispatcher.paused_scopes()
        assert dispatcher.lane_paused("CX2", {"host": HOST}), \
            "the dispatcher must hold a lane of the blocked host"
        # A stopped guard and a health sweep must not reopen a full host.
        (pause_dir / MARKER).unlink()
        state_file = pause_dir / f"disk-watermark-{HOST}.json"
        record = json.loads(state_file.read_text())
        record["updated_at"] = time.time() - 7 * 24 * 3600
        state_file.write_text(json.dumps(record))
        assert dispatcher.lane_paused("CX2", {"host": HOST}), \
            "an old blocked state must hold the gate until measured recovery"
        assert f"host:{HOST}" in dispatcher.paused_scopes()
        env["PATH"] = fake_ssh(base, DF_RECOVERED)
        done = run_guard(base, pause_dir, brain_log, env)
        assert done.returncode == 0, done.stderr
        dispatcher = load("autodispatch_disk_watermark_recovered_check", AUTODISPATCH)
        assert not dispatcher.lane_paused("CX2", {"host": HOST}), \
            "the dispatcher must open the gate again after the recovery"
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
    print("OK: the dispatcher holds the lanes of the blocked host and opens after the recovery")


def check_deployment():
    """The probe dependency of the guard lives in its own directory."""
    assert PROBE.is_file(), "the installer must deploy the probe next to the guard"
    assert GUARD.is_file(), "the installer must deploy the guard"
    source = GUARD.read_text()
    assert "disk-usage-probe.py" in source, \
        "the guard must load the probe from its own directory"
    print("OK: the guard and its probe dependency are deployed together")


def main():
    with tempfile.TemporaryDirectory(dir=TOOLS) as fixture:
        base = Path(fixture)
        check_probe(base)
        check_hysteresis(base)
        check_failed_probe(base)
        check_dispatch_gate(base)
        check_deployment()


if __name__ == "__main__":
    main()
