"""CI check classification with configurable red-ignore rules (Refs #1461, part of #1355).

Dispatch, the merge gate and the rerun loop all read the same
`gh pr view --json statusCheckRollup` entries and must agree on what counts as
red. This module is the single place that decides it.

Ignore rules come from config:

* `ignore_red_re`: a red check whose name matches is always ignored (e.g. `upstream`).
* `ignore_red_when_file`: `{"path": ..., "re": ...}` (or a list of them). A red check
  whose name matches `re` is ignored only while `path` exists (e.g. the
  `MACOS-UPSTREAM-RED` switch file with `macos`).

Names are matched case-insensitively. The module is loaded by path:
`openswarm/lib/ci/` is a namespace package without an `__init__.py`.
"""
import os
import re

GREEN = "green"
RED = "red"
PENDING = "pending"
NONE = "none"

_RED_CONCLUSIONS = {"FAILURE", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE", "STALE", "ERROR"}
_GREEN_CONCLUSIONS = {"SUCCESS", "NEUTRAL", "SKIPPED"}
_RED_STATES = {"ERROR", "FAILURE"}


class IgnorePolicy:
    """Which red checks do not count as red."""

    def __init__(self, ignore_re=None, file_rules=(), exists=os.path.exists):
        self.ignore_re = ignore_re
        self.file_rules = list(file_rules)
        self._exists = exists

    @classmethod
    def from_cfg(cls, cfg, exists=os.path.exists):
        """Build a policy from the config mapping; `exists` is injectable for tests."""
        cfg = cfg or {}
        pattern = cfg.get("ignore_red_re")
        ignore_re = re.compile(pattern, re.IGNORECASE) if pattern else None
        raw = cfg.get("ignore_red_when_file") or []
        if isinstance(raw, dict):
            raw = [raw]
        file_rules = [
            (os.path.expanduser(rule["path"]), re.compile(rule["re"], re.IGNORECASE))
            for rule in raw
        ]
        return cls(ignore_re, file_rules, exists=exists)

    def ignores(self, name):
        """True when a red check with this name must not count as red."""
        if self.ignore_re is not None and self.ignore_re.search(name):
            return True
        for path, rule_re in self.file_rules:
            if rule_re.search(name) and self._exists(path):
                return True
        return False


def _bucket(item):
    """Return (name, bucket) for a CheckRun, a StatusContext, or a workflow run.

    A workflow run row (`gh run list --json databaseId,name,conclusion,headSha`)
    has no `status` field at all, so a missing status must not be read as
    "still running": the `conclusion` decides the bucket there.
    """
    if "context" in item and "state" in item:
        name = item.get("context") or ""
        state = (item.get("state") or "").upper()
        if state == "SUCCESS":
            return name, GREEN
        if state in _RED_STATES:
            return name, RED
        return name, PENDING
    name = item.get("name") or item.get("workflowName") or ""
    status = (item.get("status") or "").upper()
    if status and status != "COMPLETED":
        return name, PENDING
    conclusion = (item.get("conclusion") or "").upper()
    if conclusion in _GREEN_CONCLUSIONS:
        return name, GREEN
    if conclusion in _RED_CONCLUSIONS:
        return name, RED
    return name, PENDING


def classify(checks, policy):
    """Summarise a statusCheckRollup list into green, red, pending or none."""
    red, pending, ignored = [], [], []
    seen = False
    for item in checks or []:
        seen = True
        name, bucket = _bucket(item)
        if bucket == RED:
            if policy.ignores(name):
                ignored.append(name)
            else:
                red.append(name)
        elif bucket == PENDING:
            pending.append(name)
    if not seen:
        state = NONE
    elif red:
        state = RED
    elif pending:
        state = PENDING
    else:
        state = GREEN
    return {"state": state, "red": red, "pending": pending, "ignored": ignored}


def rerun_targets(runs, policy):
    """Failed workflow runs worth rerunning, minus the ignored ones, in input order."""
    targets = []
    for run in runs or []:
        name, bucket = _bucket(run)
        if bucket == RED and not policy.ignores(name):
            targets.append(run)
    return targets
