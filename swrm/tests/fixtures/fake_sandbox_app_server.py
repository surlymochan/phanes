#!/usr/bin/env python3
"""Fake Codex app-server that emulates the sandbox mount policy of a lane (Refs #1832).

A real Codex ``workspace-write`` sandbox mounts a worktree's git metadata read-only: every write
outside the sandbox's writable roots fails with ``Read-only file system``. This fixture applies the
same rule to the lane's git admin dir, so a lane can only commit when the runtime handed that admin
dir to the sandbox as a writable root.

The writable roots arrive the same way the runtime passes them: as
``-c sandbox_workspace_write.writable_roots=[...]`` on the app-server command line and as the
``config`` of the ``thread/start`` request.

The prompt must contain ``SANDBOX_PROBE: <command>``; the fixture runs that command in the
thread's cwd and reports its output and exit code.
"""
import json
import os
import subprocess
import sys

PROBE_MARKER = "SANDBOX_PROBE:"


def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def response(req, result=None):
    send({"jsonrpc": "2.0", "id": req.get("id"), "result": result or {}})


def parse_config(argv):
    """``-c key=value`` overrides of the app-server command line."""
    config = {}
    for index, arg in enumerate(argv):
        if arg != "-c" or index + 1 >= len(argv):
            continue
        key, _, value = argv[index + 1].partition("=")
        try:
            config[key] = json.loads(value)
        except ValueError:
            config[key] = value
    return config


def roots_of(config):
    """The writable roots of a config mapping (absolute, symlink free)."""
    roots = (config or {}).get("sandbox_workspace_write.writable_roots")
    if isinstance(roots, str):
        try:
            roots = json.loads(roots)
        except ValueError:
            roots = []
    if not isinstance(roots, list):
        return []
    return [os.path.realpath(root) for root in roots if isinstance(root, str) and root]


def covered(path, roots):
    """Whether ``path`` lies inside one of the writable roots."""
    if not path:
        return False
    path = os.path.realpath(path)
    return any(path == root or path.startswith(root + os.sep) for root in roots)


def git_admin_dir(cwd):
    """The git admin dir of the checkout in ``cwd`` (``.git/worktrees/<name>`` for a worktree)."""
    try:
        proc = subprocess.run(["git", "rev-parse", "--absolute-git-dir"], cwd=cwd,
                              capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode:
        return None
    return os.path.realpath(proc.stdout.strip())


launch_config = parse_config(sys.argv[1:])
thread_id = "fake-sandbox-thread"
turn_id = "fake-sandbox-turn"
cwd = os.getcwd()
thread_config = {}

for line in sys.stdin:
    try:
        req = json.loads(line)
    except ValueError:
        continue
    method = req.get("method")
    params = req.get("params") or {}
    if method == "initialize":
        response(req, {"serverInfo": {"name": "fake-sandbox-app-server", "version": "1"}})
    elif method == "initialized":
        continue
    elif method == "thread/start":
        cwd = params.get("cwd", cwd)
        thread_config = params.get("config") or {}
        response(req, {"thread": {"id": thread_id}, "model": params.get("model")})
    elif method == "turn/start":
        prompt = (params.get("input") or [{}])[0].get("text", "")
        response(req, {"turn": {"id": turn_id, "status": "inProgress"}})
        command = prompt.split(PROBE_MARKER, 1)[1].strip() if PROBE_MARKER in prompt else ""
        roots = roots_of(launch_config) + roots_of(thread_config)
        admin = git_admin_dir(cwd)
        if admin and not covered(admin, roots):
            # The sandbox mounts the lane's git metadata read-only.
            output = ("fatal: could not write index: Read-only file system\n"
                      "hint: the sandbox writable roots do not contain {}".format(admin))
            exit_code = 1
        else:
            try:
                proc = subprocess.run(command, shell=True, cwd=cwd, capture_output=True, text=True, timeout=300)
                output = (proc.stdout or "") + (proc.stderr or "")
                exit_code = proc.returncode
            except Exception as error:  # noqa: BLE001
                output = str(error)
                exit_code = 1
        send({"jsonrpc": "2.0", "method": "item/started", "params": {
            "threadId": thread_id, "turnId": turn_id,
            "item": {"id": "sandbox-command", "type": "commandExecution", "command": command}}})
        send({"jsonrpc": "2.0", "method": "item/completed", "params": {
            "threadId": thread_id, "turnId": turn_id,
            "item": {"id": "sandbox-command", "type": "commandExecution", "command": command,
                     "exitCode": exit_code, "aggregatedOutput": output}}})
        send({"jsonrpc": "2.0", "method": "turn/completed", "params": {
            "threadId": thread_id, "turn": {"id": turn_id, "status": "completed"},
            "usage": {"inputTokens": 2}}})
