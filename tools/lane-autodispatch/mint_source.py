#!/usr/bin/env python3
"""Phanes seed-mint/planner as an openswarm dispatch source hook (Refs #1371).

The openswarm dispatcher can ask external commands for extra task sources
(``sources.hooks`` in the dispatch config, protocol:
``openswarm/lib/dispatch/source_hooks.py``).  This command is the phanes side of
that interface: it tops the phanes card pool up when it runs dry and prints
every card it minted as one protocol task line::

    {"issue": 501, "priority": "p2", "backend": "pi", "reason": "phanes seed-mint"}

The card refill (补卡) itself is the one of the autodispatcher
(``tools/lane-autodispatch/autodispatch.py``): local backlog cards first, then
the planner batch of the phanes repository, and finally one planner issue that
asks a lane to write the next batch.  Only this hook knows the protocol; the
openswarm core keeps no phanes specific code.

The hook never works while the operator pause is up: ``PAUSE-MINT`` (or
``NO_SEED_MINT``) in the phanes state directory stops the mint, so the hook
files nothing and prints nothing.  The dispatcher applies the same rule before
it starts the hook at all -- the hook is configured with ``pause_scope:
"mint"`` -- so a paused pool never grows from either side.

Wiring (dispatch config, phanes side)::

    "sources": {
      "hooks": [
        {"name": "phanes-mint",
         "argv": ["python3", "/workspace/phanes/tools/lane-autodispatch/mint_source.py"],
         "pause_scope": "mint"}
      ],
      "timeout_s": 60
    }

Usage::

    python3 tools/lane-autodispatch/mint_source.py --state-dir ~/phanes-ctl/autodispatch \
        --repo surlymochan/phanes --target 20
"""
import argparse
import fcntl
import json
import os
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import autodispatch as ad  # noqa: E402  the card refill (补卡) lives in the autodispatcher

# The original shell of the autodispatcher; the hook replaces ad.sh with a
# wrapper around it (see make_sh).
AD_SH = ad.sh

PAUSE_MARKERS = ("PAUSE-MINT", "NO_SEED_MINT")
PRIORITIES = ("p0", "p1", "p2")
REASON = "phanes seed-mint"
ISSUE_REF = re.compile(r"#(\d+)")
DEFAULT_STATE_DIR = os.path.join(os.path.expanduser("~"), "phanes-ctl", "autodispatch")
DEFAULT_REPO = "surlymochan/phanes"
DEFAULT_REPO_PATH = "/workspace/phanes-repo"
DEFAULT_TARGET = 20


def paused(state_dir):
    """Whether the operator pause stops new cards (PAUSE-MINT / NO_SEED_MINT)."""
    return any(os.path.exists(os.path.join(state_dir, marker)) for marker in PAUSE_MARKERS)


def parse_ref(ref):
    """The issue number of a filed card ("#501" or "#502(planner)")."""
    match = ISSUE_REF.search(str(ref or ""))
    return int(match.group(1)) if match else None


def task(number, labels, reason=REASON):
    """One protocol task line: issue, priority, backend and reason."""
    names = {name for name in (labels or ()) if isinstance(name, str)}
    priority = next((prio for prio in PRIORITIES if prio in names), None)
    backend = next((name.split(":", 1)[1] for name in names if name.startswith("backend:")), None)
    return {"issue": number, "priority": priority, "backend": backend, "reason": reason}


def state_path(state_dir):
    """Share the autodispatcher import history."""
    return os.path.join(state_dir, "state.json")


def load_state(state_dir):
    """The imported planner batch paths of the earlier runs."""
    try:
        with open(state_path(state_dir), encoding="utf-8") as stream:
            state = json.load(stream)
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def save_state(state_dir, state):
    """Merge import history without overwriting the dispatcher's lane state."""
    with open(os.path.join(state_dir, "state.lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        current = load_state(state_dir)
        current["_imported"] = list(dict.fromkeys(current.get("_imported", []) + state.get("_imported", [])))
        path = state_path(state_dir)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as stream:
            json.dump(current, stream, indent=1, ensure_ascii=False)
        os.replace(tmp, path)


def log_to_stderr(message, brain=True):
    """The autodispatcher logger, redirected to stderr.

    stdout carries the protocol lines only: a log line there would be counted
    as a rejected task line by the dispatcher.
    """
    print(message, file=sys.stderr, flush=True)


def make_sh(repo_path):
    """The autodispatcher shell with the phanes repository path applied.

    The autodispatcher hardcodes the repository of the planner batch; the hook
    takes that path over, so the operator (and the offline checks) can point it
    somewhere else.  A missing binary is an error code instead of an exception:
    a box without git must skip the planner batch, not break the hook.
    """
    def sh(cmd, timeout=120, inp=None):
        if isinstance(cmd, list) and cmd[:1] == ["git"] and "-C" in cmd:
            cmd = list(cmd)
            cmd[cmd.index("-C") + 1] = repo_path
        try:
            return AD_SH(cmd, timeout=timeout, inp=inp)
        except (OSError, ValueError) as exc:
            return 127, "", str(exc)
    return sh


def configure(state_dir, repo, repo_path):
    """Point the autodispatcher at the phanes runtime this hook works on."""
    ad.D = state_dir
    ad.REPO = repo
    ad.log = log_to_stderr
    ad.save_state = lambda state: save_state(state_dir, state)
    ad.sh = make_sh(repo_path)


def pool_size():
    """The number of dispatchable cards, or None when the pool cannot be read.

    The count comes from the label queue of the repository, so a broken `gh`
    must not look like an empty pool: an unreadable pool mints nothing.
    """
    try:
        return len(ad.queue())
    except Exception:
        return None


def mint(need):
    """File the missing cards and return the references of the filed cards."""
    state = load_state(ad.D)
    try:
        return ad.replenish(need, state)
    except Exception as exc:  # a broken card source must not break the hook
        log_to_stderr(f"phanes seed-mint: card refill failed: {exc!r}", brain=False)
        return []


def main(argv=None):
    parser = argparse.ArgumentParser(description="phanes seed-mint dispatch source hook")
    parser.add_argument("--state-dir", default=os.environ.get("PHANES_AD_STATE_DIR", DEFAULT_STATE_DIR),
                        help="phanes autodispatch state directory (the card backlog lives in it)")
    parser.add_argument("--repo", default=os.environ.get("PHANES_REPO", DEFAULT_REPO),
                        help="repository the cards are filed in")
    parser.add_argument("--repo-path", default=os.environ.get("PHANES_REPO_PATH", DEFAULT_REPO_PATH),
                        help="checkout of the repository the planner batch is read from")
    parser.add_argument("--target", type=int, default=int(os.environ.get("PHANES_MINT_TARGET", DEFAULT_TARGET)),
                        help="wanted number of dispatchable cards; below it the hook mints cards")
    args = parser.parse_args(argv)

    state_dir = os.path.abspath(os.path.expanduser(args.state_dir))
    if paused(state_dir):
        # PAUSE-MINT: the pool must not grow, so the hook stays quiet.
        log_to_stderr("phanes seed-mint: paused (%s)" % ", ".join(PAUSE_MARKERS), brain=False)
        return 0

    os.makedirs(state_dir, exist_ok=True)
    configure(state_dir, args.repo, args.repo_path)
    size = pool_size()
    if size is None:
        log_to_stderr("phanes seed-mint: the card pool could not be read", brain=False)
        return 0
    need = max(0, args.target - size)
    if not need:
        return 0

    for ref in mint(need):
        number = parse_ref(ref)
        if number is None:
            continue
        print(json.dumps(task(number, ad.issue_labels(number)), ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
