#!/usr/bin/env python3
"""Phanes lane autodispatcher (runs on the box).

Every TICK seconds: finish runs that ended (push branch + open PR, relabel issue), then give every idle
lane the next open issue labelled `lane-ready` (not `in-progress`), ordered p0 < p1 < p2 < unlabelled,
then by issue number. Optional `backend:pi|agy|codex` label pins an issue to that executor family.
State: ~/phanes-ctl/autodispatch/state.json   Log: ~/phanes-ctl/autodispatch/autodispatch.log + brain/log.md
Pause: touch ~/phanes-ctl/autodispatch/PAUSE (whole loop) or PAUSE-<LANE> (one lane).
"""
import json, os, re, shlex, subprocess, sys, time, concurrent.futures as cf
HOME = os.path.expanduser("~"); D = f"{HOME}/phanes-ctl/autodispatch"
REPO = "surlymochan/phanes"; LOCAL = "/workspace/phanes-repo"; BRAIN_LOG = "/workspace/phanes/brain/log.md"
TICK = int(os.environ.get("AD_TICK", "60")); BUDGET = os.environ.get("AD_BUDGET", "45"); LOW = int(os.environ.get("AD_LOW", "10")); TARGET = int(os.environ.get("AD_TARGET", "20"))
RULES = ("Rules: work only inside this repository worktree (the current directory). Never create, modify, move or delete "
         "files outside it — not the main repo, not $HOME, not /tmp; for scratch files use $TMPDIR (inside the worktree, cleaned "
         "automatically) and leave cleanup to the executor. Do not push, do not open PRs, do not touch credentials, no network "
         "installs. Keep the change small and factual; cite file paths and line numbers you checked. Commit your result with a "
         "clear message referencing the issue (\"Refs #{n}\"). Write in the same language as the issue.")
PRIO = {"p0": 0, "p1": 1, "p2": 2}
# One multiplexed connection per host (cuts handshakes ~10x; banner-exchange timeouts came from bursts of parallel ssh).
SSHO = ["-o", "ConnectTimeout=20", "-o", "BatchMode=yes", "-o", "ServerAliveInterval=15", "-o", "ControlMaster=auto",
        "-o", f"ControlPath={HOME}/.ssh/cm/%r@%h:%p", "-o", "ControlPersist=600"]
BACKOFF = {}  # host -> {"n": consecutive failures, "until": epoch}; transient ssh errors never mark a lane or issue failed
HOST_BACKOFF_BASE = 30
HOST_BACKOFF_MAX = 600
HARVEST_FETCH_MAX_ATTEMPTS = 3  # initial fetch plus two bounded retries
HARVEST_FETCH_RETRY_DELAY = 20

def host_ok(h): return time.time() >= BACKOFF.get(h, {}).get("until", 0)
def host_fail(h, why):
    b = BACKOFF.setdefault(h, {"n": 0, "until": 0}); b["n"] += 1
    wait = min(HOST_BACKOFF_MAX, HOST_BACKOFF_BASE * 2 ** (b["n"] - 1)); b["until"] = time.time() + wait
    log(f"{h} ssh transient error #{b['n']} ({why[-80:]}); backing off {wait}s", brain=(b["n"] == 4))
def host_good(h):
    if BACKOFF.pop(h, None): log(f"{h} ssh recovered", brain=False)

def now(): return time.strftime("%H:%M")
def log(msg, brain=True):
    line = f"{time.strftime('%F %T')} {msg}"
    print(line, flush=True)
    with open(f"{D}/autodispatch.log", "a") as f: f.write(line + "\n")
    if brain:
        with open(BRAIN_LOG, "a") as f: f.write(f"- {now()} UTC+8 autodispatch: {msg}\n")
def sh(cmd, timeout=120, inp=None):
    p = subprocess.run(cmd, shell=isinstance(cmd, str), capture_output=True, text=True, timeout=timeout, input=inp)
    return p.returncode, p.stdout, p.stderr
def load(p, d):
    try: return json.load(open(p))
    except Exception: return d
def save_state(s):
    tmp = f"{D}/state.json.tmp"; json.dump(s, open(tmp, "w"), indent=1, ensure_ascii=False); os.replace(tmp, f"{D}/state.json")
def gh(args, timeout=60):
    return sh(["gh"] + args + ["-R", REPO], timeout=timeout)

REMOTE_STATUS = r'''
import json,os,sys
O=os.path.expanduser("~/.claude/openswarm-executor-candidate"); L=sys.argv[1]; R=sys.argv[2] if len(sys.argv)>2 else ""
out={"lane":None,"run":None}
try:
  ls=json.load(open(f"{O}/lanes/{L}/state.json")); out["lane"]=ls.get("state"); out["current_run"]=ls.get("current_run")
except Exception as e: out["lane"]="unknown"
if R:
  p=f"{O}/runs/{R}/result.json"
  if os.path.exists(p):
    r=json.load(open(p)); res=r.get("result") or {}
    out["run"]={"status":r.get("status"),"branch":r.get("branch"),"commit":r.get("commit"),"repo":(r.get("workspace") or {}).get("repo"),
      "backend":r.get("backend"),"model":r.get("model"),"error_class":r.get("error_class"),"summary":str(res.get("summary") if isinstance(res,dict) else res)[:600],
      "verify_rc":(r.get("verify") or {}).get("rc") if isinstance(r.get("verify"),dict) else None}
  else:
    out["run"]="running"
    try:
      s=json.load(open(f"{O}/runs/{R}/status.json")); pid=s.get("runner_pid"); out["run_state"]=s.get("state")
      if pid:
        try: os.kill(int(pid),0); out["runner_alive"]=True
        except ProcessLookupError: out["runner_alive"]=False
        except PermissionError: out["runner_alive"]=True
    except Exception: out["run_state"]="missing"
print(json.dumps(out))
'''
def remote(lane, cfg, run=""):
    try: rc, out, err = sh(["ssh", *SSHO, cfg["host"], "python3", "-", lane, run], timeout=60, inp=REMOTE_STATUS)
    except subprocess.TimeoutExpired: return {"error": "ssh status timeout"}
    if rc: return {"error": (err or out).strip()[-200:]}
    try: return json.loads(out.strip().splitlines()[-1])
    except Exception: return {"error": out[-200:]}

def queue():
    rc, out, err = gh(["issue", "list", "--state", "open", "--label", "lane-ready", "--limit", "100", "--json", "number,title,labels,body"])
    if rc: raise RuntimeError(err)
    items = []
    for i in json.loads(out):
        labels = {l["name"] for l in i["labels"]}
        if "in-progress" in labels: continue
        pr = min([PRIO[l] for l in labels if l in PRIO] or [3])
        be = next((l.split(":", 1)[1] for l in labels if l.startswith("backend:")), None)
        items.append({"n": i["number"], "title": i["title"], "body": i["body"] or "", "prio": pr, "backend": be, "strict": "strict-backend" in labels})
    return sorted(items, key=lambda x: (x["prio"], x["n"]))

SOCKET_RE = re.compile(r"unix\s*socket|AF_UNIX|socketpair|needs-socket|Unix 套接字|套接字", re.I)
def can_take(backend, item):
    """Exact pin or unpinned always; cross-pin allowed unless `strict-backend`, except codex never takes Unix-socket work
    (codex sandbox blocks AF_UNIX bind) — so idle agy/codex lanes drain the pi-heavy queue instead of sitting idle."""
    if item["backend"] in (None, backend): return True
    if item.get("strict"): return False
    if backend == "codex" and SOCKET_RE.search(item["title"] + "\n" + item["body"]): return False
    return True
def rank(backend, item):  # own pin first, then unpinned, then cross-pinned; within each by priority, number
    return (0 if item["backend"] == backend else 1 if item["backend"] is None else 2, item["prio"], item["n"])

def age_s(st):
    try: return time.time() - time.mktime(time.strptime(st["at"], "%Y-%m-%d %H:%M:%S"))
    except Exception: return 1e9

def reconcile(L, cfg, st, r, state):
    """Local state says busy but the remote lane is idle on another/no run, the run has no result.json and its runner is dead:
    the run was orphaned (lane restart / reassignment). Drop it; requeue the issue unless it already got a PR or closed."""
    if r.get("run") != "running" or r.get("lane") != "idle" or r.get("current_run") == st["run"]: return False
    if r.get("runner_alive") or (r.get("runner_alive") is None and age_s(st) < 600) or age_s(st) < 180: return False
    n = st["issue"]; _, out, _ = gh(["issue", "view", str(n), "--json", "state,labels"])
    try: j = json.loads(out); labels = {l["name"] for l in j["labels"]}
    except Exception: return False
    if j["state"] == "OPEN" and "lane-pr" not in labels:
        gh(["issue", "edit", str(n), "--remove-label", "in-progress", "--add-label", "lane-ready"])
        what = "requeued (lane-ready)"
    else: what = f"already {j['state'].lower()}/{'lane-pr' if 'lane-pr' in labels else '-'}; dropped"
    log(f"{L} #{n} run {st['run']} orphaned (remote lane idle, current_run={r.get('current_run')}, run_state={r.get('run_state')}, runner dead) → {what}")
    return True

def verify_of(body):
    for line in reversed(body.splitlines()):
        if re.match(r"\s*(验收|Verify|verify|Acceptance)\s*[:：]", line):
            cmds = re.findall(r"`([^`]+)`", line)
            if cmds: return " && ".join(cmds) if len(cmds) > 1 and all(not c.startswith("cd ") for c in cmds[1:]) else cmds[0]
    return None

def fresh_ready(n, state):
    """`gh issue list --label` goes through the search index, which lags label edits; re-read the issue directly."""
    # labels from `gh issue view` are authoritative; `_done` alone no longer blocks (a re-labelled lane-ready issue is retried)
    rc, out, _ = gh(["issue", "view", str(n), "--json", "state,labels"])
    if rc: return False
    j = json.loads(out); labels = {l["name"] for l in j["labels"]}
    if not (j["state"] == "OPEN" and "lane-ready" in labels and not labels & {"in-progress", "lane-pr", "lane-failed"}): return False
    return True

def deps_met(item):
    """Body line `依赖：#12 #34` (or `Depends on: #12`) gates dispatch until those issues are CLOSED."""
    m = re.search(r"^\s*(?:依赖|Depends on)\s*[:：](.*)$", item["body"], re.M)
    for d in re.findall(r"#(\d+)", m.group(1)) if m else []:
        rc, out, _ = gh(["issue", "view", d, "--json", "state"])
        if rc or json.loads(out)["state"] != "CLOSED": return False
    return True

def refill_need(ready, low=LOW, target=TARGET, starve=0):
    """Return the refill size when a low queue or backend starvation needs work."""
    if ready >= low and starve <= 0:
        return 0
    return max(target - ready, starve)

def dispatch(lane, cfg, item, base):
    n = item["n"]; task = f"# #{n} {item['title']}\n\n{item['body']}\n\n{RULES.format(n=n)}\n"
    path = f"{D}/tasks/{lane}-{n}.md"; os.makedirs(f"{D}/tasks", exist_ok=True); open(path, "w").write(task)
    h = cfg["host"]; F = cfg.get("fleet", "~/.local/bin/openswarm-fleet"); R = cfg.get("repo", "~/work/phanes-fleet")
    with open(path) as task_file:
        rc, _, err = sh(["ssh", *SSHO, h, f"cat > /tmp/task-{lane}.md"], timeout=60, inp=task_file.read())
    if rc: raise RuntimeError(f"task upload: {err.strip()[-200:]}")
    ver = verify_of(item["body"]); title = f"#{n} {item['title']}"[:120]
    cmd = (f"git -C {R} fetch -q https://github.com/{REPO}.git main && git -C {R} cat-file -e {base}^{{commit}} && "
           f"{F} lane assign {lane} --task-file /tmp/task-{lane}.md --base {base} --budget-minutes {BUDGET} --title {shlex.quote(title)}"
           + (f" --verify {shlex.quote(ver)}" if ver else ""))
    rc, out, err = sh(["ssh", *SSHO, h, cmd], timeout=180)
    m = re.search(r'"id":\s*"([^"]+)"', out)
    if rc or not m: raise RuntimeError(f"assign rc={rc}: {(err or out).strip()[-300:]}")
    return m.group(1), ver

def git_fetch_command(ssh_options, local_repo, remote, branch, lane):
    """Build the harvest fetch argv without splitting SSH option values twice."""
    ssh_command = shlex.join(["ssh", *ssh_options])
    return ["git", "-c", f"core.sshCommand={ssh_command}", "-C", local_repo,
            "fetch", "-q", remote, f"+{branch}:refs/fleet/{lane}"]

def should_retry_harvest_fetch(error, attempt, max_attempts=HARVEST_FETCH_MAX_ATTEMPTS):
    """Return whether a failed harvest fetch is transient and still within its retry bound."""
    transient = re.search(r"Permission denied|Connection closed|broken pipe|timed out|banner", error or "", re.I)
    return bool(transient and attempt < max_attempts)

def mark_done(state, n):
    state.setdefault("_done", []); state["_done"] = (state["_done"] + [n])[-200:]

def finish(lane, cfg, st, info, state):
    n, run = st["issue"], st["run"]
    if info.get("branch") and info["branch"] != "None" and info.get("commit"):
        b = f"fleet/{lane}-issue{n}-{run}"
        fetch = git_fetch_command(SSHO, LOCAL, f"{cfg['host']}:{info['repo']}", info["branch"], lane)
        for attempt in range(1, HARVEST_FETCH_MAX_ATTEMPTS + 1):
            rc, _, err = sh(fetch, timeout=180)
            if not rc: break
            if should_retry_harvest_fetch(err, attempt):
                log(f"{lane} #{n} fetch failed attempt {attempt}/{HARVEST_FETCH_MAX_ATTEMPTS} ({err.strip()[-90:]}); retrying in {HARVEST_FETCH_RETRY_DELAY}s", brain=False)
                time.sleep(HARVEST_FETCH_RETRY_DELAY)
            else:
                log(f"{lane} #{n} fetch failed after {attempt}/{HARVEST_FETCH_MAX_ATTEMPTS} attempts ({err.strip()[-120:]})", brain=False)
        if not rc: rc, _, err = sh(["git", "-C", LOCAL, "push", "-q", "origin", f"refs/fleet/{lane}:refs/heads/{b}"], timeout=180)
        if rc: return log(f"{lane} #{n} run {run}: push failed: {err.strip()[-200:]}") or False
        _, lj, _ = gh(["issue", "view", str(n), "--json", "labels"])
        if "lane-pr" in lj:
            gh(["issue", "comment", str(n), "--body", f"Lane {lane} also finished this issue (run `{run}`, duplicate dispatch); branch `{b}` pushed for comparison, no second PR opened."])
            log(f"{lane} finished #{n} (run {run}) duplicate → branch {b}, no PR"); mark_done(state, n); return True
        _, subj, _ = sh(["git", "-C", LOCAL, "log", "-1", "--format=%s", f"refs/fleet/{lane}"])
        body = (f"Closes #{n}\n\nAutodispatched to lane **{lane}** on **{cfg['host']}** ({info.get('backend')}/{info.get('model')}), run `{run}`, "
                f"base `{st['base'][:8]}`, lane status `{info.get('status')}`, host verify rc `{info.get('verify_rc')}`.\n\nExecutor summary: {info.get('summary')}")
        rc, out, err = gh(["pr", "create", "--base", "main", "--head", b, "--title", (subj.strip() or f"#{n}")[:200], "--body", body])
        url = (out.strip().splitlines() or [""])[-1] if not rc else f"(pr create failed: {err.strip()[-160:]})"
        gh(["issue", "edit", str(n), "--remove-label", "in-progress", "--remove-label", "lane-ready", "--add-label", "lane-pr"])
        log(f"{lane} finished #{n} (run {run}, {info.get('status')}) → {url}"); mark_done(state, n)
    else:
        gh(["issue", "edit", str(n), "--remove-label", "in-progress", "--remove-label", "lane-ready", "--add-label", "lane-failed"])
        gh(["issue", "comment", str(n), "--body", f"Lane {lane} ({cfg['host']}, run `{run}`) ended `{info.get('status')}` without a commit "
            f"(error_class `{info.get('error_class')}`).\n\nExecutor summary: {info.get('summary')}\n\nLabel `lane-failed`; re-add `lane-ready` to retry."])
        mark_done(state, n); log(f"{lane} ended #{n} without a commit (run {run}, {info.get('status')}/{info.get('error_class')}) → labelled lane-failed")
    return True

def tick(lanes, state, last_warn):
    if os.path.exists(f"{D}/PAUSE"): return last_warn
    state.setdefault("_done", [])
    live = [L for L in lanes if host_ok(lanes[L]["host"])]
    with cf.ThreadPoolExecutor(4) as ex:
        res = dict(zip(live, ex.map(lambda L: remote(L, lanes[L], (state.get(L) or {}).get("run", "")), live)))
    for L in lanes:
        if L not in res: res[L] = {"error": "host in backoff", "skip": True}
    seen = set()
    for L in live:
        h = lanes[L]["host"]
        if h in seen: continue
        errs = [res[x] for x in live if lanes[x]["host"] == h and "error" in res[x]]
        if errs: host_fail(h, errs[0]["error"]); seen.add(h)
        else: host_good(h); seen.add(h)
    for L, r in res.items():
        st = state.get(L)
        if "error" in r: continue
        if st and isinstance(r.get("run"), dict):
            if finish(L, lanes[L], st, r["run"], state): state.pop(L); save_state(state)
        elif st and reconcile(L, lanes[L], st, r, state): state.pop(L); save_state(state)
    try: q = queue()
    except Exception as e: log(f"queue error: {e}", brain=False); return last_warn
    for i in q: i["deps_ok"] = deps_met(i)
    blocked = sum(not i["deps_ok"] for i in q)
    base = None
    for L, cfg in lanes.items():
        r = res[L]
        if "error" in r or not host_ok(cfg["host"]) or state.get(L) or r.get("lane") != "idle" or os.path.exists(f"{D}/PAUSE-{L}"): continue
        pick = None
        for c in sorted((i for i in q if i.get("deps_ok", True) and can_take(cfg["backend"], i)), key=lambda i: rank(cfg["backend"], i)):
            if fresh_ready(c["n"], state): pick = c; break
            q.remove(c); log(f"skip #{c['n']}: stale search index (already claimed/done)", brain=False)  # try the next one this tick
        if not pick: continue
        if base is None:
            _, out, _ = sh(["git", "ls-remote", f"https://github.com/{REPO}.git", "refs/heads/main"]); base = out.split()[0]
        gh(["issue", "edit", str(pick["n"]), "--add-label", "in-progress"])
        try: run, ver = dispatch(L, cfg, pick, base)
        except Exception as e:
            gh(["issue", "edit", str(pick["n"]), "--remove-label", "in-progress"]); host_fail(cfg["host"], f"dispatch #{pick['n']}: {e}"); continue
        q.remove(pick); state[L] = {"issue": pick["n"], "run": run, "host": cfg["host"], "base": base, "at": time.strftime("%F %T"), "verify": ver}
        save_state(state); log(f"dispatched #{pick['n']} → {L} ({cfg['host']}) run {run} base {base[:8]}")
    ready = len(q) - blocked
    # refill when the queue is short, or when some backend's lanes could not all be fed (counting cross-pin fallback)
    short = {b: sum(c["backend"] == b for c in lanes.values()) - sum(1 for i in q if i.get("deps_ok", True) and can_take(b, i))
             for b in {c["backend"] for c in lanes.values()}}
    starve = max(short.values(), default=0)
    need = refill_need(ready, LOW, TARGET, starve)
    if need:
        filed = replenish(need, state)
        why = f"queue low ({ready} < {LOW}, target {TARGET})" if ready < LOW else f"backend starved ({', '.join(f'{b} short {k}' for b, k in short.items() if k > 0)})"
        if filed: log(f"{why}: filed {len(filed)} issue(s) from backlog: {' '.join(filed)}")
        elif time.time() - last_warn > 600:
            log(f"WARNING queue low: {ready} dispatchable lane-ready issue(s) left (+{blocked} waiting on 依赖), backlog/ empty (threshold {LOW})"); last_warn = time.time()
    st = {L: ("busy #%s" % state[L]["issue"]) if state.get(L) else (res[L].get("error") or res[L].get("lane") or "?") for L in lanes}
    st["_busy"] = sum(1 for L in lanes if state.get(L)); st["_ready"] = ready; st["_blocked"] = blocked; st["_at"] = time.strftime("%F %T")
    json.dump(st, open(f"{D}/lanes-status.json", "w"), ensure_ascii=False, indent=1)
    return last_warn

def file_issue(text, extra=()):
    lines = text.splitlines()
    if is_thin_card(text): raise ValueError("thin backlog card")
    title = lines[0].lstrip("# ").strip(); labels = [l.strip() for l in lines[1].split(":", 1)[1].split(",") if l.strip()]
    body = "\n".join(lines[2:]).strip() + "\n"
    args = ["issue", "create", "-t", title, "-b", body]
    for l in ["lane-ready", *labels, *extra]: args += ["-l", l]
    rc, o, e = gh(args)
    if rc: raise RuntimeError(e.strip()[-200:])
    return "#" + o.strip().rsplit("/", 1)[-1]

def is_thin_card(text):
    """Reject backlog entries that contain only a title/labels header."""
    lines = text.splitlines()
    return len(lines) < 3 or not "\n".join(lines[2:]).strip()

PLANNER = """# 规划：为 lane 队列写 {k} 条新任务（{tag}）
labels: p1,backend:codex,planner
自动补队列任务。读 main 上的代码与 `docs/reviews/`、`docs/scenario/`、`openbot/examples/org-health-local/`，写 {k} 个新文件 `docs/backlog/next/{tag}-01.md` … `{tag}-{k:02d}.md`，每个文件是一条可独立派给 lane 的小任务，格式严格为：

```
# <中文标题>
labels: <p1 或 p2>,backend:<pi|codex|agy>
<任务正文：要改的文件路径、具体改动、边界；不得依赖其它未完成任务>

验收：`<一条在仓库根或 cd 子目录后可直接运行、只用 python3 标准库/现有脚本的命令>`
```

要求：{k} 条里 backend:pi / backend:agy / backend:codex / 无 backend 标签 大约 2:1:2:1（至少 3 条无 backend 标签供饥饿兜底）；优先 #6 三 Bot 组织诊断 e2e（openbot/examples/org-health-local）的测试覆盖、文档、健壮性小修，其次 openswarm 的纯单元测试（不需要 Unix socket 的可标 codex，需要的标 pi）；每条 15 分钟内可完成；不要写安全加固、不要改 CI 的 macOS 矩阵、不要碰签名；不要与下列已有 issue 重复：

{open}

只新增 `docs/backlog/next/` 下的文件，不改其它文件。

验收：`ls docs/backlog/next/{tag}-*.md | wc -l | grep -qx {k}`
"""

def replenish(k, state):
    """Queue refill, in order: (1) ~/phanes-ctl/autodispatch/backlog/*.md (`# title` / `labels: a,b` / body; moved to backlog/filed/);
    (2) docs/backlog/next/*.md on origin/main not yet imported (written by planner runs); (3) if still short and no planner
    issue is open, file one planner issue asking a lane to write the next batch into docs/backlog/next/."""
    bd = f"{D}/backlog"; os.makedirs(f"{bd}/filed", exist_ok=True); out = []
    for f in sorted(x for x in os.listdir(bd) if x.endswith(".md")):
        if len(out) >= k: return out
        try: out.append(file_issue(open(f"{bd}/{f}").read()))
        except Exception as e: log(f"backlog {f}: {e}", brain=False); return out
        os.replace(f"{bd}/{f}", f"{bd}/filed/{f}")
    G = ["git", "-C", "/workspace/phanes-repo"]
    sh(G + ["fetch", "-q", "origin", "main"], timeout=120)
    rc, ls, _ = sh(G + ["ls-tree", "--name-only", "origin/main", "docs/backlog/next/"])
    imp = state.setdefault("_imported", [])
    for path in sorted(x for x in ls.split() if x.endswith(".md")) if not rc else []:
        if len(out) >= k: break
        if path in imp: continue
        rc, text, _ = sh(G + ["show", f"origin/main:{path}"])
        imp.append(path); save_state(state)
        try:
            if rc or not text.startswith("#") or not text.splitlines()[1].startswith("labels:"): raise RuntimeError("bad format")
            out.append(file_issue(text))
        except Exception as e: log(f"repo backlog {path}: {e}", brain=False)
    if len(out) < k:
        rc, o, _ = gh(["issue", "list", "--state", "open", "--label", "planner", "--json", "number"])
        if not rc and json.loads(o) == []:
            rc, o, _ = gh(["issue", "list", "--state", "open", "--limit", "200", "--json", "number,title"])
            titles = "\n".join(f"- #{i['number']} {i['title']}" for i in json.loads(o)) if not rc else ""
            tag = time.strftime("%Y%m%d-%H%M")
            try: out.append(file_issue(PLANNER.format(k=20, tag=tag, open=titles)) + "(planner)")
            except Exception as e: log(f"planner issue: {e}", brain=False)
    return out

def main():
    lanes = load(f"{D}/lanes.json", {}); last_warn = 0
    if "--once" in sys.argv: tick(lanes, load(f"{D}/state.json", {}), 0); return
    while True:
        try: last_warn = tick(load(f"{D}/lanes.json", lanes), load(f"{D}/state.json", {}), last_warn)
        except Exception as e: log(f"tick error: {e!r}", brain=False)
        open(f"{D}/heartbeat", "w").write(time.strftime("%F %T") + "\n")
        time.sleep(TICK)
if __name__ == "__main__": main()
