"""Read-only fleet lane availability report (Refs #1503)."""
import argparse
import datetime
import json
import os
import re
import subprocess
import sys

DEFAULTS = {
    "repo": "", "labels": {"ready": "lane-ready", "in_progress": "in-progress", "pr": "lane-pr",
        "failed": "lane-failed", "strict_backend": "strict-backend", "backend_prefix": "backend:"},
    "skip_labels": [], "fallback": {"pi": ["codex", "copilot", "agy"], "codex": ["pi", "copilot", "agy"],
        "copilot": ["codex", "pi", "agy"], "agy": ["pi", "codex", "copilot"]},
    "never_take_re": {"codex": r"unix\s*socket|AF_UNIX|socketpair|needs-socket|Unix 套接字|套接字"},
    "depends_line_re": r"^\s*(?:依赖|Depends on)\s*[:：](.*)$",
    "hot_files": {"line_re": r"^\s*(?:热文件|Hot-files)\s*[:：](.*)$", "union": [], "limits": {}, "default_limit": 1},
}


def _merge(base, override):
    out = dict(base)
    for key, value in (override or {}).items():
        out[key] = _merge(out[key], value) if isinstance(out.get(key), dict) and isinstance(value, dict) else value
    return out


def _lanes(raw):
    if isinstance(raw, dict):
        if isinstance(raw.get("lanes"), list):
            return _lanes(raw["lanes"])
        return [(name, spec if isinstance(spec, dict) else {}) for name, spec in raw.items()]
    if isinstance(raw, list):
        return [(x.get("name"), x) if isinstance(x, dict) else (x, {})
                for x in raw if isinstance(x, (dict, str))]
    return []


def can_take(backend, item, config=None):
    cfg = _merge(DEFAULTS, config or {})
    if item.get("backend") in (None, backend):
        return True
    if item.get("strict"):
        return False
    pattern = cfg.get("never_take_re", {}).get(backend)
    return not (pattern and re.search(pattern, (item.get("title", "") + "\n" + item.get("body", "")), re.I))


def _is_busy(name, status, state):
    value = status.get(name, "")
    if re.search(r"\bbusy\b", str(value), re.I):
        return True
    entry = state.get(name, {})
    return isinstance(entry, dict) and bool(entry.get("issue"))


def _active_pauses(pauses):
    now = datetime.datetime.now(datetime.timezone.utc).timestamp()
    active = {}
    for key, val in (pauses or {}).items():
        scope = val.get("scope", key) if isinstance(val, dict) else key
        reason = val.get("reason", "") if isinstance(val, dict) else ""
        expiry = val.get("expires_at") if isinstance(val, dict) else None
        try:
            if expiry is not None and float(expiry) <= now:
                continue
        except (TypeError, ValueError):
            continue
        active[scope] = reason
    return active


def summarize(lanes_cfg, lanes_status, state, pauses, queue, config=None):
    """Summarize lanes. queue=None means unavailable; deps_ok/hot must be supplied by caller."""
    cfg = _merge(DEFAULTS, config or {})
    rows, busy_counts, idle_counts = _lanes(lanes_cfg), {}, {}
    paused = _active_pauses(pauses)
    internal = set(cfg["fallback"]) | {"pi", "codex", "agy", "copilot"}
    candidates = [x for x in (queue or []) if not x.get("external")
                  and (not x.get("backend") or x["backend"] in internal)]
    held = {}
    for name, spec in rows:
        if _is_busy(name, lanes_status or {}, state or {}):
            entry = (state or {}).get(name, {})
            if isinstance(entry, dict):
                for filename in entry.get("hot", []): held[filename] = held.get(filename, 0) + 1
    idle = []
    for name, spec in rows:
        backend = spec.get("backend", "unknown")
        busy_counts.setdefault(backend, 0); idle_counts.setdefault(backend, 0)
        if _is_busy(name, lanes_status or {}, state or {}):
            busy_counts[backend] += 1
            continue
        idle_counts[backend] += 1
        host = spec.get("host", "")
        reason = None
        if "all" in paused: reason = "paused:PAUSE"
        elif "lane:" + str(name) in paused: reason = "paused:PAUSE-" + str(name)
        elif "host:" + str(host) in paused: reason = "paused:PAUSE-HOST-" + str(host)
        elif "backend:" + str(backend) in paused: reason = "paused:PAUSE-BACKEND-" + str(backend)
        elif spec.get("drain") or "drain" in str((lanes_status or {}).get(name, "")).lower(): reason = "drain"
        elif any(x in str((lanes_status or {}).get(name, "")).lower() for x in ("timeout", "failed", "error", "host in backoff", "missing in batch", "down")): reason = "host_fail"
        elif str((lanes_status or {}).get(name, "")).strip().lower() in ("?", "unknown"): reason = "unknown"
        elif queue is None: reason = "unknown"
        else:
            eligible = [item for item in candidates if can_take(backend, item, cfg)]
            if eligible and all(item.get("deps_ok") is False for item in eligible): reason = "deps"
            else:
                hot_cfg = cfg["hot_files"]
                limits = hot_cfg.get("limits", {})
                ready = [item for item in eligible if item.get("deps_ok", True)]
                conflicts = [next((f for f in item.get("hot", [])
                                   if f not in set(hot_cfg.get("union", [])) and
                                   held.get(f, 0) >= int(limits.get(f, hot_cfg.get("default_limit", 1)))), None)
                             for item in ready]
                blocked = conflicts[0] if conflicts and all(conflicts) else None
                reason = "hot_blocked:" + blocked if blocked else ("ready" if ready else ("no_card" if not eligible else "unknown"))
        idle.append({"lane": name, "backend": backend, "reason": reason})
    return {"backends": {b: {"busy": busy_counts[b], "idle": idle_counts[b]} for b in busy_counts}, "busy": sum(busy_counts.values()), "idle": idle, "queue_known": queue is not None}


def _read(path, default):
    try:
        with open(path, encoding="utf-8") as stream: return json.load(stream)
    except (OSError, ValueError): return default


def _queue(config):
    labels = config["labels"]
    args = ["gh", "issue", "list", "--state", "open", "--label", labels["ready"], "--limit", "100", "--json", "number,title,labels,body"]
    if config.get("repo"): args[2:2] = ["-R", config["repo"]]
    try:
        proc = subprocess.run(args, capture_output=True, text=True, check=False)
        if proc.returncode: return None
        result = []
        for raw in json.loads(proc.stdout):
            names = {x["name"] for x in raw.get("labels", [])}
            if labels["in_progress"] in names or names & set(config.get("skip_labels", [])): continue
            pinned = next((x[len(labels["backend_prefix"]):] for x in names if x.startswith(labels["backend_prefix"])), None)
            if pinned and pinned not in config["fallback"] and pinned not in ("pi", "codex", "agy", "copilot"): continue
            strict = labels["strict_backend"] in names
            body = raw.get("body") or ""
            deps_match = re.search(config["depends_line_re"], body, re.M)
            deps_ok = True
            if deps_match:
                for dependency in re.findall(r"#(\d+)", deps_match.group(1)):
                    view = ["gh", "issue", "view", dependency, "--json", "state"]
                    if config.get("repo"): view[2:2] = ["-R", config["repo"]]
                    try:
                        dep = subprocess.run(view, capture_output=True, text=True, check=False)
                        if dep.returncode or json.loads(dep.stdout).get("state") != "CLOSED": deps_ok = False
                    except (OSError, ValueError): deps_ok = False
            hot_match = re.search(config["hot_files"].get("line_re", DEFAULTS["hot_files"]["line_re"]), body, re.M)
            hot = re.findall(r"[^\s,，]+", hot_match.group(1)) if hot_match else []
            result.append({"n": raw.get("number"), "title": raw.get("title", ""), "body": body, "backend": pinned,
                "strict": strict, "deps_ok": deps_ok, "hot": hot})
        return result
    except (OSError, ValueError, TypeError):
        return None


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", required=True); parser.add_argument("--lanes", required=True)
    parser.add_argument("--config"); parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    cfg = _merge(DEFAULTS, _read(args.config or os.path.join(args.state_dir, "dispatch.json"), {}))
    lanes = _read(args.lanes, {})
    status = _read(os.path.join(args.state_dir, "lanes-status.json"), {})
    state = _read(os.path.join(args.state_dir, "state.json"), {})
    pauses = {}
    for marker in os.listdir(args.state_dir) if os.path.isdir(args.state_dir) else []:
        if marker == "PAUSE": pauses["all"] = marker
        elif marker.startswith("PAUSE-"):
            suffix = marker[6:]
            scope = "backend:" + suffix[len("BACKEND-"):] if suffix.startswith("BACKEND-") else ("host:" + suffix[5:] if suffix.startswith("HOST-") else "lane:" + suffix)
            pauses[scope] = marker
    now = datetime.datetime.now(datetime.timezone.utc).timestamp()
    for path in __import__("glob").glob(os.path.join(args.state_dir, "pauses", "*.json")):
        record = _read(path, {})
        scope = record.get("scope")
        expiry = record.get("expires_at")
        try:
            if scope and (expiry is None or float(expiry) > now): pauses[scope] = record
        except (TypeError, ValueError): pass
    report = summarize(lanes, status, state, pauses, _queue(cfg), cfg)
    if args.json: print(json.dumps(report, ensure_ascii=False, sort_keys=True)); return 0
    backends = ", ".join("%s %d/%d" % (b, v["busy"], v["busy"] + v["idle"]) for b, v in report["backends"].items())
    print("busy %d: %s" % (report["busy"], backends))
    if not report["queue_known"]: print("queue: unknown")
    for item in report["idle"]: print("%s: %s" % (item["lane"], item["reason"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
