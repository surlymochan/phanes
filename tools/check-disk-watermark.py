#!/usr/bin/env python3
"""Offline disk watermark hysteresis and allocated-size ranking checks."""
from __future__ import annotations

import tempfile
import subprocess
from unittest.mock import patch
from pathlib import Path

from importlib.machinery import SourceFileLoader

ROOT = Path(__file__).resolve().parent
guard = SourceFileLoader("disk_watermark", str(ROOT / "disk-watermark-guard.py")).load_module()
probe = SourceFileLoader("disk_probe", str(ROOT / "disk-usage-probe.py")).load_module()


def main() -> None:
    with patch.object(guard.subprocess, "run") as run:
        run.return_value.stdout = "Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/sda 1000 950 50 95% /\n"
        assert guard.measure(Path("/")) == (95, 51200)
        assert run.call_args.args[0][-2] == "govpn2"
        run.side_effect = subprocess.CalledProcessError(1, "ssh")
        try:
            guard.measure(Path("/"))
            assert False, "failed SSH must not appear healthy"
        except subprocess.CalledProcessError:
            pass
    with tempfile.TemporaryDirectory(dir=ROOT) as temp:
        base = Path(temp)
        state, log = base / "pause", base / "brain.log"
        assert guard.apply(state, log, 94, 100) == "block"
        owned = state / "PAUSE-HOST-govpn2"
        assert owned.read_text().startswith(guard.OWNER)
        # Another automatic health action may replace or clear our pause; the
        # independent hysteresis state must recreate it throughout the hold.
        owned.unlink()
        assert guard.apply(state, log, 91, 200) == "warn"
        assert owned.read_text().startswith(guard.OWNER)
        owned.write_text("auto:other-guard\n")
        assert guard.apply(state, log, 90, 300) == "warn"
        assert owned.read_text() == "auto:other-guard\n"
        owned.unlink()
        assert guard.apply(state, log, 87, 400) == "recovered"
        assert not owned.exists() and not (state / ".disk-watermark-blocked").exists()
        lines = log.read_text().splitlines()
        assert len(lines) == 3 and "disk-watermark block" in lines[0]

        sparse, allocated = base / "sparse", base / "allocated"
        sparse.mkdir(); allocated.mkdir()
        with (sparse / "huge").open("wb") as f:
            f.truncate(64 * 1024 * 1024)
        with (allocated / "small").open("wb") as f:
            import os
            os.posix_fallocate(f.fileno(), 0, 8 * 1024 * 1024)
        sizes = dict((path.name, size) for size, path in probe.top_directories(base))
        assert sizes["allocated"] > sizes["sparse"], sizes
    print("OK: block/warn/recovered retains gate, preserves other owner, logs alerts")
    print("OK: top directories sort by allocated blocks (sparse file fixture)")


if __name__ == "__main__":
    main()
