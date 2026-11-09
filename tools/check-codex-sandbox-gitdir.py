#!/usr/bin/env python3
"""Read-only lane inventory and host sandbox acceptance for issue #1832.

Failure ledger (retained from the issue attachment; not locally verified):
primary affected host run 20261103-161536-bd638c reported cherry-pick -n failing with
Read-only file system at .git/worktrees/wt1. CX5 used wt; CX10 used wt1.
The proposed sandbox whitelist cause remains a hypothesis for that host.
Later CX2 run 20261103-215947-68f2a0 had no actual permission error in events;
the issue comments attribute that run to an inaccessible task. No commit alone
is not failure evidence when the controller harvests diff.patch.

Source PR findings (historical): lane_begin_turn sends a policy object, resolved gitdir and
common Git objects/refs directory, with network disabled unless configured.
Policy tests passed; real sandbox execution in that environment was blocked: Codex 0.166.0 emits
"Codex's Linux sandbox uses bubblewrap and needs access to create user namespaces."
No primary affected host logs, mounts, tester commits, CX10 recovery or other-host verification
have been obtained. Do not resume CX10 based on this inventory.

Delivery mapping (line anchors refer to this delivery):
B1 -> swrm/bin/swrm:7330; swrm/tests/test_lane.py:84 ->
  CodexSandboxTests.test_real_codex_sandbox_two_lanes_commit -> namespace-blocked in source PR environment; host required;
  two real command/exec sandboxes commit tracked files; negative write control.
B2 -> tools/check-codex-sandbox-gitdir.py:59; swrm/tests/test_lane.py:127 ->
  CodexSandboxTests.test_host_checker_uses_actual_lane_paths -> PASS locally;
  --sandbox-test runs B1 on the host; inventory alone is not mount/commit proof.
B3 -> tools/check-codex-sandbox-gitdir.py:4 -> manual_failure_ledger_review ->
  PASS: attachment's findings/uncertainties retained; host evidence FOLLOW-UP.
B4 -> tools/check-codex-sandbox-gitdir.py:19 -> manual_delivery_mapping_review ->
  PASS: all four mappings present (manual checks, not automated test claims).
Configuration review -> swrm/bin/swrm:7330 ->
  CodexSandboxTests.test_policy_modes_and_network_restrictions -> PASS (18 cases).
Original 06e4cb428 regression remains -> swrm/tests/test_lane.py:479 ->
  LaneTests.test_codex_two_lanes_allow_writes_to_each_worktree_gitdir -> PASS.

Migration: openswarm/bin/openswarm -> swrm/bin/swrm; both old sandbox/gitdir
test modules -> swrm/tests/test_lane.py. No openswarm files are changed.

Host commands (run with scratch space in the checkout):
  python3 tools/check-codex-sandbox-gitdir.py
  python3 tools/check-codex-sandbox-gitdir.py --sandbox-test
  python3 -m pytest -q swrm/tests/test_lane.py
  python3 -m pytest -q swrm/tests
On primary affected host separately inspect the original result.json and sandbox mount logs,
record tester commits for both lanes in the failure ledger, then verify CX10
three consecutive real tasks. Repeat inventory/verification on secondary affected host CX2/CX4
and another affected host CX9 (issue comments also mention an additional affected host CX11).
"""

import argparse
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sandbox-test", action="store_true",
                        help="run real Codex dual-worktree commit regression in disposable worktrees")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if args.sandbox_test:
        scratch = root / ".swrm-tmp" / "issue-1832"
        scratch.mkdir(parents=True, exist_ok=True)
        return subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "swrm/tests/test_lane.py",
             "-k", "test_real_codex_sandbox_two_lanes_commit"], cwd=root,
            env=dict(os.environ, TMPDIR=str(scratch))).returncode
    loader = importlib.machinery.SourceFileLoader("swrm_gitdir_check", str(root / "swrm/bin/swrm"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    swrm = importlib.util.module_from_spec(spec)
    loader.exec_module(swrm)
    results = []
    for lane in swrm.lane_config():
        if lane["backend"] != "codex":
            continue
        # This is the same path as lane_prepare_workspace/lane supervisor.
        wt = Path(swrm.lane_root(lane["name"])) / "wt"
        record = {"lane": lane["name"], "worktree": str(wt), "verified_commits": False}
        try:
            if not swrm.lane_worktree_matches(lane["repo"], str(wt)):
                raise RuntimeError("worktree missing or does not match configured repository")
            record["sandboxPolicy"] = swrm.lane_codex_sandbox_policy(str(wt))
        except (RuntimeError, ValueError) as error:
            record["error"] = str(error)
        results.append(record)
    print(json.dumps({"inventory_only": True, "lanes": results}, indent=2))
    return int(any("error" in record for record in results))


if __name__ == "__main__":
    sys.exit(main())
