#!/usr/bin/env python3
"""Offline contract check for the lane-merge idle heartbeat (Refs #955).

Contract under test (production code only, `bin/merge-loop.sh`):

- While the queue holds no PR waiting to merge (no PR pending), the loop skips
  the merge step: no `gh pr merge` call may be issued.
- Every tick still refreshes `merge-heartbeat`, so an idle period is never
  mistaken for a stalled merge loop (false-positive stall detection).

The check replays the real `bin/merge-loop.sh` inside a fake directory with a
fake `gh` stub and never touches the live environment.  Merge policy (green /
mergeability) is deliberately not modelled here: `bin/merge-loop.sh` delegates
the merge decision to the host's `gh` configuration and branch protections.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import time


ROOT = Path(__file__).resolve().parents[1]
MERGE_LOOP = ROOT / "bin" / "merge-loop.sh"
SCRATCH_ROOT = ROOT / ".openswarm-tmp"
DOC = ROOT / "docs" / "ctl-boot.md"
REQUIRED_DOC_TERMS = ("merge-heartbeat", "no PR", "skip", "idle")


def assert_docs_document_contract() -> None:
    """Assert docs/ctl-boot.md states the idle heartbeat contract (Refs #955)."""
    if not DOC.is_file():
        raise AssertionError(f"Documentation file not found: {DOC}")
    text = DOC.read_text(encoding="utf-8")
    for term in REQUIRED_DOC_TERMS:
        if term not in text:
            raise AssertionError(f"docs/ctl-boot.md missing required term: {term!r}")


def assert_fake_merge_loop_script() -> None:
    """Replay the real bin/merge-loop.sh with a fake `gh` stub (Refs #955).

    With an empty PR queue (no PR pending) the loop must skip the merge step
    (no `gh pr merge` call) but must still refresh the merge-heartbeat file,
    otherwise an idle period looks like a wedged merge loop.
    """
    if not MERGE_LOOP.is_file():
        raise AssertionError(f"merge loop script not found: {MERGE_LOOP}")

    SCRATCH_ROOT.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=SCRATCH_ROOT) as raw_fixture:
        fixture = Path(raw_fixture)
        fake_bin = fixture / "bin"
        fake_bin.mkdir()
        calls = fixture / "gh-calls.log"
        gh = fake_bin / "gh"
        gh.write_text(
            "#!/usr/bin/env bash\n"
            'printf "%s\\n" "$*" >> "$GH_CALLS"\n'
            'if [[ "$1" == "pr" && "$2" == "list" ]]; then printf "[]\\n"; fi\n',
            encoding="utf-8",
        )
        gh.chmod(0o755)

        heartbeat = fixture / "autodispatch" / "merge-heartbeat"
        heartbeat.parent.mkdir(parents=True)
        heartbeat.touch()
        time.sleep(0.05)
        old_mtime = heartbeat.stat().st_mtime_ns

        env = {
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "GH_CALLS": str(calls),
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
        assert heartbeat.is_file(), "idle tick did not keep merge-heartbeat"
        assert heartbeat.stat().st_mtime_ns > old_mtime, "idle tick did not refresh merge-heartbeat"

        recorded = calls.read_text(encoding="utf-8") if calls.is_file() else ""
        assert "pr list" in recorded, f"fake gh stub was never called: {recorded!r}"
        assert "pr merge" not in recorded, f"idle tick must not merge anything: {recorded!r}"


def main() -> None:
    assert_docs_document_contract()
    assert_fake_merge_loop_script()
    print("OK: merge-loop idle heartbeat contract passed (fake directory replay verified)")


if __name__ == "__main__":
    main()
