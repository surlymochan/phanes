"""Dispatcher side of GitHub-only external lanes (`openswarm external ...`).

An external lane is an agent that can read/write the repo on GitHub but cannot be reached over SSH (a hosted personal
agent, a CI bot, ...). It polls issues itself using the contract in sdk/external-lane/external_lane/contract.py; this
module is the dispatcher's half: keep up to max_inflight cards labelled for it, notice its PRs, gate thin PRs with a
commit status, and put stale claims back. Review and merge stay with the normal lane-review/merge gate.

Self-registration: an agent needs no config here. Every `backend:<name>` label whose name is not an internal backend
(`_internal`, default pi/codex/agy/copilot) is an external lane, and so is every name seen on a `lane-claimed` card (its
label is created if missing). New names start from the shared pool: the tick keeps `pool_size` starter cards labelled
`backend:external`, which any external agent may claim (the claim re-pins the card to `backend:<name>`). Refills go
only to names listed in policy (the allowlist) or names that have claimed before, so a typo cannot swallow cards.

Config (policy.external_backends, all optional except `_defaults.repo`):
  _defaults  {repo, max_inflight=1, claim_timeout_s, max_requeues, auto_assign, assign_exclude_hot, assign_from_backends,
              branch_prefixes, pool_size=1}
  _internal  [internal backend names]
  <name>     per-agent overrides; listing a name also allowlists it for refills.
Pure planning (`plan`) is separated from GitHub I/O.
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
            "auto_assign": True, "assign_exclude_hot": [], "assign_from_backends": [], "branch_prefixes": [],
            "min_effective_lines": C.MIN_EFFECTIVE_LINES, "pool_size": 1}
PRIO = {"p0": 0, "p1": 1, "p2": 2}


class ExternalError(RuntimeError):
    pass


def _raw(policy):
    raw = (policy or {}).get("external_backends") or {}
    if not isinstance(raw, dict):
        raise ExternalError("policy.external_backends must be an object")
    return raw


def internal_backends(policy):
    names = _raw(policy).get("_internal", list(C.INTERNAL_BACKENDS))
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        raise ExternalError("policy.external_backends._internal must be a list of names")
    return tuple(names)


def defaults(policy):
    d = _raw(policy).get("_defaults") or {}
    if not isinstance(d, dict):
        raise ExternalError("policy.external_backends._defaults must be an object")
    return dict(DEFAULTS, **d)


def backend_configs(policy, discovered=()):
    """Validated {name: cfg}: names listed in policy (allowlisted) plus discovered names (defaults only).
    Keys starting with '_' are settings/docs, not agents."""
    raw, base, internal = _raw(policy), defaults(policy), internal_backends(policy)
    entries = [(n, c, True) for n, c in raw.items() if not n.startswith("_")]
    entries += [(n, {}, False) for n in sorted(set(discovered)) if n not in raw and n not in internal]
    out = {}
    for name, cfg, listed in entries:
        if not isinstance(cfg, dict):
            raise ExternalError(f"policy.external_backends.{name} must be an object")
        c = dict(base, **cfg)
        c["allowlisted"] = listed
        if name in internal:
            raise ExternalError(f"policy.external_backends.{name}: {name} is an internal backend")
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


def pool_config(policy):
    base = defaults(policy)
    return dict(base, name=C.POOL_NAME, label=C.POOL, allowlisted=True, max_inflight=base["pool_size"],
                branch_prefixes=[], enabled=base["pool_size"] > 0)


def reserved_labels(cfgs):
    """Labels a fleet dispatcher must never hand to its own lanes: known external labels, the pool, lane-claimed.
    Dynamic names are covered by the rule in `reserved_rule` (contract.fleet_must_skip)."""
    return sorted({c["label"] for c in cfgs.values() if c["enabled"]} | {C.CLAIMED, C.POOL})


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
    if linked or any(C.label_names(i) & {C.CLAIMED, C.PR_OPEN} for i in own):
        memory["claimed_before"] = True
    active = [i for i in own if C.label_names(i) & {C.READY, C.CLAIMED} and not C.label_names(i) & {C.PR_OPEN, C.FAILED}]
    free = cfg["max_inflight"] - len(active)
    # refills only for allowlisted names or names that have proven themselves with a claim (typos never get cards)
    if cfg["auto_assign"] and (cfg.get("allowlisted", True) or memory.get("claimed_before")) and free > 0:
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


def discover(labels, claimed, internal):
    """External names from repo labels (`backend:<name>`) and from claimed cards. -> (names, names seen only on claims)."""
    from_labels = {C.external_name(l.get("name") if isinstance(l, dict) else l, internal) for l in labels} - {None}
    from_claims = {C.external_name(l, internal) for i in claimed for l in C.label_names(i)} - {None}
    return from_labels | from_claims, from_claims - from_labels


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


def ensure_label(host, repo, name, color, description):
    host.gh_transport(f"repos/{repo}/labels", "POST", {"name": name, "color": color, "description": description[:100]})


INBOX_BODY = """Inbox for external lane **{name}**. **Never merge** (draft, empty commit).

When a card becomes `lane-ready` for `backend:{name}`, the `external-lane-notify` workflow comments here
(once per card per 24 h). Agents whose platform delivers only pull-request events subscribe to comments on this PR,
then run `openswarm external check --agent {name}` and, on rc 0, one round. Guide: openswarm/sdk/external-lane/README.md.
Lane review, merge-green and the external tick ignore it (branch `{prefix}*`, label `{label}`, draft)."""


def ensure_inbox(host, repo, name):
    """Idempotently create the agent's inbox PR (draft, empty commit on top of main, label external-inbox). -> PR number."""
    branch, owner = C.inbox_branch(name), repo.split("/")[0]
    found = host.gh_pages(f"repos/{repo}/pulls?state=open&head={urllib.parse.quote(owner + ':' + branch, safe=':')}")
    if found:
        return found[0]["number"], False
    if host.gh_transport(f"repos/{repo}/git/ref/heads/{branch}", allow_not_found=True) is None:
        head = host.gh_transport(f"repos/{repo}/commits/main")
        commit = host.gh_transport(f"repos/{repo}/git/commits", "POST", {
            "message": f"external-inbox: {name} (empty commit; never merge)",
            "tree": head["commit"]["tree"]["sha"], "parents": [head["sha"]]})
        host.gh_transport(f"repos/{repo}/git/refs", "POST", {"ref": f"refs/heads/{branch}", "sha": commit["sha"]})
    if host.gh_transport(f"repos/{repo}/labels/{C.INBOX_LABEL}", allow_not_found=True) is None:
        host.gh_transport(f"repos/{repo}/labels", "POST", {"name": C.INBOX_LABEL, "color": "c5def5",
                                                           "description": "external lane inbox PR: never merge, never review"})
    pr = host.gh_transport(f"repos/{repo}/pulls", "POST", {
        "head": branch, "base": "main", "draft": True, "title": f"external-inbox: {name} (wake-up notices; never merge)",
        "body": INBOX_BODY.format(name=name, prefix=C.INBOX_PREFIX, label=C.INBOX_LABEL)})
    host.gh_transport(f"repos/{repo}/issues/{pr['number']}/labels", "POST", {"labels": [C.INBOX_LABEL]})
    return pr["number"], True


def tick(host, cfg, dry_run=False, now=None, ready=None, prs=None):
    repo = cfg["repo"]
    own = _issues(host, repo, [cfg["label"]])
    ready = (_issues(host, repo, [C.READY]) if ready is None else ready) if cfg["auto_assign"] else []
    prs = host.gh_pages(f"repos/{repo}/pulls?state=open") if prs is None else prs
    memory = load_memory(host, cfg["name"])
    acts = plan(cfg, own, ready, prs, lambda n: host.gh_pages(f"repos/{repo}/pulls/{n}/files"), memory,
                time.time() if now is None else now)
    if not dry_run:
        for act in acts:
            apply(host, repo, act)
        save_memory(host, cfg["name"], memory)
    return {"backend": cfg["name"], "dry_run": dry_run, "own": len(own), "actions": acts}


def tick_all(host, policy, name=None, dry_run=False, now=None):
    """Discover external names, then one pass per name (named agents first, then the starter pool)."""
    base = defaults(policy)
    repo = base.get("repo")
    if not isinstance(repo, str) or repo.count("/") != 1:
        raise ExternalError("dispatcher not configured: set policy.external_backends._defaults.repo "
                            "(agents run `openswarm external check|tick --agent <name>` instead)")
    internal = internal_backends(policy)
    names, claim_only = discover(host.gh_pages(f"repos/{repo}/labels"), _issues(host, repo, [C.CLAIMED]), internal)
    out = []
    for n in sorted(claim_only):
        if not dry_run:
            ensure_label(host, repo, C.backend_label(n), "5319e7", f"openswarm external lane ({n})")
        out.append({"backend": n, "label_created": not dry_run})
    cfgs = backend_configs(policy, names)
    if name:
        cfgs = {k: v for k, v in cfgs.items() if k == name}
    ready = _issues(host, repo, [C.READY])
    prs = host.gh_pages(f"repos/{repo}/pulls?state=open")
    taken = set()
    for cfg in list(cfgs.values()) + ([pool_config(policy)] if not name else []):
        if not cfg["enabled"]:
            continue
        res = tick(host, cfg, dry_run, now, [i for i in ready if i["number"] not in taken], prs)
        taken |= {a["n"] for a in res["actions"] if a["op"] == "labels"}
        out.append(res)
    return out


def add_parser(sub):
    sp = sub.add_parser("external", help="GitHub-only external lanes (agents without SSH)")
    es = sp.add_subparsers(dest="external_cmd", required=True)
    for cmd, help_text in (("check", "AGENT wake gate: rc 0 = a card to take, 1 = nothing, 2 = error (read-only, 1 JSON line)"),
                           ("tick", "with --agent: one agent pass (resume or claim 1 card); without: one dispatcher pass"),
                           ("init", "create backend labels, backend:external and lane-claimed"),
                           ("reserved", "labels/rule fleet dispatchers must skip (JSON)"),
                           ("status", "external backends and their cards")):
        p = es.add_parser(cmd, help=help_text)
        p.add_argument("--name", help="dispatcher: only this backend")
        if cmd in ("check", "tick"):
            p.add_argument("--agent", required=cmd == "check", help="your agent name (label backend:<agent>)")
            p.add_argument("--repo", help="owner/name (default: origin of the current clone)")
        if cmd == "check":
            p.add_argument("--max-wakes-per-hour", type=int)
            p.add_argument("--fail-cooldown", type=int, metavar="SECONDS")
            p.add_argument("--state-file")
        if cmd == "tick":
            p.add_argument("--dry-run", action="store_true", help="dispatcher only")
            p.add_argument("--work-cmd", help="agent: shell command that edits + commits")
            p.add_argument("--dir", help="agent: clone location for --work-cmd")
            p.add_argument("--no-verify", action="store_true")


def agent_argv(args):
    """`openswarm external check|tick --agent X ...` -> the SDK CLI (same code an agent gets with `python3 -m external_lane`)."""
    argv = ["--agent", args.agent] + (["--repo", args.repo] if args.repo else []) + [args.external_cmd]
    for flag, key in (("--max-wakes-per-hour", "max_wakes_per_hour"), ("--fail-cooldown", "fail_cooldown"),
                      ("--state-file", "state_file"), ("--work-cmd", "work_cmd"), ("--dir", "dir")):
        if getattr(args, key, None) is not None:
            argv += [flag, str(getattr(args, key))]
    if getattr(args, "no_verify", False):
        argv.append("--no-verify")
    return argv


def cmd_external(args, host, policy):
    if getattr(args, "agent", None):
        from external_lane import cli
        raise SystemExit(cli.main(agent_argv(args)))
    if args.external_cmd == "reserved":
        return {"skip_labels": reserved_labels(backend_configs(policy)), "internal": list(internal_backends(policy)),
                "skip_rule": "lane-claimed, backend:external, or backend:<name> with name not internal",
                "contract_version": C.CONTRACT_VERSION}
    if args.external_cmd == "tick":
        return tick_all(host, policy, args.name, dry_run=args.dry_run)
    cfgs = backend_configs(policy)
    if args.name:
        if args.name not in cfgs:
            raise ExternalError(f"no policy.external_backends.{args.name}")
        cfgs = {args.name: cfgs[args.name]}
    out = []
    if args.external_cmd == "init":  # inbox PRs only for allowlisted (policy-listed) names
        cfgs = {k: v for k, v in cfgs.items() if v.get("allowlisted")}
    for cfg in cfgs.values():
        if not cfg["enabled"]:
            continue
        if args.external_cmd == "init":
            inbox, made = ensure_inbox(host, cfg["repo"], cfg["name"])
            out.append({"backend": cfg["name"], "created": ensure_labels(host, cfg), "inbox_pr": inbox, "inbox_created": made})
        else:
            own = _issues(host, cfg["repo"], [cfg["label"]])
            out.append({"backend": cfg["name"], "repo": cfg["repo"], "max_inflight": cfg["max_inflight"],
                        "cards": [{"n": i["number"], "labels": sorted(C.label_names(i)), "title": i.get("title")} for i in own]})
    if args.external_cmd == "init":
        pool = pool_config(policy)
        if isinstance(pool.get("repo"), str):
            out.append({"backend": C.POOL_NAME, "created": ensure_labels(host, pool)})
    return out
