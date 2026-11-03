#!/usr/bin/env python3
"""Collect the Pi spawn-path evidence of a lane that fails before it produces a report (Refs #1747).

A pi lane that fails with `exec_error` and without an agent-side error string used to leave
nothing to read: the resident executor writes its reason to stderr, and the lane child dropped
stderr on the floor (`openswarm/bin/openswarm`, `stderr=subprocess.DEVNULL`).  The lane's
`pi-sessions` directory is empty by construction in RPC mode (the wrapper runs pi with
`--session-dir /tmp/pi-sessions` *inside* the sandbox), so it is not evidence either.

Two kinds of evidence are kept apart, because only one of them can speak about a past failure:

* the **historical** evidence: what the failed runs themselves recorded (`result.json`,
  `status.json`, the events file).  This is the only evidence about the incident.
* the **current state**: the spawn preconditions as they hold *now*, evaluated with the
  supervisor's own environment and the supervisor's own wrapper.  A green result here does not
  prove anything about the runs that already failed, and a red one does not explain them.

The current state is read from the lane supervisor itself, never from the shell that runs this
tool: the supervisor's environment comes from `/proc/<pid>/environ` (or, when the supervisor is
not running, from its systemd unit), and the wrapper is the `pi-isolated` next to the
supervisor's own `openswarm` binary, identified by its sha256.  A wrapper that predates
`--preflight` is reported as such and the guards are evaluated locally instead of stopping the
investigation.

Everything is read-only: no lane state, run directory or broker is modified.

Usage:
  check-pi-spawn-failure.py --lane M9
  check-pi-spawn-failure.py --lane M9 --run-id 20261103-004712-abc123
  check-pi-spawn-failure.py --lane M9 --run-dir ~/.claude/openswarm/runs/<id>
  check-pi-spawn-failure.py --lane M9 --json
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PROC_ROOT = Path("/proc")
ISOLATED_TOOLS = "read,bash,edit,write,grep,find,ls"
DEFAULT_MODEL = "MiniMax-M3.1-Flash"
PI_ENV_VARS = ("OPENSWARM_PI_RUNTIME", "OPENSWARM_PI_BROKER_SOCKET", "OPENSWARM_PI_SANDBOX_TMP",
               "OPENSWARM_PI_DEPS_ROOT", "OPENSWARM_PI_ISOLATED_MODEL")
UNIT_NAMES = ("openswarm-lane-{lane}.service", "openswarm-lane-{lane}")
CURRENT_STATE_NOTE = ("current state of the host, observed now; it is not evidence about the "
                      "runs that already failed")


# --------------------------------------------------------------------------- helpers


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def read_text(path):
    """Read a file, replacing what cannot be decoded (kept explicit for python 3.9)."""
    try:
        with open(path, encoding="utf-8", errors="replace") as stream:
            return stream.read()
    except OSError:
        return ""


def now_iso():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def file_sha256(path):
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as stream:
            for chunk in iter(lambda: stream.read(65536), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def openswarm_homes(explicit=None):
    """The homes that may hold the lane's lanes.json, runs and state.

    The candidate runtime exports `OPENSWARM_HOME=$HOME/.claude/openswarm-executor-candidate`
    (`openswarm/bin/openswarm-fleet`), the default runtime uses `$HOME/.claude/openswarm`.
    """
    homes = []
    for value in (explicit, os.environ.get("OPENSWARM_HOME"),
                  os.path.join(Path.home(), ".claude", "openswarm-executor-candidate"),
                  os.path.join(Path.home(), ".claude", "openswarm")):
        if not value:
            continue
        path = Path(value).expanduser()
        if path not in homes:
            homes.append(path)
    return homes


def lanes_file(home, explicit=None):
    value = explicit or os.environ.get("OPENSWARM_LANES_FILE")
    return Path(value).expanduser() if value else home / "lanes.json"


def load_lane(path, name):
    """The lane's entry in lanes.json."""
    config = read_json(path)
    if not isinstance(config, dict) or not isinstance(config.get("lanes"), list):
        return None, f"invalid lane config {path}: expected {{\"lanes\":[...]}}"
    for lane in config["lanes"]:
        if isinstance(lane, dict) and lane.get("name") == name:
            return lane, None
    names = sorted(str(lane.get("name")) for lane in config["lanes"] if isinstance(lane, dict))
    return None, f"lane {name!r} is not in {path} (lanes: {', '.join(names) or 'none'})"


def lane_model_provider(lane, home):
    """The model and provider the lane's child starts with, and where each value comes from.

    A pi lane may leave them out of lanes.json; the policy then supplies them, exactly like
    `lane_config` does.  A model that does not match the broker's model is a spawn precondition
    failure, so the source of each value is reported.
    """
    policy = {}
    for candidate in (home / "policy.local.json", REPO_ROOT / "openswarm" / "policy.json"):
        data = read_json(candidate)
        if isinstance(data, dict) and isinstance(data.get("pi"), dict):
            policy = {**policy, **data["pi"]}
    provider = lane.get("provider") or policy.get("provider") or "minimax-cn"
    model = lane.get("model") or policy.get("default_model") or DEFAULT_MODEL
    return provider, model, {"provider": "lanes.json" if lane.get("provider") else "policy",
                              "model": "lanes.json" if lane.get("model") else "policy"}


# --------------------------------------------------------------------------- supervisor


def process_cmdline(pid, proc_root=PROC_ROOT):
    try:
        raw = (proc_root / str(pid) / "cmdline").read_bytes()
    except OSError:
        return []
    return [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]


def supervisor_pid(lane, proc_root=PROC_ROOT):
    """The pid of the running `openswarm lane serve <lane>` supervisor, if there is one."""
    for entry in sorted(proc_root.iterdir(), key=lambda item: item.name):
        if not entry.name.isdigit():
            continue
        argv = process_cmdline(entry.name, proc_root)
        for index, token in enumerate(argv):
            if token == "serve" and index and argv[index - 1] == "lane" and \
                    index + 1 < len(argv) and argv[index + 1] == lane:
                return int(entry.name)
    return None


def process_environment(pid, proc_root=PROC_ROOT, names=()):
    """The environment of a process, as the kernel hands it to it.

    Only the requested names are returned: the full environment holds secrets.
    """
    try:
        raw = (proc_root / str(pid) / "environ").read_bytes()
    except OSError:
        return {}
    env = {}
    for part in raw.split(b"\0"):
        if not part or b"=" not in part:
            continue
        key, value = part.split(b"=", 1)
        key = key.decode("utf-8", "replace")
        if not names or key in names:
            env[key] = value.decode("utf-8", "replace")
    return env


def supervisor_wrapper(pid, proc_root=PROC_ROOT):
    """The `pi-isolated` next to the supervisor's own openswarm binary.

    A pi lane always starts the wrapper that sits next to the running `openswarm`
    (`openswarm/bin/openswarm`, `lane_config`/`LaneChild`), so this is the wrapper the failing
    lane really used - not the one in the current checkout.
    """
    argv = process_cmdline(pid, proc_root)
    for token in argv:
        if token.endswith("/openswarm") or token == "openswarm":
            candidate = Path(token).parent / "pi-isolated"
            if candidate.is_file():
                return candidate
    return None


def wrapper_info(path):
    """Identity of a wrapper: which build it is (sha256) and whether it knows `--preflight`."""
    if not path:
        return None
    path = Path(path)
    info = {"path": str(path), "exists": path.is_file()}
    if not info["exists"]:
        return info
    info["sha256"] = file_sha256(path)
    try:
        info["mtime"] = dt.datetime.fromtimestamp(path.stat().st_mtime, dt.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
    except OSError:
        info["mtime"] = None
    try:
        info["supports_preflight"] = "--preflight" in read_text(path)
    except OSError:
        info["supports_preflight"] = None
    return info


def systemd_environment(lane):
    """The environment the lane's systemd unit hands to the supervisor."""
    for name in UNIT_NAMES:
        unit = name.format(lane=lane)
        try:
            result = subprocess.run(["systemctl", "--user", "show", "-p", "Environment", "--value", unit],
                                    capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            result = None
        if result and result.returncode == 0 and result.stdout.strip():
            return parse_environment(result.stdout), f"systemd --user unit {unit}"
    return None, None


def parse_environment(text):
    """Parse a systemd `Environment=` line into a dict."""
    env = {}
    for chunk in text.split():
        if "=" not in chunk:
            continue
        key, value = chunk.split("=", 1)
        env[key.strip("\"'")] = value.strip("\"'")
    return env


def parse_env_pairs(pairs):
    """Parse `KEY=VALUE` arguments into a dict."""
    env = {}
    for pair in pairs or []:
        if "=" not in pair:
            continue
        key, value = pair.split("=", 1)
        env[key] = value
    return env


def parse_env_file(path):
    """Parse a file with `KEY=VALUE` lines (e.g. the supervisor's /proc/<pid>/environ dump)."""
    env = {}
    try:
        lines = read_text(path).splitlines()
    except OSError:
        return env
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip()
    return env


# --------------------------------------------------------------------------- guards


def broker_socket_live(path, timeout=1.0):
    """Whether a unix socket still accepts connections (a stale socket file does not)."""
    probe = socket.socket(socket.AF_UNIX)
    try:
        probe.settimeout(timeout)
        probe.connect(path)
        return True
    except OSError:
        return False
    finally:
        probe.close()


def worktree_of(cwd):
    """The dedicated worktree the wrapper would start pi in."""
    try:
        result = subprocess.run(["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
                                capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return str(cwd)
    if result.returncode == 0 and result.stdout.strip():
        return str(Path(result.stdout.strip()).resolve())
    return str(cwd)


def guard_mirror(env, cwd, provider, model, isolated_model=DEFAULT_MODEL):
    """The wrapper's spawn guards, evaluated locally.

    This mirrors `pi-isolated`'s checks for a wrapper that predates `--preflight`, so an old
    release does not stop the investigation.  The verdict is about the current state only.
    """
    runtime_value = (env.get("OPENSWARM_PI_RUNTIME") or "").strip()
    broker_value = (env.get("OPENSWARM_PI_BROKER_SOCKET") or "").strip()
    sandbox_value = (env.get("OPENSWARM_PI_SANDBOX_TMP") or "").strip()
    pi_binary = os.path.join(runtime_value, "node_modules/.bin/pi") if runtime_value else ""
    path = env.get("PATH")
    bwrap, socat = shutil.which("bwrap", path=path), shutil.which("socat", path=path)
    worktree = worktree_of(cwd)
    findings = {
        "provider": provider, "model": model, "expected_model": isolated_model, "tools": ISOLATED_TOOLS,
        "mode": "rpc",
        "runtime": {"env": "OPENSWARM_PI_RUNTIME", "path": runtime_value or None,
                    "pi_binary": pi_binary if os.path.isfile(pi_binary) else None,
                    "ok": bool(runtime_value) and os.path.isfile(pi_binary)},
        "sandbox": {"bwrap": bwrap, "socat": socat, "ok": bool(bwrap and socat)},
        "broker": {"env": "OPENSWARM_PI_BROKER_SOCKET", "path": broker_value or None,
                   "exists": False, "is_socket": False, "live": None},
        "worktree": {"path": worktree, "ok": worktree != "/" and os.path.isdir(worktree)},
        "sandbox_tmp": {"env": "OPENSWARM_PI_SANDBOX_TMP", "path": sandbox_value or None, "ok": True},
    }
    if provider != "minimax-cn" or model != isolated_model:
        findings["guard"] = "configuration"
        findings["error"] = (f"isolated Pi rejected provider, model or tool configuration: "
                             f"provider={provider} model={model} expected_model={isolated_model}")
        return findings
    if not findings["sandbox"]["ok"] or not findings["runtime"]["ok"]:
        findings["guard"] = "dependencies"
        findings["error"] = (f"isolated Pi dependencies unavailable: bwrap={bwrap or 'missing'} "
                             f"socat={socat or 'missing'} "
                             f"pi_runtime={runtime_value or 'OPENSWARM_PI_RUNTIME unset'}")
        return findings
    if not broker_value:
        findings["guard"] = "broker_socket"
        findings["error"] = ("isolated Pi broker socket unavailable: OPENSWARM_PI_BROKER_SOCKET is not "
                             "set (the host broker socket must be named explicitly)")
        return findings
    try:
        findings["broker"]["exists"] = True
        findings["broker"]["is_socket"] = stat.S_ISSOCK(os.stat(broker_value).st_mode)
    except OSError:
        pass
    if not findings["broker"]["is_socket"]:
        findings["guard"] = "broker_socket"
        findings["error"] = (f"isolated Pi broker socket unavailable: {broker_value} is not a unix socket")
        return findings
    findings["broker"]["live"] = broker_socket_live(broker_value)
    if not findings["broker"]["live"]:
        findings["guard"] = "broker_socket"
        findings["error"] = (f"isolated Pi broker socket is not live: {broker_value} (the socket file "
                             "exists but nothing is listening on it)")
        return findings
    if not findings["worktree"]["ok"]:
        findings["guard"] = "worktree"
        findings["error"] = f"isolated Pi requires a dedicated worktree: {worktree}"
        return findings
    if sandbox_value and (not os.path.isabs(sandbox_value) or os.path.realpath(sandbox_value) == "/"):
        findings["guard"] = "sandbox_tmp"
        findings["error"] = "OPENSWARM_PI_SANDBOX_TMP must be an absolute path below /"
        return findings
    findings["guard"] = None
    findings["error"] = None
    return findings


def wrapper_preflight(wrapper, env, cwd, provider, model):
    """Run the wrapper's own `--preflight`, with the supervisor's environment."""
    command = [str(wrapper), "--preflight", "--mode", "rpc", "--provider", str(provider),
               "--model", str(model)]
    try:
        result = subprocess.run(command, cwd=str(cwd), env=env, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as error:
        return None, f"cannot run {wrapper}: {error}"
    try:
        return json.loads(result.stdout), None
    except ValueError:
        return None, (f"{wrapper} did not print a preflight report "
                      f"(exit {result.returncode}): {result.stderr.strip() or result.stdout.strip()}")


# --------------------------------------------------------------------------- run records


def run_evidence(run_dir):
    """The failed run's own record: error class, error tail, timings and the last events."""
    out = {"run_dir": str(run_dir), "result": None, "status": None, "events": []}
    result = read_json(Path(run_dir) / "result.json")
    if isinstance(result, dict):
        attempts = result.get("attempts") or []
        out["result"] = {"status": result.get("status"), "error_class": result.get("error_class"),
                         "commit": result.get("commit"), "seconds": result.get("seconds"),
                         "failure_reason": result.get("failure_reason"),
                         "attempts": [{"error_class": a.get("error_class"), "error_tail": a.get("error_tail"),
                                       "seconds": a.get("seconds"), "events": a.get("events"),
                                       "spawn": a.get("spawn")}
                                      for a in attempts if isinstance(a, dict)]}
    status = read_json(Path(run_dir) / "status.json")
    if isinstance(status, dict):
        out["status"] = {key: status.get(key) for key in
                         ("state", "status", "error_class", "last_error", "timings", "executor_exit_code",
                          "started", "finished", "lane", "backend", "model", "spawn") if key in status}
    for event_file in sorted(Path(run_dir).glob("events*.jsonl")):
        try:
            lines = read_text(event_file).splitlines()
        except OSError:
            continue
        records = []
        for line in lines[-40:]:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict):
                records.append({key: record.get(key) for key in
                                ("type", "error", "guard", "status", "lane", "last_activity_at")
                                if key in record})
        out["events"].append({"file": event_file.name, "records": records})
    return out


def lane_runs(home, lane, limit=25):
    """The lane's own run records, oldest first.

    These are the historical evidence: what the failed runs recorded about themselves.
    """
    runs_dir = Path(home) / "runs"
    if not runs_dir.is_dir():
        return []
    out = []
    for run_dir in sorted(runs_dir.iterdir()):
        result = read_json(run_dir / "result.json")
        if not isinstance(result, dict) or result.get("lane") != lane:
            continue
        status = read_json(run_dir / "status.json") or {}
        attempts = result.get("attempts") or []
        error_tail = next((a.get("error_tail") for a in reversed(attempts)
                           if isinstance(a, dict) and a.get("error_tail")), None)
        out.append({"run_id": run_dir.name, "status": result.get("status"),
                    "error_class": result.get("error_class"), "error_tail": error_tail,
                    "failure_reason": result.get("failure_reason"), "commit": result.get("commit"),
                    "started": status.get("started"), "finished": status.get("finished"),
                    "seconds": result.get("seconds"), "timings": result.get("timings") or status.get("timings"),
                    "spawn": status.get("spawn")})
    out.sort(key=lambda run: (run.get("started") or 0, run["run_id"]))
    return out[-limit:]


def historical_verdict(runs):
    """What the run records themselves say about the incident."""
    if not runs:
        return {"verdict": "no run records found for this lane", "basis": []}
    exec_errors = [run for run in runs if run.get("error_class") == "exec_error"]
    failed = [run for run in runs if run.get("status") == "failed"]
    basis = []
    tails = sorted({run.get("error_tail") for run in exec_errors if run.get("error_tail")})
    durations = sorted({round(float(run["seconds"]), 1) for run in failed
                        if isinstance(run.get("seconds"), (int, float))})
    basis.append(f"{len(exec_errors)} run(s) recorded error_class=exec_error, "
                 f"{len(failed)} run(s) ended as failed")
    if tails:
        basis.append("recorded error_tail: " + " | ".join(tails))
    else:
        basis.append("no error_tail was recorded: the lane runner dropped the executor's stderr "
                     "(stderr=subprocess.DEVNULL), so the agent-side error string of these runs "
                     "was never written down and cannot be recovered from the host")
    if durations:
        basis.append("run durations (s): " + ", ".join(str(value) for value in durations))
    return {"verdict": "the failed runs left only their own bookkeeping", "basis": basis}


# --------------------------------------------------------------------------- report


def build_report(lane_name, home, lanes_path, lane, provider, model, wrapper, env, env_source,
                 findings, findings_source, runs, run_evidence_report):
    problems = []
    if lane is None:
        problems.append(f"lane {lane_name!r} is not configured in {lanes_path}")
    summary = historical_verdict(runs)
    report = {
        "observed_at": now_iso(),
        "lane": {"name": lane_name, "backend": (lane or {}).get("backend"),
                 "model": model, "provider": provider},
        "scope": {"kind": "current-state", "note": CURRENT_STATE_NOTE},
        "environment": {"source": env_source,
                        "values": {name: env.get(name) for name in PI_ENV_VARS if env.get(name)}},
        "wrapper": wrapper,
        "preflight": {"source": findings_source, "findings": findings},
        "historical": {"runs": runs, "verdict": summary["verdict"], "basis": summary["basis"]},
        "run": run_evidence_report,
        "problems": problems,
    }
    if findings and findings.get("error"):
        report["current_verdict"] = f"spawn precondition failed now: {findings.get('guard')}"
    elif findings:
        report["current_verdict"] = "spawn preconditions hold now"
    else:
        report["current_verdict"] = "the spawn preconditions could not be evaluated"
    note = CURRENT_STATE_NOTE
    if wrapper and wrapper.get("source") and "not the supervisor" in str(wrapper["source"]):
        note = f"{note}; the wrapper is {wrapper['source']}"
    report["current_verdict"] = f"{report['current_verdict']} ({note})"
    return report


def render(report):
    lines = []
    lane = report.get("lane") or {}
    lines.append(f"lane        {lane.get('name')}  backend={lane.get('backend')} "
                 f"model={lane.get('model')} provider={lane.get('provider')}")
    environment = report.get("environment") or {}
    lines.append(f"env source  {environment.get('source')}")
    for name, value in sorted((environment.get("values") or {}).items()):
        lines.append(f"env         {name}={value}")
    wrapper = report.get("wrapper") or {}
    lines.append(f"wrapper     {wrapper.get('path')}  sha256={wrapper.get('sha256')} "
                 f"mtime={wrapper.get('mtime')} preflight={wrapper.get('supports_preflight')}")
    preflight = report.get("preflight") or {}
    findings = preflight.get("findings") or {}
    lines.append(f"preflight   {preflight.get('source')}")
    if findings:
        broker = findings.get("broker") or {}
        runtime = findings.get("runtime") or {}
        sandbox = findings.get("sandbox") or {}
        lines.append(f"guard       {findings.get('guard') or 'none'}")
        lines.append(f"error       {findings.get('error') or 'none'}")
        lines.append(f"broker      {broker.get('path')}  exists={broker.get('exists')} "
                     f"is_socket={broker.get('is_socket')} live={broker.get('live')}")
        lines.append(f"pi runtime  {runtime.get('path')}  pi_binary={runtime.get('pi_binary')}")
        lines.append(f"sandbox     bwrap={sandbox.get('bwrap')} socat={sandbox.get('socat')}")
    lines.append(f"current     {report.get('current_verdict')}")
    historical = report.get("historical") or {}
    lines.append(f"historical  {historical.get('verdict')}")
    for basis in historical.get("basis") or []:
        lines.append(f"  -         {basis}")
    for run in historical.get("runs") or []:
        lines.append(f"run         {run.get('run_id')}  status={run.get('status')} "
                     f"error_class={run.get('error_class')} seconds={run.get('seconds')} "
                     f"commit={run.get('commit')}")
        if run.get("error_tail"):
            lines.append(f"  error_tail {run.get('error_tail')}")
    evidence = report.get("run")
    if evidence:
        result = evidence.get("result") or {}
        lines.append(f"run dir     {evidence.get('run_dir')}")
        lines.append(f"run status  {result.get('status')}  error_class={result.get('error_class')} "
                     f"commit={result.get('commit')}")
        for attempt in result.get("attempts") or []:
            lines.append(f"attempt     error_class={attempt.get('error_class')} "
                         f"seconds={attempt.get('seconds')}")
            if attempt.get("error_tail"):
                lines.append(f"error_tail  {attempt.get('error_tail')}")
            if attempt.get("spawn"):
                lines.append(f"spawn       {json.dumps(attempt['spawn'], ensure_ascii=False)}")
        status = evidence.get("status") or {}
        if status:
            lines.append(f"timings     {status.get('timings')}")
            lines.append(f"last_error  {status.get('last_error')}")
            if status.get("spawn"):
                lines.append(f"spawn       {json.dumps(status['spawn'], ensure_ascii=False)}")
        for event in evidence.get("events") or []:
            for record in event.get("records") or []:
                if record.get("type") in ("error", "stalled") or record.get("error"):
                    lines.append(f"event       {record.get('type')}: {record.get('error')}")
    for problem in report.get("problems") or []:
        lines.append(f"problem     {problem}")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--lane", help="the lane name, e.g. M9")
    parser.add_argument("--home", help="the openswarm home (default: $OPENSWARM_HOME)")
    parser.add_argument("--lanes-file", help="the lanes.json to read (default: $OPENSWARM_HOME/lanes.json)")
    parser.add_argument("--run-id", help="a run id below $OPENSWARM_HOME/runs")
    parser.add_argument("--run-dir", help="a run directory to read")
    parser.add_argument("--wrapper", help="the pi-isolated wrapper to evaluate")
    parser.add_argument("--supervisor-pid", type=int, help="the lane supervisor's pid (default: detected)")
    parser.add_argument("--env", action="append", metavar="KEY=VALUE",
                        help="the supervisor's environment (repeatable); wins over the detected one")
    parser.add_argument("--env-file", help="a file with the supervisor's KEY=VALUE lines")
    parser.add_argument("--runs", type=int, default=25, help="how many of the lane's runs to list")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args()
    if not args.lane:
        parser.error("--lane is required")

    problems = []
    home = None
    lane = None
    lanes_path = None
    for candidate in openswarm_homes(args.home):
        path = lanes_file(candidate, args.lanes_file)
        found, problem = load_lane(path, args.lane)
        if found is not None:
            home, lane, lanes_path = candidate, found, path
            break
        problems.append(problem)
    if home is None:
        home = openswarm_homes(args.home)[0]
        lanes_path = lanes_file(home, args.lanes_file)
    provider, model, sources = lane_model_provider(lane or {}, home)

    pid = args.supervisor_pid or supervisor_pid(args.lane)
    env, env_source = {}, "not found"
    env_verified = False
    if args.env_file:
        env = parse_env_file(args.env_file)
        env_source = f"the environment file {args.env_file} (unverified snapshot)"
    elif args.env:
        env = parse_env_pairs(args.env)
        env_source = "the environment given on the command line (unverified snapshot)"
    elif pid:
        # The whole environment of the supervisor: the wrapper sees exactly this one.
        env = process_environment(pid)
        env_verified = bool(env)
        env_source = f"supervisor process {pid} (/proc/{pid}/environ)"
        if not any(env.get(name) for name in PI_ENV_VARS):
            env_source += " (no OPENSWARM_PI_* variables in it)"
    else:
        unit_env, unit_source = systemd_environment(args.lane)
        if unit_env:
            env = unit_env
            env_source = f"{unit_source} (unverified snapshot)"
        else:
            env_source = "not found (no running supervisor and no systemd unit environment)"

    wrapper_path = Path(args.wrapper).expanduser() if args.wrapper else None
    wrapper_source = "--wrapper"
    if wrapper_path is None and pid:
        wrapper_path = supervisor_wrapper(pid)
        wrapper_source = "the supervisor's own openswarm binary"
    if wrapper_path is None:
        wrapper_path = (home / "bin" / "pi-isolated")
        wrapper_source = "the openswarm home (not the supervisor's wrapper)"
        if not wrapper_path.is_file():
            wrapper_path = REPO_ROOT / "openswarm" / "bin" / "pi-isolated"
            wrapper_source = "this checkout (not the supervisor's wrapper)"
    wrapper = wrapper_info(wrapper_path)
    wrapper["source"] = wrapper_source

    worktree = home / "lanes" / args.lane / "wt"
    if not worktree.is_dir():
        worktree = Path.cwd()
    findings, findings_source = None, None
    if not env_verified:
        # The shell environment is not the lane's environment: evaluating the guards with it
        # would report a fault the lane never had.
        findings_source = "not evaluated (the supervisor's environment is unknown)"
        problems.append("the supervisor's complete environment is unavailable or unverified; the spawn preconditions "
                        "were not evaluated, because the shell environment is not the lane's")
    elif wrapper.get("supports_preflight"):
        findings, error = wrapper_preflight(wrapper_path, env, worktree, provider, model)
        if error:
            problems.append(error)
            findings, findings_source = None, "the wrapper's --preflight failed"
        else:
            findings_source = "the wrapper's own --preflight"
    elif wrapper.get("exists"):
        findings = guard_mirror(env, worktree, provider, model)
        findings_source = (f"local guard mirror (the deployed wrapper {wrapper.get('sha256')} "
                           f"predates --preflight)")
    else:
        problems.append(f"no pi-isolated wrapper found at {wrapper_path}; pass --wrapper")

    runs = lane_runs(home, args.lane, limit=max(1, args.runs))
    run_dir = None
    if args.run_dir:
        run_dir = Path(args.run_dir).expanduser()
    elif args.run_id:
        run_dir = home / "runs" / args.run_id
    run_report = run_evidence(run_dir) if run_dir else None
    if run_report and not (run_dir / "result.json").exists():
        problems.append(f"no result.json in {run_dir}")

    report = build_report(args.lane, home, lanes_path, lane, provider, model, wrapper, env, env_source,
                          findings, findings_source, runs, run_report)
    report["problems"] = problems + report.get("problems", [])
    report["model_source"] = sources
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(render(report))
    return 1 if report["problems"] else 0


if __name__ == "__main__":
    sys.exit(main())
