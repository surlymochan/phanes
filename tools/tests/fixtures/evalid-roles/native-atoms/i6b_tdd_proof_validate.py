"""Offline atom fixture: independently rerun the captured snapshot command."""
import argparse
import json
import os
from pathlib import Path
import subprocess


p = argparse.ArgumentParser()
for name in ("project-root", "change-id", "phase", "tdd-mode"):
    p.add_argument("--" + name, required=True)
a = p.parse_args()
out = Path(a.project_root) / "specs" / "changes" / a.change_id / "tdd"
proof = json.loads((out / (a.phase + "-0.json")).read_text())
result = subprocess.run(proof["command"], shell=True,
    cwd=out / "snapshots" / (a.phase + "-0"), capture_output=True,
    env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
print(json.dumps({"ok": result.returncode == proof["exit"]}))
