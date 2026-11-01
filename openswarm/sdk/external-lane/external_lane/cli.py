"""python3 -m external_lane <command> — see README.md. Config: flags or EXTERNAL_LANE_REPO / EXTERNAL_LANE_AGENT /
EXTERNAL_LANE_MAX_INFLIGHT; token: GITHUB_TOKEN or GH_TOKEN (or a logged-in gh CLI)."""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time

from . import contract as C
from . import gate
from .agent import ExternalLane


def repo_from_git(cwd=None):
    """owner/name from the `origin` remote of the current clone, else None."""
    try:
        url = subprocess.run(["git", "remote", "get-url", "origin"], cwd=cwd, capture_output=True, text=True,
                             timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"github\.com[:/]([^/\s]+/[^/\s]+?)(?:\.git)?/?$", url)
    return m.group(1) if m else None


def build_parser():
    ap = argparse.ArgumentParser(prog="external_lane", description=__doc__)
    ap.add_argument("--repo", default=os.environ.get("EXTERNAL_LANE_REPO"), help="owner/name (default: origin of the current clone)")
    ap.add_argument("--agent", default=os.environ.get("EXTERNAL_LANE_AGENT"), help="agent name; label is backend:<agent>")
    ap.add_argument("--max-inflight", type=int, default=int(os.environ.get("EXTERNAL_LANE_MAX_INFLIGHT") or 1))
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("contract", help="print the contract (labels, branch format) as JSON")
    sp = sub.add_parser("check", help="wake gate: rc 0 = a card to take, 1 = nothing to do, 2 = error; read-only, one JSON line")
    sp.add_argument("--max-wakes-per-hour", type=int,
                    default=int(os.environ.get("EXTERNAL_LANE_MAX_WAKES_PER_HOUR") or gate.MAX_WAKES_PER_HOUR))
    sp.add_argument("--fail-cooldown", type=int, metavar="SECONDS",
                    default=int(os.environ.get("EXTERNAL_LANE_FAIL_COOLDOWN_S") or gate.FAIL_COOLDOWN_S))
    sp.add_argument("--state-file", default=os.environ.get("EXTERNAL_LANE_STATE_FILE"))
    sp = sub.add_parser("tick", help="one idempotent pass: resume your claim or claim 1 card; with --work-cmd also do it")
    sp.add_argument("--work-cmd", help="optional: shell command that edits + commits (then verify, push, PR happen here)")
    sp.add_argument("--dir", default="./lane-work", help="clone location for --work-cmd (reused between runs)")
    sp.add_argument("--no-verify", action="store_true")
    sub.add_parser("status", help="claimed (in flight) and claimable issues")
    sub.add_parser("poll", help="claimable issues, best first")
    sp = sub.add_parser("claim", help="claim issue N, or the best claimable one")
    sp.add_argument("number", nargs="?", type=int)
    sp = sub.add_parser("branch", help="print the branch to push for issue N (rework issues reuse the PR branch)")
    sp.add_argument("number", type=int)
    sp = sub.add_parser("submit", help="after pushing BRANCH: open the PR (Closes #N) and relabel the issue lane-pr")
    sp.add_argument("number", type=int)
    sp.add_argument("--branch", required=True)
    sp.add_argument("--title", required=True)
    sp.add_argument("--summary", default="")
    sp.add_argument("--dir", help="local clone; enables the >=30-line thin-diff gate before opening the PR")
    sp = sub.add_parser("fail", help="label issue N lane-failed and comment the reason")
    sp.add_argument("number", type=int)
    sp.add_argument("--reason", required=True)
    sp = sub.add_parser("run", help="claim -> run --work-cmd in a branch -> verify -> push -> PR")
    sp.add_argument("--work-cmd", required=True, help="shell command that edits + commits; gets ISSUE_NUMBER, ISSUE_BODY_FILE, LANE_BRANCH")
    sp.add_argument("--dir", default="./lane-work", help="clone location (reused between runs)")
    sp.add_argument("--loop", action="store_true", help="keep polling")
    sp.add_argument("--interval", type=int, default=300, help="seconds between polls with --loop (min 60)")
    sp.add_argument("--no-verify", action="store_true", help="skip the issue's 验收/Verify command")
    return ap


def contract_info(agent=None):
    out = {"version": C.CONTRACT_VERSION, "ready": C.READY, "claimed": C.CLAIMED, "pr_open": C.PR_OPEN,
           "failed": C.FAILED, "min_effective_lines": C.MIN_EFFECTIVE_LINES}
    if agent:
        out.update(label=C.backend_label(agent), claim_comment=C.claim_comment(agent),
                   branch=f"{C.branch_prefix(agent)}issue<N>-<YYYYmmdd-HHMMSS>")
    return out


def brief(i):
    return {"number": i["number"], "title": i.get("title"), "labels": sorted(C.label_names(i))}


def work_brief(lane, issue):
    n = issue["number"]
    branch, rework_pr = lane.branch_for(issue)
    steps = ([f"git fetch origin {branch} && git checkout {branch}  # rework: push to this branch, no new PR"] if rework_pr else
             [f"git fetch origin main && git checkout -b {branch} origin/main"])
    steps += [f"do the work in the issue body; run its acceptance command: {C.verify_command(issue.get('body') or '') or '(none given)'}",
              "commit (>= 30 effective lines of code/tests), then git push origin " + branch,
              f"python3 -m external_lane submit {n} --branch {branch} --title '<type>: <what> (#{n})' --dir .",
              f"or if you cannot finish: python3 -m external_lane fail {n} --reason '<why>'"]
    return {"issue": n, "title": issue.get("title"), "url": issue.get("html_url"), "branch": branch,
            "rework_pr": rework_pr, "body": issue.get("body") or "", "next_steps": steps}


def tick(lane, a):
    """One pass, safe to repeat: at most 1 new claim; an unfinished claim is resumed instead of claiming another."""
    if a.work_cmd:
        url = lane.run_once(os.path.abspath(a.dir), a.work_cmd, run_verify=not a.no_verify)
        print(json.dumps({"pr": url}) if url else json.dumps({"pr": None, "note": "nothing claimed, or failed (see issue comment)"}))
        return 0 if url else 1
    held = lane.inflight()
    issue = held[0] if held else lane.claim_next()
    if not issue:
        print(json.dumps({"issue": None, "note": "nothing to take; end this round"}))
        return 1
    print(json.dumps(dict(work_brief(lane, lane.gh.get_issue(issue["number"])), resumed=bool(held)), ensure_ascii=False, indent=1))
    return 0


def main(argv=None):
    a = build_parser().parse_args(argv)
    if a.cmd == "contract":
        print(json.dumps(contract_info(a.agent), ensure_ascii=False, indent=1))
        return 0
    a.repo = a.repo or repo_from_git()
    if not a.repo or not a.agent:
        msg = "need --agent and --repo (or EXTERNAL_LANE_AGENT / EXTERNAL_LANE_REPO, or run inside a clone)"
        if a.cmd == "check":
            print(json.dumps({"rc": gate.ERROR, "reason": "error", "error": msg}))
        else:
            print(msg, file=sys.stderr)
        return 2
    try:
        lane = ExternalLane(a.repo, a.agent, a.max_inflight, log=lambda m: print(m, file=sys.stderr))
    except ValueError as error:
        print(json.dumps({"rc": gate.ERROR, "reason": "error", "error": str(error)}) if a.cmd == "check" else error,
              file=sys.stdout if a.cmd == "check" else sys.stderr)
        return 2
    if a.cmd == "check":
        rc, summary = gate.check(lane, a.state_file, max_wakes_per_hour=a.max_wakes_per_hour, fail_cooldown_s=a.fail_cooldown)
        print(json.dumps(summary, ensure_ascii=False, separators=(",", ":")))
        return rc
    if a.cmd == "tick":
        return tick(lane, a)
    if a.cmd == "status":
        print(json.dumps({"inflight": [brief(i) for i in lane.inflight()], "claimable": [brief(i) for i in lane.poll()],
                          "max_inflight": lane.max_inflight}, ensure_ascii=False, indent=1))
    elif a.cmd == "poll":
        print(json.dumps([brief(i) for i in lane.poll()], ensure_ascii=False, indent=1))
    elif a.cmd == "claim":
        got = lane.claim(a.number) if a.number else lane.claim_next()
        if not got:
            print("nothing claimed (not claimable, or max_inflight reached)", file=sys.stderr)
            return 1
        print(json.dumps(brief(got), ensure_ascii=False))
    elif a.cmd == "branch":
        print(lane.branch_for(lane.gh.get_issue(a.number))[0])
    elif a.cmd == "submit":
        issue = lane.gh.get_issue(a.number)
        _, rework_pr = lane.branch_for(issue)
        files = lane.numstat(a.dir) if a.dir else None
        url = lane.open_pr(a.number, a.branch, a.title, a.summary, files=files, rework_pr=rework_pr)
        if not url:
            return 1
        print(url)
    elif a.cmd == "fail":
        lane.fail(a.number, a.reason)
    elif a.cmd == "run":
        while True:
            url = lane.run_once(os.path.abspath(a.dir), a.work_cmd, run_verify=not a.no_verify)
            if url:
                print(url)
            if not a.loop:
                break
            time.sleep(max(60, a.interval))
    return 0


if __name__ == "__main__":
    sys.exit(main())
