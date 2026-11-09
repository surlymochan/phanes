"""Offline acceptance verifier for a recorded evalid role chain (#1982).

The verifier replays one recorded Issue -> Spec -> spec-check -> Build(red,
green) -> holdout -> Review -> delivery chain and reports every binding the
recording violates. It runs no model, opens no socket and never mutates the
recording, so a chain can be checked on any host, offline.

A recording is a de-identified transcript: it carries the sealed receipts, the
reviewed commit chain and the public artifact bytes, but no holdout test
source, no evaluation content and no private log. Each receipt is sealed with
a digest over its own canonical JSON, so a tampered receipt has to be resealed
to look self-consistent; the semantic bindings below are what catch it.

Checked bindings:

* the stage sequence is exactly issue, spec, spec-check, red, green, holdout,
  review, delivery, with no stage missing and none invented;
* every receipt digest matches its content and every receipt binds the digest
  of its parent, so the chain is one unbroken history;
* each model matches the assignment frozen in the Issue receipt and an approved
  profile for its stage (see ``swrm/bin/evalid_roles.py``), and the recorded
  backend and effort are the ones of that profile;
* the frozen base and head are identical in the recording, in the Issue
  receipt, in the Build receipts, in the holdout receipt, in the Review
  receipt and in the delivery receipt;
* the Reviewer saw the whole base..head commit chain, not just its tip;
* the Spec receipt binds the artifact digest, which matches its bytes;
* the chain ends in the controller state VERIFIED, with a passing Reviewer
  that raised no BLOCKER finding and a holdout receipt from an isolated run.

Seam:
    verify_chain(chain: Any) -> list[str]
    -> [] when the recording replays as a VERIFIED chain, otherwise one
       diagnostic string per violated binding.

CLI:
    python3 -m tools.evalid_recorded_chain <recording.json>
    -> exit 0 when the chain is valid, 1 when a binding is violated and 2 when
       the recording cannot be read.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import sys
from pathlib import Path, PurePosixPath
from typing import Any

if __package__ in (None, ""):  # pragma: no cover - direct execution fallback
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.evalid_roles_control import DIGEST, OID, digest


RECORDING_VERSION = 1
RECORDING_FIELDS = {"version", "base_commit", "head_commit", "commits",
                    "artifacts", "receipts"}
STAGE_SEQUENCE = ("issue", "spec", "spec-check", "red", "green", "holdout",
                  "review", "delivery")
# The approved profiles of swrm's role resolver (swrm/bin/evalid_roles.py):
# Spec is sol on codex, the independent checks and the Reviewer are terra on
# codex, and the Build author is either MiniMax on pi (first half of the 50/50
# assignment, no effort) or luna on codex. Every profile is backend plus effort.
PROFILES = {
    "spec": {"gpt-6.1-sol": ("codex", "high")},
    "spec-check": {"gpt-6.1-terra": ("codex", "high")},
    "red": {"MiniMax-M3.1-Flash": ("pi", None), "gpt-6-luna": ("codex", "high")},
    "green": {"MiniMax-M3.1-Flash": ("pi", None), "gpt-6-luna": ("codex", "high")},
    "review": {"gpt-6.1-terra": ("codex", "high")},
}
EXPECTED_MODELS = {stage: tuple(sorted(models)) for stage, models in PROFILES.items()}
# Required and optional fields of every receipt. A field outside these sets is
# rejected: a recording carries no private material, so an unexpected field
# can only be an attempt to smuggle content into the transcript.
RECEIPT_FIELDS = {
    "issue": ({"stage", "issue_ref", "base_commit", "models", "parent_digest", "digest"}, set()),
    "spec": ({"stage", "run_id", "model", "artifact", "artifact_digest", "status", "parent_digest",
              "digest"}, {"backend", "effort"}),
    "spec-check": ({"stage", "run_id", "model", "verdict", "parent_digest",
                    "digest"}, {"backend", "effort"}),
    "red": ({"stage", "run_id", "model", "head_commit", "status", "parent_digest",
             "digest"}, {"backend", "effort"}),
    "green": ({"stage", "run_id", "model", "head_commit", "status", "parent_digest",
               "digest"}, {"backend", "effort"}),
    "holdout": ({"stage", "subject_head", "status", "isolation", "parent_digest",
                 "digest"}, set()),
    "review": ({"stage", "run_id", "model", "reviewed_base", "reviewed_head",
                "commits", "verdict", "findings", "parent_digest", "digest"},
               {"backend", "effort"}),
    "delivery": ({"stage", "head_commit", "controller_status", "parent_digest",
                  "digest"}, set()),
}
# A recorded artifact is public content only: its name, its bytes and the hash
# of those bytes. Any other field could carry hidden test or evaluation
# material into the transcript.
ARTIFACT_FIELDS = {"name", "bytes_base64", "sha256"}
FINDING_FIELDS = {"severity", "requirement_ref", "artifact_ref", "reason", "path", "line"}
FINDING_SEVERITIES = ("BLOCKER", "FOLLOW-UP")


def verify_chain(chain: Any) -> list[str]:
    """Return the bindings a recorded chain violates; empty means VERIFIED.

    The recording is only read. Every diagnostic names one violated binding so
    a failing run says which receipt drifted instead of just "invalid".
    """
    if not isinstance(chain, dict):
        return ["recording_not_a_mapping"]
    errors: list[str] = []
    errors.extend(_envelope_errors(chain))
    receipts = chain.get("receipts")
    if not isinstance(receipts, list) or not receipts:
        errors.append("receipts_missing")
        return list(dict.fromkeys(errors))
    errors.extend(_stage_errors(receipts))
    errors.extend(_receipt_errors(receipts))
    errors.extend(_artifact_errors(chain))
    errors.extend(_binding_errors(chain, receipts))
    return list(dict.fromkeys(errors))


def _envelope_errors(chain: dict[str, Any]) -> list[str]:
    """Check the frozen envelope: version, base, head and the commit chain."""
    errors: list[str] = []
    for field in sorted(set(chain) - RECORDING_FIELDS):
        errors.append(f"unsupported_recording_field:{field}")
    if chain.get("version") != RECORDING_VERSION:
        errors.append("unsupported_recording_version")
    base = chain.get("base_commit")
    head = chain.get("head_commit")
    if not isinstance(base, str) or not OID.fullmatch(base):
        errors.append("invalid_base_commit")
    if not isinstance(head, str) or not OID.fullmatch(head):
        errors.append("invalid_head_commit")
    commits = chain.get("commits")
    if not isinstance(commits, list) or not commits:
        errors.append("invalid_commit_chain")
    elif any(not isinstance(item, str) or not OID.fullmatch(item) for item in commits):
        errors.append("invalid_commit_chain")
    elif len(set(commits)) != len(commits):
        errors.append("duplicate_commit")
    else:
        # The reviewed chain is the ordered base..head range, so the head is
        # its tip and the base itself is not part of it.
        if commits[-1] != head:
            errors.append("head_not_in_commit_chain")
        if base in commits:
            errors.append("base_in_commit_chain")
    return errors


def _stage_errors(receipts: list[Any]) -> list[str]:
    """Check that the chain replays the whole stage sequence, in order."""
    errors: list[str] = []
    stages: list[Any] = []
    for index, receipt in enumerate(receipts):
        if not isinstance(receipt, dict):
            errors.append(f"receipt_not_a_mapping:{index}")
            stages.append(None)
            continue
        stages.append(receipt.get("stage"))
    for stage in STAGE_SEQUENCE:
        if stage not in stages:
            errors.append(f"missing_stage:{stage}")
    for stage in stages:
        if isinstance(stage, str) and stage not in STAGE_SEQUENCE:
            errors.append(f"unexpected_stage:{stage}")
    counts: dict[str, int] = {}
    for stage in stages:
        if isinstance(stage, str):
            counts[stage] = counts.get(stage, 0) + 1
    for stage, count in sorted(counts.items()):
        if count > 1:
            errors.append(f"duplicate_stage:{stage}")
    known = [stage for stage in stages if isinstance(stage, str)]
    if known != list(STAGE_SEQUENCE) and sorted(known) == sorted(STAGE_SEQUENCE):
        errors.append("stage_order_mismatch")
    return errors


def _receipt_errors(receipts: list[Any]) -> list[str]:
    """Check every receipt seal, its parent binding and its field contract."""
    errors: list[str] = []
    previous: dict[str, Any] | None = None
    for index, receipt in enumerate(receipts):
        if not isinstance(receipt, dict):
            previous = None
            continue
        stage = receipt.get("stage")
        known = isinstance(stage, str) and stage in RECEIPT_FIELDS
        label = stage if known else f"receipt[{index}]"
        if "stage" not in receipt:
            errors.append(f"missing_receipt_field:{label}:stage")
        if known:
            required, optional = RECEIPT_FIELDS[stage]
            for field in sorted(set(receipt) - required - optional):
                errors.append(f"unsupported_receipt_field:{label}:{field}")
            for field in sorted(required - set(receipt)):
                errors.append(f"missing_receipt_field:{label}:{field}")
        declared = receipt.get("digest")
        if not isinstance(declared, str) or not DIGEST.fullmatch(declared):
            errors.append(f"receipt_digest_missing:{label}")
        else:
            try:
                sealed = digest({key: value for key, value in receipt.items()
                                 if key != "digest"})
            except (TypeError, ValueError):
                sealed = None
            if sealed != declared:
                errors.append(f"receipt_digest_mismatch:{label}")
        if "parent_digest" in receipt:
            expected = None if previous is None else previous.get("digest")
            if receipt["parent_digest"] != expected:
                errors.append(f"parent_digest_mismatch:{label}")
        if stage in PROFILES:
            model = receipt.get("model")
            if not isinstance(model, str) or model not in PROFILES[stage]:
                errors.append(f"model_mismatch:{label}")
            else:
                errors.extend(_profile_errors(label, receipt, PROFILES[stage][model]))
        previous = receipt
    return errors


def _profile_errors(label: str, receipt: dict[str, Any],
                    profile: tuple[str, str | None]) -> list[str]:
    """Check the recorded backend and effort against the approved profile."""
    backend, effort = profile
    errors: list[str] = []
    recorded_backend = receipt.get("backend")
    if recorded_backend is not None and recorded_backend != backend:
        errors.append(f"backend_mismatch:{label}")
    recorded_effort = receipt.get("effort")
    if recorded_effort is not None and recorded_effort != effort:
        errors.append(f"effort_mismatch:{label}")
    return errors


def _artifact_errors(chain: dict[str, Any]) -> list[str]:
    """Check that every recorded artifact still hashes to its recorded bytes."""
    errors: list[str] = []
    artifacts = chain.get("artifacts")
    if not isinstance(artifacts, list):
        errors.append("artifacts_missing")
        return errors
    names: set[str] = set()
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            errors.append("artifact_not_a_mapping")
            continue
        name = artifact.get("name")
        if not isinstance(name, str) or not name.strip():
            errors.append("artifact_name_missing")
            continue
        if name in names:
            errors.append(f"duplicate_artifact:{name}")
        names.add(name)
        for field in sorted(set(artifact) - ARTIFACT_FIELDS):
            errors.append(f"unsupported_artifact_field:{name}:{field}")
        if name.startswith("/") or ".." in PurePosixPath(name).parts:
            errors.append(f"artifact_path_escapes:{name}")
        encoded = artifact.get("bytes_base64")
        if not isinstance(encoded, str):
            errors.append(f"artifact_bytes_missing:{name}")
            continue
        try:
            payload = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            errors.append(f"artifact_bytes_invalid:{name}")
            continue
        declared = artifact.get("sha256")
        if not isinstance(declared, str) or not DIGEST.fullmatch(declared):
            errors.append(f"artifact_digest_missing:{name}")
        elif digest(payload) != declared:
            errors.append(f"artifact_digest_mismatch:{name}")
    return errors


def _binding_errors(chain: dict[str, Any], receipts: list[Any]) -> list[str]:
    """Check the cross-receipt bindings: base, head, commits, verdict, state."""
    errors: list[str] = []
    by_stage: dict[str, dict[str, Any]] = {}
    for receipt in receipts:
        if isinstance(receipt, dict) and isinstance(receipt.get("stage"), str):
            by_stage.setdefault(receipt["stage"], receipt)
    base = chain.get("base_commit")
    head = chain.get("head_commit")
    commits = chain.get("commits")
    artifacts = chain.get("artifacts")

    issue = by_stage.get("issue")
    if issue is not None:
        if not isinstance(issue.get("issue_ref"), str) or not issue["issue_ref"].strip():
            errors.append("issue_ref_missing")
        if issue.get("base_commit") != base:
            errors.append("base_commit_mismatch:issue")
        models = issue.get("models")
        if not isinstance(models, dict) or set(models) != set(EXPECTED_MODELS):
            errors.append("invalid_frozen_models")
        else:
            for stage, allowed in EXPECTED_MODELS.items():
                model = models[stage]
                if not isinstance(model, str) or model not in allowed:
                    errors.append(f"invalid_frozen_model:{stage}")
                receipt = by_stage.get(stage)
                if receipt is not None and receipt.get("model") != model:
                    errors.append(f"frozen_model_mismatch:{stage}")

    spec = by_stage.get("spec")
    if spec is not None:
        if spec.get("status") != "PASS":
            errors.append("spec_not_passed")
        name = spec.get("artifact")
        if not isinstance(name, str) or not name.strip():
            errors.append("spec_artifact_missing")
        elif not isinstance(artifacts, list) or not any(
                isinstance(item, dict) and item.get("name") == name
                for item in artifacts):
            errors.append(f"spec_artifact_unknown:{name}")
        else:
            matching = [item for item in artifacts
                        if isinstance(item, dict) and item.get("name") == name]
            declared = spec.get("artifact_digest")
            if (not isinstance(declared, str) or not DIGEST.fullmatch(declared)
                    or any(item.get("sha256") != declared for item in matching)):
                errors.append(f"spec_artifact_digest_mismatch:{name}")

    spec_check = by_stage.get("spec-check")
    if spec_check is not None and spec_check.get("verdict") != "PASS":
        errors.append("spec_check_not_passed")

    red = by_stage.get("red")
    if red is not None:
        if red.get("status") != "PASS":
            errors.append("red_not_passed")
        if isinstance(commits, list) and red.get("head_commit") not in commits:
            errors.append("red_head_not_in_commit_chain")
        elif isinstance(commits, list) and commits and red.get("head_commit") == commits[-1]:
            # The Build author commits the failing tests before the
            # implementation, so the red head cannot be the chain tip.
            errors.append("red_head_not_before_head")

    green = by_stage.get("green")
    if green is not None:
        if green.get("status") != "PASS":
            errors.append("green_not_passed")
        if green.get("head_commit") != head:
            errors.append("head_commit_mismatch:green")

    holdout = by_stage.get("holdout")
    if holdout is not None:
        if holdout.get("status") != "PASS":
            errors.append("holdout_not_passed")
        if holdout.get("isolation") != "verified":
            errors.append("holdout_isolation_unverified")
        if holdout.get("subject_head") != head:
            errors.append("subject_head_mismatch:holdout")

    review = by_stage.get("review")
    if review is not None:
        if review.get("verdict") != "PASS":
            errors.append("review_not_passed")
        if review.get("reviewed_base") != base:
            errors.append("reviewed_base_mismatch:review")
        if review.get("reviewed_head") != head:
            errors.append("reviewed_head_mismatch:review")
        # The Reviewer must have seen the whole base..head chain, not only its
        # tip: a dropped commit is an unreviewed commit.
        if review.get("commits") != commits:
            errors.append("commit_chain_mismatch:review")
        findings = review.get("findings")
        if not isinstance(findings, list):
            errors.append("review_findings_missing")
        else:
            for finding in findings:
                if not isinstance(finding, dict):
                    errors.append("unsupported_finding")
                    continue
                if set(finding) - FINDING_FIELDS:
                    errors.append("unsupported_finding_field")
                severity = finding.get("severity")
                if severity not in FINDING_SEVERITIES:
                    errors.append(f"unsupported_finding_severity:{severity}")
                if severity == "BLOCKER":
                    errors.append("review_blocker_finding")

    delivery = by_stage.get("delivery")
    if delivery is not None:
        if delivery.get("controller_status") != "VERIFIED":
            errors.append("delivery_not_verified")
        if delivery.get("head_commit") != head:
            errors.append("head_commit_mismatch:delivery")
    return errors


def main(argv: list[str] | None = None) -> int:
    """Verify a recorded chain file; return 0, 1 or 2 as documented above."""
    parser = argparse.ArgumentParser(
        description="Verify a recorded evalid Issue-to-VERIFIED role chain.")
    parser.add_argument("recording", type=Path,
                        help="path to the recorded chain JSON")
    args = parser.parse_args(argv)
    try:
        chain = json.loads(args.recording.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        print(f"evalid_recorded_chain: {error}", file=sys.stderr)
        return 2
    errors = verify_chain(chain)
    if errors:
        for error in errors:
            print(f"evalid_recorded_chain: {error}", file=sys.stderr)
        print(f"evalid_recorded_chain: {len(errors)} binding(s) violated",
              file=sys.stderr)
        return 1
    print("evalid_recorded_chain: VERIFIED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
