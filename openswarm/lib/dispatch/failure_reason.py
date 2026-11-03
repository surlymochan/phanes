"""#1710: the canonical reason every ended run must carry.

A run that ends without a usable result records one reason from
``RUN_FAILURE_REASONS``.  A cause outside the enumeration (or a missing one)
becomes ``unknown`` and keeps the raw error next to it, so a failed run is
never reason-less (``None``) and never loses the error that explains it.

The enumeration is deliberately small: the fleet health histogram counts these
reasons, so they must stay comparable across backends.
"""

import re

RUN_FAILURE_REASONS = ("spawn-failed", "fetch-failed", "timeout", "oom-killed",
                       "exec-error", "budget-exceeded", "rate-limit", "unknown")

# error_class (what the executor reports) -> reason (what the fleet counts).
REASON_ALIASES = {
    "spawn_failed": "spawn-failed",
    "prepare_failed": "spawn-failed",
    "worktree_unavailable": "spawn-failed",
    "session_failed": "spawn-failed",
    "fetch_failed": "fetch-failed",
    "timeout": "timeout",
    "timed_out": "timeout",
    "stalled": "timeout",
    "oom": "oom-killed",
    "oom_killed": "oom-killed",
    "exec_error": "exec-error",
    "executor_io_lost": "exec-error",
    "executor_failed": "exec-error",
    "budget_exceeded": "budget-exceeded",
    "budget_exceeded_daily": "budget-exceeded",
    "rate_limit": "rate-limit",
    "quota": "rate-limit",
}

# The specific reasons win over the generic exec-error alias: a session that
# could not be derived is a spawn failure, not a plain execution error.
SPECIFIC_REASONS = ("spawn-failed", "fetch-failed", "timeout", "oom-killed",
                    "budget-exceeded", "rate-limit")

SPAWN_RE = re.compile(r"(session|spawn|deriv|prepare|workspace|会话|派生)", re.I)
FAILED_RE = re.compile(r"(fail|failed|failure|error|denied|refused|cannot|can't|could not|unable|"
                       r"失败|错误|拒绝|无法)", re.I)
TIMEOUT_RE = re.compile(r"(timed out|timeout|deadline exceeded|超时)", re.I)
OOM_RE = re.compile(r"(out of memory|oom[- ]?kill|\boom\b|内存不足)", re.I)
RATE_RE = re.compile(r"(rate.?limit|too many requests|\b429\b|quota|限流|配额)", re.I)
FETCH_RE = re.compile(r"(git fetch failed|failed to fetch|fetch failed|fatal: unable to access|"
                       r"could not resolve host|拉取失败)", re.I)

MISSING = "原因缺失"


def tail(error_detail=None, rc=None, limit=300):
    """The raw error kept next to a reason: a reason alone cannot be debugged."""
    raw = str(error_detail or "").strip()
    if not raw and rc is not None:
        raw = f"exit code {rc}"
    return raw[-limit:] if raw else MISSING


def classify(error_class=None, error_detail=None, rc=None):
    """The canonical reason of an ended run: always one of ``RUN_FAILURE_REASONS``."""
    name = str(error_class or "").strip().lower().replace("-", "_")
    raw = str(error_detail or "").strip()
    reason = REASON_ALIASES.get(name)
    if reason in SPECIFIC_REASONS:
        return reason
    if raw and SPAWN_RE.search(raw) and FAILED_RE.search(raw):
        return "spawn-failed"
    for pattern, candidate in ((TIMEOUT_RE, "timeout"), (OOM_RE, "oom-killed"),
                               (RATE_RE, "rate-limit"), (FETCH_RE, "fetch-failed")):
        if raw and pattern.search(raw):
            return candidate
    if reason:
        return reason
    return "unknown"


def canonical(reason, error_detail=None, rc=None):
    """Force a reason into the enumeration; an empty or foreign reason becomes ``unknown``."""
    name = str(reason or "").strip()
    base = name.split(":", 1)[0].strip().lower().replace("_", "-")
    if base in RUN_FAILURE_REASONS:
        return base
    return "unknown"
