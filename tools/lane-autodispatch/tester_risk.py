"""Deterministic review-depth classification for tester evidence (#1988)."""
import hashlib
import json
import math


DEFAULT_FILES_FULL_READ = 20
DEFAULT_CHURN_FULL_READ = 1000


def _non_negative_int(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def classify(
    diff_stats,
    needs_human_history=(),
    *,
    pr_number,
    sample_rate=0.2,
    seed="risk-v1",
    files_full_read=DEFAULT_FILES_FULL_READ,
    churn_full_read=DEFAULT_CHURN_FULL_READ,
):
    """Return a fail-closed decision and explanation for the evidence review depth.

    Sampling is derived from SHA-256(seed, PR number), so repeated classification
    of a PR with the same configuration produces the same result.
    """
    reasons = []
    if not isinstance(diff_stats, dict):
        return {"decision": "full_read", "reason": "diff statistics missing or malformed"}
    required = ("files_changed", "additions", "deletions")
    if any(not _non_negative_int(diff_stats.get(key)) for key in required):
        return {"decision": "full_read", "reason": "diff statistics missing or malformed"}
    if not isinstance(needs_human_history, (list, tuple)) or any(
        not isinstance(item, bool) for item in needs_human_history
    ):
        return {"decision": "full_read", "reason": "needs-human history missing or malformed"}
    if any(not _non_negative_int(value) for value in (files_full_read, churn_full_read)):
        return {"decision": "full_read", "reason": "risk thresholds missing or malformed"}
    if (not isinstance(pr_number, int) or isinstance(pr_number, bool) or pr_number <= 0
            or not isinstance(seed, str) or not seed
            or isinstance(sample_rate, bool) or not isinstance(sample_rate, (int, float))
            or not math.isfinite(sample_rate) or not 0 <= sample_rate <= 1):
        return {"decision": "full_read", "reason": "sampling configuration missing or malformed"}

    files = diff_stats["files_changed"]
    churn = diff_stats["additions"] + diff_stats["deletions"]
    if files >= files_full_read or churn >= churn_full_read:
        return {
            "decision": "full_read",
            "reason": f"large diff: {files} files, {churn} changed lines",
        }
    if any(needs_human_history):
        return {"decision": "full_read", "reason": "needs-human history present"}

    material = json.dumps([seed, pr_number], ensure_ascii=True, separators=(",", ":"))
    bucket = int(hashlib.sha256(material.encode("utf-8")).hexdigest(), 16) / (1 << 256)
    if bucket < sample_rate:
        return {"decision": "full_read", "reason": f"stable sample selected at rate {sample_rate:g}"}
    return {"decision": "sampled", "reason": f"routine diff; stable sample rate {sample_rate:g}"}
