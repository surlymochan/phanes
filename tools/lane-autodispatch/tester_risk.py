"""Deterministic review-depth classification for tester evidence (#1988)."""
import hashlib
import json
import math
import re
import subprocess


DEFAULT_FILES_FULL_READ = 20
DEFAULT_CHURN_FULL_READ = 1000


def _non_negative_int(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def classify(
    diff_stats,
    needs_human_history=None,
    *,
    pr_number,
    sample_rate=0.2,
    seed="risk-v1",
    files_full_read=DEFAULT_FILES_FULL_READ,
    churn_full_read=DEFAULT_CHURN_FULL_READ,
):
    """Return a fail-closed decision and explanation for the evidence review depth.

    `sample_rate` is the share of the routine cards that the stable sample picks
    for a full read (0 = every routine card is sampled, 1 = every routine card is
    read in full). The pick is derived from SHA-256(seed, PR number), so repeated
    classification of a PR with the same configuration produces the same result.
    """
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


def _read_json(read, *args, paginated=False):
    try:
        rc, out, _ = read(["gh", "api", *args], timeout=30)
        if rc:
            return None
        if not paginated:
            return json.loads(out)
        # gh --paginate emits consecutive JSON values; --slurp needs a newer gh.
        pages = []
        decoder = json.JSONDecoder()
        while out.strip():
            out = out.lstrip()
            page, end = decoder.raw_decode(out)
            pages.append(page)
            out = out[end:]
        return pages
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _needs_human_history(labels, pages):
    """Include removed labels; an unreadable page cannot establish clean history."""
    if not isinstance(labels, list) or not isinstance(pages, list) or not pages:
        return None
    names = []
    for label in labels:
        if not isinstance(label, dict) or not isinstance(label.get("name"), str) or not label["name"]:
            return None
        names.append(label["name"])
    for page in pages:
        if not isinstance(page, list):
            return None
        for event in page:
            if not isinstance(event, dict) or not isinstance(event.get("event"), str) or not event["event"]:
                return None
            if event["event"] in {"labeled", "unlabeled"}:
                label = event.get("label")
                if not isinstance(label, dict) or not isinstance(label.get("name"), str) or not label["name"]:
                    return None
                names.append(label["name"])
    return [True] if "needs-human" in names else []


def classify_card(body, *, repo, read, sample_rate=0.2, seed="risk-v1"):
    """Read a verify card's target PR through AD's command runner, then classify it.

    GitHub's pull response supplies diff counters and current labels; paginated issue
    events supply historical labels, including a needs-human label since removed.
    AD persists the returned decision/reason in the lane's card state.
    """
    target = re.search(r"(?mi)^[ \t]*目标 PR[ \t]*[:：][ \t]*([^\r\n]*)$", body)
    match = re.fullmatch(
        rf"(?:#?([1-9][0-9]*)|https://github\.com/{re.escape(repo)}/pull/([1-9][0-9]*)/?)",
        target[1].strip() if target else "",
    )
    if not match:
        return {"decision": "full_read", "reason": "target PR missing or malformed"}
    pr_number = int(match[1] or match[2])
    pr = _read_json(read, f"repos/{repo}/pulls/{pr_number}")
    pages = _read_json(read, f"repos/{repo}/issues/{pr_number}/events?per_page=100",
                       "--paginate", paginated=True)
    diff = None
    history = None
    if isinstance(pr, dict):
        diff = {"files_changed": pr.get("changed_files"), "additions": pr.get("additions"),
                "deletions": pr.get("deletions")}
        history = _needs_human_history(pr.get("labels"), pages)
    if isinstance(sample_rate, str):
        try:
            sample_rate = float(sample_rate)
        except ValueError:
            sample_rate = None
    return classify(diff, history, pr_number=pr_number, sample_rate=sample_rate, seed=seed)
