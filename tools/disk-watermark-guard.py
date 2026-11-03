#!/usr/bin/env python3
"""Guard govpn2 dispatch against a full filesystem (Refs #1666)."""
from __future__ import annotations

import argparse
import subprocess
import shlex
import time
from pathlib import Path

BLOCK_PCT = 93
WARN_PCT = 90
RECOVER_PCT = 88
OWNER = "auto:disk-watermark"


def apply(pause_dir: Path, brain_log: Path, used_pct: int, free_bytes: int) -> str:
    pause = pause_dir / "PAUSE-HOST-govpn2"
    state = pause_dir / ".disk-watermark-blocked"
    pause_dir.mkdir(parents=True, exist_ok=True)
    brain_log.parent.mkdir(parents=True, exist_ok=True)
    prior = pause.read_text() if pause.is_file() else ""
    active = prior.startswith(OWNER)
    was_blocked = state.exists()
    blocked = used_pct >= BLOCK_PCT or (was_blocked and used_pct > RECOVER_PCT)
    if blocked:
        state.touch()
        if not pause.exists() or active:
            pause.write_text(f"{OWNER} used_pct={used_pct} free_bytes={free_bytes}\n")
        level = "block" if used_pct >= BLOCK_PCT else "warn"
        with brain_log.open("a") as log:
            log.write(f"disk-watermark {level} govpn2 used_pct={used_pct} free_bytes={free_bytes}\n")
        return level
    if active:
        pause.unlink()
    state.unlink(missing_ok=True)
    return "recovered"


def measure(path: Path) -> tuple[int, int]:
    command = "df -Pk -- " + shlex.quote(str(path))
    result = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20",
                             "govpn2", command], capture_output=True, text=True,
                            timeout=30, check=True)
    fields = result.stdout.strip().splitlines()[-1].split()
    if len(fields) < 6 or not fields[4].endswith("%"):
        raise ValueError("invalid remote df output")
    pct, free = int(fields[4][:-1]), int(fields[3]) * 1024
    if not 0 <= pct <= 100 or free < 0:
        raise ValueError("invalid remote disk usage")
    return pct, free


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", type=Path, default=Path("/"))
    parser.add_argument("--pause-dir", type=Path, required=True)
    parser.add_argument("--brain-log", type=Path, required=True)
    parser.add_argument("--interval", type=int, default=300)
    parser.add_argument("--loop", action="store_true")
    args = parser.parse_args()
    while True:
        try:
            pct, free = measure(args.path)
        except (subprocess.SubprocessError, OSError, ValueError, IndexError) as exc:
            args.brain_log.parent.mkdir(parents=True, exist_ok=True)
            with args.brain_log.open("a") as log:
                log.write(f"disk-watermark probe-error govpn2 {exc}\n")
            pct, free = 100, 0  # Fail closed until a successful recovery measurement.
        print(f"disk-watermark {apply(args.pause_dir, args.brain_log, pct, free)} used_pct={pct} free_bytes={free}", flush=True)
        if not args.loop:
            break
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
