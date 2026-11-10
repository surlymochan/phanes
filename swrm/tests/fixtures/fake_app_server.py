#!/usr/bin/env python3
"""Small JSON-RPC app-server subset for the resident lane tests."""
import json
import os
import select
import sys
import time


def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def response(req, result=None):
    send({"jsonrpc": "2.0", "id": req.get("id"), "result": result or {}})


thread_id = "fake-thread"
turn_id = "fake-turn"
cwd = os.getcwd()
model = None
overload_model = os.environ.get("FAKE_APP_SERVER_OVERLOAD_MODEL")
overload_used = False
thread_start_error = os.environ.get("FAKE_APP_SERVER_THREAD_START_ERROR")
hung = False
marker = os.environ.get("FAKE_APP_SERVER_IGNORE_PROBE_ONCE_FILE")
ignore_probe = os.environ.get("FAKE_APP_SERVER_IGNORE_PROBE") == "1"
if marker and not os.path.exists(marker):
    with open(marker, "w") as stream:
        stream.write("first server ignores probes")
    ignore_probe = True


# CX9 replay: discovery works but the first process cannot create sessions.
session_failure = os.environ.get("FAKE_APP_SERVER_SESSION_FAILURE")
session_marker = os.environ.get("FAKE_APP_SERVER_SESSION_FAILURE_ONCE_FILE")
if session_marker:
    if os.path.exists(session_marker):
        session_failure = None
    else:
        with open(session_marker, "w") as stream:
            stream.write("first server cannot create sessions")


def probe_response(req):
    if not hung and not ignore_probe:
        response(req, {"data": []})


for line in sys.stdin:
    try:
        req = json.loads(line)
    except ValueError:
        continue
    if os.environ.get("FAKE_APP_SERVER_LOG"):
        with open(os.environ["FAKE_APP_SERVER_LOG"], "a") as stream:
            stream.write(json.dumps(req) + "\n")
    method = req.get("method")
    params = req.get("params") or {}
    if method == "model/list":
        probe_response(req)
    elif method == "initialize":
        send({"jsonrpc": "2.0", "method": "fixture/unrelated", "params": {"before": method}})
        response(req, {"serverInfo": {"name": "fake-app-server", "version": "1"}})
    elif method == "initialized":
        continue
    elif method in ("thread/start", "thread/resume"):
        if session_failure and method == "thread/start":
            if session_failure == "error":
                send({"jsonrpc": "2.0", "id": req.get("id"),
                      "error": {"message": "fixture cannot create sessions"}})
            continue
        if str(req.get("id", "")).startswith("health-session-"):
            response(req, {"thread": {"id": "health-thread"}})
            continue
        cwd = params.get("cwd", cwd)
        model = params.get("model")
        send({"jsonrpc": "2.0", "method": "fixture/unrelated", "params": {"before": method}})
        if thread_start_error:
            send({"jsonrpc": "2.0", "id": req.get("id"), "error": {"message": thread_start_error}})
        else:
            response(req, {"thread": {"id": thread_id}, "model": params.get("model")})
    elif method == "turn/start":
        prompt = (params.get("input") or [{}])[0].get("text", "")
        if "LANE_TEST_CRASH" in prompt:
            os._exit(17)
        send({"jsonrpc": "2.0", "method": "fixture/unrelated", "params": {"before": method}})
        response(req, {"turn": {"id": turn_id, "status": "inProgress"}})
        if "LANE_TEST_HANG" in prompt:
            hung = True
            continue
        if os.environ.get("FAKE_APP_SERVER_FAIL_TURNS") == "1":
            send({"jsonrpc": "2.0", "method": "turn/failed", "params": {
                "threadId": thread_id, "turn": {"id": turn_id},
                "error": {"message": "fixture rejected validation turn"}}})
            continue
        if "LANE_TEST_IO_LOST" in prompt:
            for seq in range(3):
                send({"jsonrpc": "2.0", "method": "item/completed", "params": {
                    "threadId": thread_id, "turnId": turn_id,
                    "item": {"id": f"empty-cmd-{seq}", "type": "commandExecution",
                             "exitCode": 0, "aggregatedOutput": None, "durationMs": 0}}})
            time.sleep(1.0)
            continue
        if overload_model and overload_model == model and not overload_used:
            overload_used = True
            with open(os.path.join(cwd, "lane_partial.txt"), "w") as f:
                f.write("kept after overload")
            send({"jsonrpc": "2.0", "method": "turn/failed", "params": {
                "threadId": thread_id, "turn": {"id": turn_id},
                "error": {"message": "Selected model is at capacity",
                          "data": {"codexErrorInfo": "serverOverloaded"}}}})
            continue
        send({"jsonrpc": "2.0", "method": "item/started", "params": {
            "threadId": thread_id, "turnId": turn_id,
            "item": {"id": "fake-command", "type": "commandExecution",
                     "command": "git push secret-value" if "LANE_TEST_RISK" in prompt else "python fake task"}}})
        steered = ""
        aborted = False
        wait_until = time.time() + (20 if "LANE_TEST_WAIT" in prompt else 0.25)
        while time.time() < wait_until:
            ready, _, _ = select.select([sys.stdin], [], [], 0.1)
            if not ready:
                continue
            control_line = sys.stdin.readline()
            if not control_line:
                os._exit(0)
            control = json.loads(control_line)
            if control.get("method") == "model/list":
                probe_response(control)
                continue
            if (control.get("method") == "thread/start"
                    and str(control.get("id", "")).startswith("health-session-")):
                response(control, {"thread": {"id": "health-thread"}})
                continue
            if control.get("method") == "turn/steer":
                steered = (control.get("params", {}).get("input") or [{}])[0].get("text", "")
                response(control)
                break
            if control.get("method") == "turn/interrupt":
                aborted = True
                response(control)
                break
        if aborted:
            send({"jsonrpc": "2.0", "method": "turn/completed", "params": {
                "threadId": thread_id, "turn": {"id": turn_id, "status": "interrupted"}}})
            continue
        if "LANE_TEST_NO_CHANGE" not in prompt:
            with open(os.path.join(cwd, "lane_result.txt"), "w") as f:
                f.write(steered or "completed")
        result = {"status": "done", "summary": "fake app-server completed", "files_changed": ["lane_result.txt"],
                  "tests": "fake executor", "followups": []}
        send({"jsonrpc": "2.0", "method": "item/completed", "params": {
            "threadId": thread_id, "turnId": turn_id, "completedAtMs": int(time.time() * 1000),
            "item": {"id": "fake-message", "type": "agentMessage", "text": json.dumps(result)}}})
        send({"jsonrpc": "2.0", "method": "turn/completed", "params": {
            "threadId": thread_id, "turn": {"id": turn_id, "status": "completed", "usage": {"inputTokens": 2}}}})
