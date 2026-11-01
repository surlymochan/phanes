#!/usr/bin/env python3
"""Offline contract check for failed assign rollback.

The fixtures model the remote lane and the autodispatcher's local state.json.
No SSH, process signalling, or live control-plane state is used.
"""
from copy import deepcopy


REMOTE_RESERVED = {"busy", "queued"}


def is_assign_ghost(assign_rc, remote, local):
    """Return true when a failed assign left a remote-only reservation."""
    if assign_rc == 0 or remote.get("state") not in REMOTE_RESERVED:
        return False
    assignment = remote.get("current_run")
    if assignment is None:
        return False
    return local.get("assignments", {}).get(assignment) is None


def rollback_assign_ghost(assign_rc, remote, local):
    """Clear only a remote reservation with no local state entry.

    A busy lane with a live runner is deliberately left alone: rollback must
    never kill or otherwise disturb a real runner.
    """
    if not is_assign_ghost(assign_rc, remote, local):
        return False
    if remote.get("state") == "busy" and remote.get("runner_alive") is not False:
        return False
    remote["state"] = "idle"
    remote["current_run"] = None
    return True


def main():
    local = {"assignments": {}}

    queued = {"state": "queued", "current_run": "#968"}
    assert is_assign_ghost(1, queued, local)
    assert rollback_assign_ghost(1, queued, local)
    assert queued == {"state": "idle", "current_run": None}

    busy_without_runner = {
        "state": "busy",
        "current_run": "#968",
        "runner_alive": False,
    }
    assert is_assign_ghost(2, busy_without_runner, local)
    assert rollback_assign_ghost(2, busy_without_runner, local)
    assert busy_without_runner["state"] == "idle"
    assert busy_without_runner["current_run"] is None

    successful = {"state": "busy", "current_run": "#968", "runner_alive": True}
    successful_local = {"assignments": {"#968": {"lane": "cx1"}}}
    before_successful = deepcopy(successful)
    assert not is_assign_ghost(0, successful, successful_local)
    assert not rollback_assign_ghost(0, successful, successful_local)
    assert successful == before_successful

    live_remote = {"state": "busy", "current_run": "#968", "runner_alive": True}
    before_live = deepcopy(live_remote)
    assert is_assign_ghost(1, live_remote, local)
    assert not rollback_assign_ghost(1, live_remote, local)
    assert live_remote == before_live

    print("assign rollback ghost contract: OK")


if __name__ == "__main__":
    main()
