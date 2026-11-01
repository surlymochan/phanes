"""Dispatcher side of GitHub-only external lanes (`openswarm external ...`).

An external lane is an agent that can read/write the repo on GitHub but cannot be reached over SSH (a hosted personal
agent, a CI bot, ...). It polls issues itself using the contract in sdk/external-lane/external_lane/contract.py; this
module is the dispatcher's half: keep up to max_inflight cards labelled for it, notice its PRs, gate thin PRs with a
commit status, and put stale claims back. Review and merge stay with the normal lane-review/merge gate.

Config: policy.external_backends.<name> = {repo, max_inflight, claim_timeout_s, max_requeues, auto_assign,
assign_exclude_hot, assign_from_backends, branch_prefixes}. Pure planning (`plan`) is separated from GitHub I/O.
"""
from __future__ import annotations

import argparse
import calendar
import json
import os
import sys
import time
import urllib.parse

_SDK = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "sdk", "external-lane")
if _SDK not in sys.path:
    sys.path.insert(0, _SDK)
from external_lane import contract as C  # noqa: E402  (single source of truth for labels/branches)

STATUS_CONTEXT = "external-lane/contract"
DEFAULTS = {"enabled": True, "repo": None, "max_inflight": 1, "claim_timeout_s": 6 * 3600, "max_requeues": 2,
            "auto_assign": False, "assign_exclude_hot": [], "assign_from_backends": [], "branch_prefixes": [],
            "min_effective_lines": C.MIN_EFFECTIVE_LINES}
PRIO = {"p0": 0, "p1": 1, "p2": 2}


class ExternalError(RuntimeError):
    pass


def backend_configs(policy):
    """Validated {name: cfg} from policy.external_backends (keys starting with '_' are docs)."""
    raw = (policy or {}).get("external_backends") or {}
    if not isinstance(raw, dict):
        raise ExternalError("policy.external_backends must be an object")
    out = {}
    for name, cfg in raw.items():
        if name.startswith("_"):
            continue
        if not isinstance(cfg, dict):
            raise ExternalError(f"policy.external_backends.{name} must be an object")
        c = dict(DEFAULTS, **cfg)
        c["name"] = C.check_agent(name)
        c["label"] = C.backend_label(name)
        if not isinstance(c["repo"], str) or c["repo"].count("/") != 1:
            raise ExternalError(f"policy.external_backends.{name}.repo must be owner/name")
        for key in ("max_inflight", "claim_timeout_s", "max_requeues", "min_effective_lines"):
            if not isinstance(c[key], int) or c[key] < 0:
                raise ExternalError(f"policy.external_backends.{name}.{key} must be a non-negative integer")
        c["branch_prefixes"] = sorted(set(c["branch_prefixes"] or []) | {C.branch_prefix(name)})
        out[c["name"]] = c
    return out


def reserved_labels(cfgs):
    """Labels a fleet dispatcher must never hand to its own lanes: every external backend label + lane-claimed."""
    return sorted({c["label"] for c in cfgs.values() if c["enabled"]} | {C.CLAIMED})


def _ts(value):
    try:
        return calendar.timegm(time.strptime(value, "%Y-%m-%dT%H:%M:%SZ"))
    except (TypeError, ValueError):
        return 0


def _prio(issue):
    return min([PRIO[l] for l in C.label_names(issue) if l in PRIO] or [3]), int(issue["number"])


def assignable(issue, cfg):
    """'' when the ready card may be relabelled for this external backend, else why not."""
    labels = C.label_names(issue)
    body = issue.get("body") or ""
    if "pull_request" in issue:
        return "pull-request"
    if C.READY not in labels or labels & {C.IN_PROGRESS, C.CLAIMED, C.PR_OPEN, C.FAILED, C.NEEDS_HUMAN, "planner"}:
        return "state"
    pins = {l[len(C.BACKEND_PREFIX):] for l in labels if l.startswith(C.BACKEND_PREFIX)}
    if pins - set(cfg["assign_from_backends"]) or "strict-backend" in labels:
        return "pinned"
    if C.hot_files(body) & set(cfg["assign_exclude_hot"]):
        return "hot-file"
    if C.rework_target(body) or C.depends_on(body):
        return "rework/depends"
    if not C.verify_command(body):
        return "no-acceptance"
    return ""


def plan(cfg, own, ready, prs, files_of, memory, now):
    """Decide this tick's actions. own: open issues labelled cfg.label; ready: open lane-ready issues;
    prs: open PRs; files_of(pr_number) -> PR files. Mutates memory (bookkeeping only); returns a list of actions."""
    acts, label = [], cfg["label"]
    own_by_n = {i["number"]: i for i in own}
    mem_pr = memory.setdefault("prs", {})
    linked = {}
    for pr in prs:
        head = (pr.get("head") or {}).get("ref") or ""
        if not any(head.startswith(p) for p in cfg["branch_prefixes"]):
            continue
        num, sha = pr["number"], (pr.get("head") or {}).get("sha")
        rec = mem_pr.setdefault(str(num), {})
        n = C.linked_issue(pr.get("body") or "", head)
        rec["issue"] = n
        if n is None:
            if rec.get("contract_sha") != sha:
                rec["contract_sha"] = sha
                acts.append({"op": "status", "sha": sha, "state": "failure", "pr": num,
                             "desc": "no `Closes #N` in PR body and branch is not fleet/<agent>-issue<N>-<ts>"})
            continue
        linked.setdefault(n, []).append(pr)
        if rec.get("contract_sha") != sha:
            rec["contract_sha"] = sha
            why = C.thin_reason(files_of(num), cfg["min_effective_lines"])
            acts.append({"op": "status", "sha": sha, "pr": num, "state": "failure" if why else "success",
                         "desc": f"thin: {why}" if why else f"contract ok (issue #{n})"})
        issue = own_by_n.get(n)
        if issue:
            labels = C.label_names(issue)
            rw = C.rework_target(issue.get("body") or "")
            if C.CLAIMED in labels and not (rw and rw[0] == num):
                acts.append({"op": "labels", "n": n, "labels": sorted(C.pr_opened_labels(labels)),
                             "log": f"#{n}: PR #{num} found → {C.PR_OPEN}"})
    requeues = memory.setdefault("requeues", {})
    for n, issue in own_by_n.items():
        labels = C.label_names(issue)
        if C.CLAIMED not in labels:
            continue
        last = max([_ts(issue.get("updated_at"))] + [_ts(p.get("updated_at")) for p in linked.get(n, [])])
        if now - last < cfg["claim_timeout_s"]:
            continue
        k = int(requeues.get(str(n)) or 0)
        idle_h = (now - last) / 3600
        if k < cfg["max_requeues"]:
            requeues[str(n)] = k + 1
            acts.append({"op": "labels", "n": n, "labels": sorted(C.requeue_labels(labels)),
                         "comment": f"external lane **{cfg['name']}**: claim idle {idle_h:.1f}h with no PR/comment → back to `{C.READY}` (requeue {k + 1}/{cfg['max_requeues']}).",
                         "log": f"#{n}: stale claim {idle_h:.1f}h → {C.READY} ({k + 1}/{cfg['max_requeues']})"})
        else:
            acts.append({"op": "labels", "n": n, "labels": sorted(C.failed_labels(labels) | {C.NEEDS_HUMAN}),
                         "comment": f"external lane **{cfg['name']}**: claim idle {idle_h:.1f}h again after {k} requeues → `{C.FAILED}` + `{C.NEEDS_HUMAN}` (no more automatic requeue).",
                         "log": f"#{n}: stale claim after {k} requeues → {C.FAILED}"})
    active = [i for i in own if C.label_names(i) & {C.READY, C.CLAIMED} and not C.label_names(i) & {C.PR_OPEN, C.FAILED}]
    free = cfg["max_inflight"] - len(active)
    if cfg["auto_assign"] and free > 0:
        for issue in sorted((i for i in ready if i["number"] not in own_by_n), key=_prio):
            if free <= 0:
                break
            if assignable(issue, cfg):
                continue
            labels = C.label_names(issue)
            was = sorted(l for l in labels if l.startswith(C.BACKEND_PREFIX))
            new = {l for l in labels if not l.startswith(C.BACKEND_PREFIX)} | {cfg["label"]}
            acts.append({"op": "labels", "n": issue["number"], "labels": sorted(new),
                         "comment": f"Assigned to external lane **{cfg['name']}** (was {', '.join(was) or 'unpinned'}). "
                                    f"Fleet lanes skip `{cfg['label']}` cards; the agent claims it with `{C.CLAIMED}`.",
                         "log": f"#{issue['number']}: assigned → {cfg['label']}"})
            free -= 1
    return acts


# ---- GitHub I/O (through the host's gh transport) ---------------------------------------------------
def _issues(host, repo, labels):
    q = urllib.parse.urlencode({"state": "open", "labels": ",".join(labels)})
    return [i for i in host.gh_pages(f"repos/{repo}/issues?{q}") if "pull_request" not in i]


def memory_path(host, name):
    return os.path.join(host.DATA, "external", f"{name}.json")


def load_memory(host, name):
    try:
        with open(memory_path(host, name)) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_memory(host, name, memory):
    path = memory_path(host, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(memory, fh, indent=1, sort_keys=True)
    os.replace(tmp, path)


def apply(host, repo, act):
    if act["op"] == "status":
        host.gh_transport(f"repos/{repo}/statuses/{act['sha']}", "POST",
                          {"state": act["state"], "context": STATUS_CONTEXT, "description": act["desc"][:139]})
    elif act["op"] == "labels":
        host.gh_transport(f"repos/{repo}/issues/{act['n']}/labels", "PUT", {"labels": act["labels"]})
        if act.get("comment"):
            host.gh_transport(f"repos/{repo}/issues/{act['n']}/comments", "POST", {"body": act["comment"]})


def ensure_labels(host, cfg):
    made = []
    for name, color in ((cfg["label"], "5319e7"), (C.CLAIMED, C.LABEL_COLORS[C.CLAIMED])):
        if host.gh_transport(f"repos/{cfg['repo']}/labels/{urllib.parse.quote(name, safe='')}", allow_not_found=True) is None:
            host.gh_transport(f"repos/{cfg['repo']}/labels", "POST",
                              {"name": name, "color": color, "description": f"openswarm external lane ({cfg['name']})"})
            made.append(name)
    return made


def tick(host, cfg, dry_run=False, now=None):
    repo = cfg["repo"]
    own = _issues(host, repo, [cfg["label"]])
    ready = _issues(host, repo, [C.READY]) if cfg["auto_assign"] else []
    prs = host.gh_pages(f"repos/{repo}/pulls?state=open")
    memory = load_memory(host, cfg["name"])
    acts = plan(cfg, own, ready, prs, lambda n: host.gh_pages(f"repos/{repo}/pulls/{n}/files"), memory,
                time.time() if now is None else now)
    if not dry_run:
        for act in acts:
            apply(host, repo, act)
        save_memory(host, cfg["name"], memory)
    return {"backend": cfg["name"], "dry_run": dry_run, "own": len(own), "actions": acts}


def add_parser(sub):
    sp = sub.add_parser("external", help="GitHub-only external lanes (agents without SSH)")
    es = sp.add_subparsers(dest="external_cmd", required=True)
    for cmd, help_text in (("tick", "one dispatcher pass: assign, link PRs, thin gate, stale claims"),
                           ("init", "create the backend label and lane-claimed"),
                           ("reserved", "labels fleet dispatchers must skip (JSON)"),
                           ("status", "configured external backends and their cards")):
        p = es.add_parser(cmd, help=help_text)
        p.add_argument("--name", help="only this backend")
        if cmd == "tick":
            p.add_argument("--dry-run", action="store_true")


def cmd_external(args, host, policy):
    cfgs = backend_configs(policy)
    if args.name:
        if args.name not in cfgs:
            raise ExternalError(f"no policy.external_backends.{args.name}")
        cfgs = {args.name: cfgs[args.name]}
    if args.external_cmd == "reserved":
        return {"skip_labels": reserved_labels(backend_configs(policy)), "contract_version": C.CONTRACT_VERSION}
    out = []
    for cfg in cfgs.values():
        if not cfg["enabled"]:
            continue
        if args.external_cmd == "init":
            out.append({"backend": cfg["name"], "created": ensure_labels(host, cfg)})
        elif args.external_cmd == "status":
            own = _issues(host, cfg["repo"], [cfg["label"]])
            out.append({"backend": cfg["name"], "repo": cfg["repo"], "max_inflight": cfg["max_inflight"],
                        "cards": [{"n": i["number"], "labels": sorted(C.label_names(i)), "title": i.get("title")} for i in own]})
        else:
            out.append(tick(host, cfg, dry_run=args.dry_run))
    return out
