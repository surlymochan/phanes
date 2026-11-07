#!/usr/bin/env python3
"""Offline contract check for the harvest push of the AD (Refs #1640).

A push that failed without any error output (empty stderr) but whose branch is
already on origin means the update reached origin before the failure was reported:
the harvest goes on to the PR instead of pushing again on every tick.  Every
scenario below is fake (no network, no git): the git calls are replaced by canned
answers, so the check runs offline on any host.
"""
import importlib.util
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "tools/lane-autodispatch/autodispatch.py"
HEAD = "a" * 40
OTHER_HEAD = "b" * 40
BRANCH = "fleet/lane-issue1-run"


def load_module():
    spec = importlib.util.spec_from_file_location("autodispatch_push_empty_err", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fake_git(push_rc=0, push_err="", remote_head=HEAD, pushed_head=HEAD):
    """Return a `sh` replacement answering the push, the ls-remote and the rev-parse."""
    def sh(cmd, timeout=120, inp=None):
        argv = cmd if isinstance(cmd, list) else [cmd]
        if "push" in argv:
            return push_rc, "", push_err
        if "ls-remote" in argv:
            return (0, f"{remote_head}\trefs/heads/{argv[-1]}\n", "") if remote_head else (0, "", "")
        if "rev-parse" in argv:
            return (0, f"{pushed_head}\n", "") if pushed_head else (1, "", "")
        return 0, "", ""
    return sh


def run_finish(ad, sh_impl):
    """Run the harvest of a finished run against the fake git and return the result."""
    st = {"issue": 1, "run": "run", "base": "base"}
    info = {"branch": "branch", "commit": "commit", "repo": "repo"}
    with mock.patch.object(ad, "sh", sh_impl), \
         mock.patch.object(ad, "harvest_fetch", return_value=(True, "", 0)), \
         mock.patch.object(ad, "filter_harvest", return_value=(HEAD, [], False)), \
         mock.patch.object(ad, "pr_validation_mode", return_value="warn"), \
         mock.patch.object(ad, "log"), \
         mock.patch.object(ad, "gh", return_value=(0, "", "")) as gh:
        result = ad.finish("lane", {"host": "host"}, st, info, {})
    return result, gh


def main():
    ad = load_module()

    # 1. the push fails without any error output and the branch is on origin at the
    #    pushed head: the update landed, the harvest continues to the PR.
    with mock.patch.object(ad, "sh", fake_git(push_rc=1, push_err="")), \
         mock.patch.object(ad, "log"):
        ok, error = ad.harvest_push("lane", 1, BRANCH, HEAD)
    assert ok and error is None, (ok, error)

    # 2. the push fails without any error output and the branch is not on origin:
    #    the push really failed, the harvest must not open a PR.
    with mock.patch.object(ad, "sh", fake_git(push_rc=1, push_err="", remote_head="")), \
         mock.patch.object(ad, "log"):
        ok, error = ad.harvest_push("lane", 1, BRANCH, HEAD)
    assert not ok and "not on origin" in error, (ok, error)

    # 3. the push failed with a reason: the reason is reported, nothing is guessed.
    with mock.patch.object(ad, "sh", fake_git(push_rc=1, push_err="! [rejected] non-fast-forward")), \
         mock.patch.object(ad, "log"):
        ok, error = ad.harvest_push("lane", 1, BRANCH, HEAD)
    assert not ok and "non-fast-forward" in error, (ok, error)

    # 4. the branch on origin carries another head: it is not the harvested commit.
    with mock.patch.object(ad, "sh", fake_git(push_rc=1, push_err="", remote_head=OTHER_HEAD)), \
         mock.patch.object(ad, "log"):
        ok, error = ad.harvest_push("lane", 1, BRANCH, HEAD)
    assert not ok and "not at the pushed head" in error, (ok, error)

    # 5. a plain successful push stays a success.
    with mock.patch.object(ad, "sh", fake_git(push_rc=0)), \
         mock.patch.object(ad, "log"):
        ok, error = ad.harvest_push("lane", 1, BRANCH, HEAD)
    assert ok and error is None, (ok, error)

    # 6. the whole harvest: the PR is opened although the push reported a failure
    #    without any error output and the branch is on origin.
    result, gh = run_finish(ad, fake_git(push_rc=1, push_err=""))
    assert result["status"] == "pr-created", result
    assert any("pr" in call.args[0][:2] for call in gh.call_args_list), gh.call_args_list

    # 7. the whole harvest: without the branch on origin the harvest stops at the push.
    result, gh = run_finish(ad, fake_git(push_rc=1, push_err="", remote_head=""))
    assert result["status"] == "push-failed", result
    assert not any("pr" in call.args[0][:2] for call in gh.call_args_list), gh.call_args_list

    print("ad push empty stderr: OK")


if __name__ == "__main__":
    main()
