#!/usr/bin/env python3
"""Offline/static contract for merge-loop's idle heartbeat (Refs #1031)."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[1]
MERGE_LOOP = ROOT / "bin" / "merge-loop.sh"


def main() -> None:
    source = MERGE_LOOP.read_text(encoding="utf-8")
    for marker in ("merge-heartbeat", "write_heartbeat", "MERGE_LOOP_ONCE", '== \"[]\"'):
        assert marker in source, f"merge-loop missing idle heartbeat marker: {marker}"

    with tempfile.TemporaryDirectory(prefix="merge-hb-idle-") as raw_dir:
        fixture = Path(raw_dir)
        fake_bin = fixture / "bin"
        fake_bin.mkdir()
        gh = fake_bin / "gh"
        gh.write_text("#!/usr/bin/env bash\nprintf '%s\\n' '[]'\n", encoding="utf-8")
        gh.chmod(0o755)
        heartbeat = fixture / "autodispatch" / "merge-heartbeat"
        env = {
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "MERGE_HEARTBEAT": str(heartbeat),
            "MERGE_LOOP_ONCE": "1",
        }
        result = subprocess.run(
            ["bash", str(MERGE_LOOP)],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert heartbeat.is_file(), "idle tick did not update merge-heartbeat"

    print("merge heartbeat idle contract: OK")


if __name__ == "__main__":
    main()
