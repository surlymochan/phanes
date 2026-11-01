"""Read-only lane capacity and idle-reason report."""
import argparse
import json
import os
import subprocess
import sys

DEFAULT_FALLBACK = {"pi": ["codex", "copilot"], "codex": ["pi", "copilot"],
                    "copilot": ["codex", "pi"], "agy": ["pi", "codex", "copilot"]}


def _lanes(cfg):
    return cfg.get("lanes", cfg) if isinstance(cfg, dict) else cfg


def can_take(backend, card, fallback=None):
    pin = card.get("backend")
    if pin and pin not in ("pi", "codex", "agy", "copilot"):
        return False
    if backend == "agy":
        return True
    if not pin or pin == backend:
        return True
    if card.get("strict"):
        return False
    return pin in (fallback or DEFAULT_FALLBACK).get(backend, [])


def summarize(lanes_cfg, lanes_status, state, pauses, queue, can_take_fn=None, fallback=None):
    """Return per-backend counts and idle lane reasons. Inputs are already-read snapshots."""
    lanes = _lanes(lanes_cfg)
    can = can_take_fn or (lambda backend, card: can_take(backend, card, fallback))
    pauses = set(pauses or ())
    counts, idle = {}, []
    for lane in lanes:
        spec = lanes[lane] if isinstance(lanes, dict) else lane
        name = spec.get("name") if isinstance(spec, dict) else str(spec)
        raw_backend = spec.get("backend") if isinstance(spec, dict) else None
        backend = raw_backend if raw_backend in ("pi", "codex", "agy", "copilot") else "外部"
        status = (lanes_status or {}).get(name)
        busy = bool((state or {}).get(name)) or (isinstance(status, str) and status.startswith("busy")) or (
            isinstance(status, dict) and status.get("busy"))
        row = counts.setdefault(backend, {"busy": 0, "idle": 0})
        row["busy" if busy else "idle"] += 1
        if busy:
            continue
        reason = None
        pause = next((p for p in pauses if p in ("PAUSE-" + name, "PAUSE-HOST-" + str(spec.get("host", "")),
                                                   "PAUSE-BACKEND-" + str(backend))), None)
        if pause:
            reason = "paused:" + pause
        elif status in ("host_fail", "failed", "error") or isinstance(status, dict) and status.get("error"):
            reason = "host_fail"
        elif isinstance(spec, dict) and spec.get("drain"):
            reason = "drain"
        elif isinstance(status, str) and status.startswith("drain"):
            reason = "drain"
        elif isinstance(status, dict) and status.get("hot_blocked"):
            reason = "hot_blocked:" + str(status["hot_blocked"])
        elif isinstance(status, dict) and status.get("deps"):
            reason = "deps"
        elif backend == "外部" or not any(can(backend, card) for card in (queue or [])):
            reason = "no_card"
        else:
            reason = "unknown"
        idle.append({"lane": name, "backend": backend, "reason": reason})
    return {"backends": counts, "idle": idle}


def _ready_queue(repo=""):
    cmd = ["gh", "issue", "list", "--state", "open", "--label", "lane-ready", "--limit", "100",
           "--json", "number,title,labels,body"]
    if repo:
        cmd += ["--repo", repo]
    result = subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    items = []
    for issue in json.loads(result.stdout):
        labels = [x["name"] for x in issue.get("labels", [])]
        if "in-progress" in labels:
            continue
        pin = next((x[8:] for x in labels if x.startswith("backend:")), None)
        items.append({"backend": pin, "strict": "strict-backend" in labels,
                      "title": issue.get("title", ""), "body": issue.get("body", "")})
    return items


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--lanes", required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        with open(args.lanes) as f:
            cfg = json.load(f)
        state_dir = args.state_dir
        with open(os.path.join(state_dir, "state.json")) as f:
            state = json.load(f)
        with open(os.path.join(state_dir, "lanes-status.json")) as f:
            status = json.load(f)
        pauses = [x for x in os.listdir(state_dir) if x.startswith("PAUSE-")]
        queue = _ready_queue(cfg.get("repo", "") if isinstance(cfg, dict) else "")
        report = summarize(cfg, status, state, pauses, queue)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print("lanes-report: " + str(exc), file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    else:
        total = sum(x["busy"] for x in report["backends"].values())
        detail = ", ".join("%s %d/%d" % (b, x["busy"], x["busy"] + x["idle"])
                            for b, x in report["backends"].items())
        print("busy %d: %s" % (total, detail))
        for lane in report["idle"]:
            print("%s: %s" % (lane["lane"], lane["reason"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
