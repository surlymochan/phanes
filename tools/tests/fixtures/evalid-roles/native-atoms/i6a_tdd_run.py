"""Offline atom fixture: run the command and retain a replayable Git snapshot."""
import argparse
from io import BytesIO
import json
import os
from pathlib import Path
import subprocess
import tarfile


p = argparse.ArgumentParser()
for name in ("project-root", "change-id", "phase", "test-command"):
    p.add_argument("--" + name, required=True)
a = p.parse_args()
root = Path(a.project_root)
result = subprocess.run(a.test_command, shell=True, cwd=root, capture_output=True, text=True,
                        env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
out = root / "specs" / "changes" / a.change_id / "tdd"
snapshot = out / "snapshots" / (a.phase + "-0")
snapshot.mkdir(parents=True)
archive = subprocess.run(["git", "archive", "HEAD"], cwd=root, check=True, capture_output=True)
with tarfile.open(fileobj=BytesIO(archive.stdout)) as bundle:
    bundle.extractall(snapshot)
proof = {"command": a.test_command, "exit": result.returncode,
         "failure_kind": "assertion" if "AssertionError" in result.stderr else None}
(out / (a.phase + "-0.json")).write_text(json.dumps(proof))
print(json.dumps({"ok": result.returncode == (1 if a.phase == "red" else 0)}))
