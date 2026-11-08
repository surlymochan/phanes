"""Phanes controller for the evalid role workflow.

The controller owns phase transitions and its append-only receipt log. Executors
are injected behind small adapters so the workflow can be exercised offline and
the swrm lifecycle can be wired without giving a lane the controller's secrets.
"""

from __future__ import annotations

import ast
import fcntl
import fnmatch
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
from io import BytesIO
from pathlib import Path
from typing import Any


STAGES = ("spec", "spec-check", "red", "green", "holdout", "review", "delivery")
MAX_SPEC_REVISIONS = 2
CHANGE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
OID = re.compile(r"^[0-9a-f]{40,64}$")
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_LOCK = threading.RLock()
REVIEW_FIELDS = {"reviewed_base", "reviewed_head", "commits", "verdict", "findings",
                 "holdout_receipt", "spec_digest", "tdd_digest"}


class Rejected(ValueError):
    """An event or evidence package does not satisfy the controller contract."""


def digest(value: Any) -> str:
    raw = value if isinstance(value, bytes) else _canonical(value)
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _reject_private_text(value: Any, suite: Any) -> None:
    """Reject private fragments in lane text without quoting them in errors.

    Six-character windows also catch fragments embedded inside a longer private
    identifier (for example a shortened test name), without word boundaries.
    This conservative fallback is separate from the public receipt whitelist.
    """
    literals, fragments = set(), set()

    def index(text: str) -> None:
        fragments.update(text[i:i + 6] for i in range(len(text) - 5))

    def collect(item: Any) -> None:
        if isinstance(item, str):
            if len(item) >= 6:
                literals.add(item)
            for token in re.findall(r"\w{6,}", item):
                index(token)
            # Suite files are source text; decode quoted string literals as
            # well as indexing identifiers, argv elements and path components.
            for match in re.finditer(r"(['\"])(?:\\.|(?!\1).)*?\1", item):
                try:
                    literal = ast.literal_eval(match.group())
                except (SyntaxError, ValueError):
                    continue
                if isinstance(literal, str):
                    index(literal)
        elif isinstance(item, dict):
            for key, child in item.items():
                collect(key)
                collect(child)
        elif isinstance(item, list):
            for child in item:
                collect(child)

    collect(suite)
    def check(item: Any) -> None:
        if isinstance(item, str):
            if (any(text in item for text in literals) or
                    any(item[i:i + 6] in fragments for i in range(len(item) - 5))):
                raise Rejected("private_evidence_leak:" + digest(item))
        elif isinstance(item, dict):
            for key, child in item.items():
                check(str(key))
                check(child)
        elif isinstance(item, list):
            for child in item:
                check(child)
        elif isinstance(item, (int, float)) and not isinstance(item, bool):
            check(str(item))

    check(value)


def git(project: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(project), *args], check=True,
                            capture_output=True, text=True)
    return result.stdout.strip()


def _tree_snapshot_digest(project: Path, commit: str) -> str:
    result = subprocess.run(["git", "-C", str(project), "ls-tree", "-rz", "-r",
                             "--full-tree", commit], check=True, capture_output=True)
    return digest(result.stdout)


class RoleController:
    """Durable, single-writer workflow controller.

    ``swrm`` is expected to implement ``run(stage, context)`` and return a real
    run receipt. ``evidence`` may implement ``replay(phase, project, command, ...)``
    and ``holdout(project, private_suite, context)``. ``review_run`` may
    implement ``review(context)`` for a separate semantic delivery gate. These
    are controller seams, not functions exported by evidence.py/review_run.py.
    NativeEvidence invokes those tools' CLIs; LocalEvidence is the offline seam.

    A rejected spec-check is kept as evidence and returns to Spec for a
    bounded number of revisions (``MAX_SPEC_REVISIONS``). Each revision
    advances the Spec version, changes its digest and binds the receipt of the
    rejection it answers; a receipt computed against an older Spec revision is
    refused. The third rejection stops the attempt at NEEDS-BOSS instead of
    overwriting the accepted Spec or advancing to red.
    """

    def __init__(self, control_dir: str | os.PathLike[str], project: str | os.PathLike[str],
                 *, swrm: Any = None, evidence: Any = None, review_run: Any = None):
        self.control_dir = Path(control_dir)
        self.project = Path(project).resolve()
        self.control_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.control_dir, 0o700)
        self._log = self.control_dir / "events.jsonl"
        if self._log.exists():
            os.chmod(self._log, 0o600)
        self._swrm, self._evidence, self._review_run = swrm, evidence, review_run
        self._events = self._read_log()
        self._changes: dict[tuple[str, int], dict[str, Any]] = {}
        self._event_keys: dict[tuple[str, int, str, str, str | None], dict[str, Any]] = {}
        for event in self._events:
            self._reduce(event)

    def _read_log(self) -> list[dict[str, Any]]:
        if not self._log.exists():
            return []
        events = []
        with self._log.open(encoding="utf-8") as stream:
            for line in stream:
                if line.strip():
                    events.append(json.loads(line))
        return events

    def _append(self, event: dict[str, Any]) -> dict[str, Any]:
        line = _canonical(event) + b"\n"
        with _LOCK, self._log.open("a+b") as stream:
            os.chmod(self._log, 0o600)
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            stream.seek(0)
            existing = [json.loads(raw) for raw in stream if raw.strip()]
            key = self._key(event)
            attempts = [item for item in existing if (item["change_id"], item["attempt"]) ==
                        (event["change_id"], event["attempt"])]
            if event["stage"] == "spec-check":
                specs = [item for item in attempts if item["stage"] == "spec"]
                if (not specs or event["data"].get("parent_spec_digest") !=
                        specs[-1]["data"].get("spec_digest") or
                        event["data"].get("spec_version") != len(specs)):
                    raise Rejected("spec_check_parent_mismatch")
            elif event["stage"] == "spec" and event["data"].get("parent_receipt"):
                checks = [item for item in attempts if item["stage"] == "spec-check" and
                          item["data"].get("verdict") == "FAIL"]
                if not checks or event["data"]["parent_receipt"] != self._rejection(
                        checks[-1]["data"], 0)["receipt_digest"]:
                    raise Rejected("parent_receipt_mismatch")
            duplicate = next((item for item in existing if self._key(item) == key), None)
            if duplicate:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                return duplicate["data"]
            if event["stage"] == "intake":
                if attempts:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                    raise Rejected("intake_frozen")
            else:
                expected = self._expected_stage(attempts)
                if expected == "needs-boss":
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                    raise Rejected("spec_revision_limit")
                if event["stage"] != expected:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                    raise Rejected("controller_order")
            stream.seek(0, os.SEEK_END)
            stream.write(line)
            stream.flush()
            os.fsync(stream.fileno())
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        self._events.append(event)
        self._reduce(event)
        return event["data"]

    def _refresh(self) -> None:
        """Reload committed events before decisions made by a long-lived worker."""
        with _LOCK, self._log.open("a+b") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_SH)
            stream.seek(0)
            events = [json.loads(raw) for raw in stream if raw.strip()]
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        self._events = []
        self._changes = {}
        self._event_keys = {}
        for item in events:
            self._events.append(item)
            self._reduce(item)

    @staticmethod
    def _expected_stage(attempts: list[dict[str, Any]]) -> str | None:
        """The stage that may follow the recorded attempt history.

        A rejected spec-check returns to Spec for a bounded revision. Once
        the revision budget is spent the attempt stops at NEEDS-BOSS instead
        of overwriting the old Spec or advancing to red.
        """
        if not attempts:
            return None
        last = attempts[-1]
        if last["stage"] == "spec-check" and last["data"].get("verdict") == "FAIL":
            revisions = sum(1 for item in attempts if item["stage"] == "spec") - 1
            return "needs-boss" if revisions >= MAX_SPEC_REVISIONS else "spec"
        return {"intake": "spec", "spec": "spec-check", "spec-check": "red",
                "red": "green", "green": "holdout", "holdout": "review",
                "review": "delivery", "delivery": "complete"}.get(last["stage"])

    @staticmethod
    def _key(event: dict[str, Any]) -> tuple[str, int, str, str, str | None]:
        """Receipt identity: stage, evidence digest and the parent it binds.

        A receipt only speaks about one parent: a Spec revision answers a
        rejected check, a spec-check answers a Spec revision. The parent is
        part of the identity, so a receipt replayed from an earlier round is
        not mistaken for a duplicate of the current one.
        """
        data = event.get("data") or {}
        return (event["change_id"], event["attempt"], event["stage"], event["input_digest"],
                data.get("parent_receipt") or data.get("parent_spec_digest"))

    def _reduce(self, event: dict[str, Any]) -> None:
        self._event_keys[self._key(event)] = event
        key = (event["change_id"], event["attempt"])
        state = self._changes.setdefault(key, {"change_id": key[0], "attempt": key[1],
            "status": "INTAKE_PENDING", "next": "intake", "receipts": {}, "intake": None,
            "private_holdout": None, "spec_version": 0, "spec_revisions": 0, "rejections": []})
        if event["stage"] == "intake":
            state["intake"] = event["data"]
            state["private_holdout"] = event.get("private_holdout")
            state["next"] = "spec"
            state["status"] = "SPEC_PENDING"
        else:
            state["receipts"][event["stage"]] = event["data"]
            if event["stage"] == "holdout":
                state["private_holdout_evidence"] = event.get("private_evidence")
            if event["stage"] == "spec":
                state["spec_version"] += 1
                state["spec_revisions"] = state["spec_version"] - 1
            if event["stage"] == "spec-check" and event["data"].get("verdict") == "FAIL":
                # A rejected check is kept as evidence and returns to Spec.
                state["rejections"].append(self._rejection(event["data"], state["spec_version"]))
                state["next"] = ("needs-boss" if state["spec_revisions"] >= MAX_SPEC_REVISIONS
                                 else "spec")
            else:
                state["next"] = STAGES[STAGES.index(event["stage"]) + 1] if event["stage"] != "delivery" else "complete"
            state["status"] = event["data"].get("controller_status", "INFRA")

    @staticmethod
    def _rejection(data: dict[str, Any], spec_version: int) -> dict[str, Any]:
        """Summarise a rejected spec-check receipt without lane text."""
        blockers = data.get("blockers")
        return {"verdict": "FAIL",
                "receipt_digest": data.get("evidence_digest") or digest(data),
                "spec_version": spec_version,
                "spec_digest": data.get("parent_spec_digest"),
                "blocker_count": len(blockers) if isinstance(blockers, list) else 0}

    def _record(self, change_id: str, attempt: int, stage: str, data: dict[str, Any],
                *, private_holdout: Any = None, private_evidence: Any = None,
                dedupe_digest: str | None = None) -> dict[str, Any]:
        input_digest = dedupe_digest or digest({"stage": stage, "data": data,
                               "private_digest": digest(private_holdout) if private_holdout is not None else None,
                               "private_evidence_digest": digest(private_evidence) if private_evidence is not None else None})
        event = {"change_id": change_id, "attempt": attempt, "stage": stage,
                 "input_digest": input_digest, "data": data}
        if private_holdout is not None:
            event["private_holdout"] = private_holdout
        if private_evidence is not None:
            event["private_evidence"] = private_evidence
        old = self._event_keys.get(self._key(event))
        if old:
            return old["data"]
        return self._append(event)

    def freeze_intake(self, change_id: str, *, attempt: int = 0, issue_ref: str,
                      base_commit: str, policy_digest: str, public_scope: dict[str, Any],
                      holdout_required: bool, holdout_suite: Any = None,
                      budget: dict[str, Any] | None = None) -> dict[str, Any]:
        if not CHANGE_ID.fullmatch(change_id) or attempt < 0:
            raise Rejected("invalid_change")
        self._refresh()
        if not OID.fullmatch(base_commit):
            raise Rejected("invalid_base_commit")
        git(self.project, "cat-file", "-e", f"{base_commit}^{{commit}}")
        if not DIGEST.fullmatch(policy_digest) or not issue_ref.strip() or not isinstance(public_scope, dict):
            raise Rejected("invalid_intake")
        if holdout_required and holdout_suite is None:
            raise Rejected("holdout_suite_required")
        data = {"issue_ref": issue_ref, "base_commit": base_commit,
                "policy_digest": policy_digest, "public_scope": public_scope,
                "holdout_required": bool(holdout_required), "budget": budget or {},
                "intake_digest": ""}
        data["intake_digest"] = digest(data)
        existing = self.state(change_id, attempt)
        if existing:
            if (existing["intake"] != data or
                    digest(existing.get("private_holdout")) != digest(holdout_suite)):
                raise Rejected("intake_frozen")
            return self._public(data, change_id=change_id, attempt=attempt)
        self._record(change_id, attempt, "intake", data, private_holdout=holdout_suite)
        return self._public(data, change_id=change_id, attempt=attempt)

    def state(self, change_id: str, attempt: int = 0) -> dict[str, Any] | None:
        state = self._changes.get((change_id, attempt))
        return json.loads(json.dumps(state)) if state else None

    def accept(self, change_id: str, stage: str, evidence: dict[str, Any], *, attempt: int = 0,
               input_digest: str | None = None) -> dict[str, Any]:
        self._refresh()
        if stage not in STAGES or stage in ("holdout", "delivery"):
            raise Rejected("invalid_stage")
        event_digest = input_digest or digest(evidence)
        if event_digest != digest(evidence):
            raise Rejected("input_digest_mismatch")
        state = self.state(change_id, attempt)
        binding = self._parent_binding(state, stage)
        previous = self._event_keys.get((change_id, attempt, stage, event_digest, binding))
        if previous:
            return self._public(previous["data"], change_id=change_id, attempt=attempt)
        if state and state["next"] == "needs-boss" and stage in ("spec", "spec-check"):
            raise Rejected("spec_revision_limit")
        if not state or state["next"] != stage:
            raise Rejected("controller_order")
        if stage == "spec-check" and self._stale_spec_check(change_id, attempt, event_digest, binding):
            raise Rejected("stale_spec_check_receipt")
        if not isinstance(evidence, dict) or not evidence:
            raise Rejected("missing_evidence")
        submitted_evidence = dict(evidence)
        if stage == "review":
            if set(submitted_evidence) - REVIEW_FIELDS:
                raise Rejected("review_field_unknown")
            findings = submitted_evidence.get("findings", [])
            finding_fields = {"severity", "requirement_ref", "artifact_ref", "reason", "path", "line"}
            if (not isinstance(findings, list) or
                    any(not isinstance(item, dict) or set(item) - finding_fields for item in findings)):
                raise Rejected("review_field_unknown")
        # Commands/manifests are structured public replay inputs, and may
        # legitimately overlap the private runner. They are never published.
        binding_fields = {"spec_digest", "proof_digest", "snapshot_digest", "tdd_digest",
                          "holdout_receipt", "head_commit", "reviewed_head", "reviewed_base",
                          "parent_receipt"}
        text = {key: value for key, value in evidence.items()
                if not (stage in ("red", "green") and key in {"command", "test_manifest"}) and
                not (key in binding_fields and isinstance(value, str) and
                     (DIGEST.fullmatch(value) or OID.fullmatch(value)))}
        _reject_private_text(text, state["private_holdout"])
        if self._swrm is not None:
            run = self._swrm.run(stage, {"change_id": change_id, "attempt": attempt,
                "intake": state["intake"], "evidence": evidence})
            if not isinstance(run, dict) or not run.get("run_id"):
                raise Rejected("missing_run_id")
            if run.get("status") != "done":
                raise Rejected("run_not_complete")
            evidence = dict(evidence, run_id=run["run_id"], model=run.get("model"))
        spec_version = None
        if stage == "spec":
            if evidence.get("status") != "PASS" or not evidence.get("spec_digest"):
                raise Rejected("spec_not_accepted")
            spec_version = self._check_spec_revision(state, evidence)
        elif stage == "spec-check":
            # The controller owns the NEEDS-BOSS escalation: a lane may only
            # pass or reject a Spec revision.
            if evidence.get("verdict") not in ("PASS", "FAIL"):
                raise Rejected("spec_check_verdict_unknown")
            if evidence.get("verdict") == "PASS" and evidence.get("blockers"):
                raise Rejected("spec_check_failed")
            self._check_spec_check(state, evidence)
        elif stage == "red":
            head = evidence.get("head_commit")
            self._check_head(state, evidence)
            if not OID.fullmatch(head or "") or not self._is_ancestor(state["intake"]["base_commit"], head):
                raise Rejected("red_base_mismatch")
            changed = self._changed_paths(state["intake"]["base_commit"], head)
            if not changed or any(not self._is_test_path(path) for path in changed):
                raise Rejected("red_contains_production_change")
            manifest = evidence.get("test_manifest")
            if not isinstance(manifest, dict) or any(path not in manifest for path in changed):
                raise Rejected("red_manifest_missing")
            self._verify_manifest(head, manifest)
            replay = self._replay("red", evidence, expected_rc=1, commit=head)
            if replay.get("failure_kind") != "assertion" or replay.get("test_count", 0) < 1:
                raise Rejected("red_not_assertion")
            if not evidence.get("snapshot_digest"):
                raise Rejected("red_not_replayed")
            evidence["replay_receipt"] = replay
        elif stage == "green":
            self._check_head(state, evidence)
            red_head = state["receipts"]["red"].get("head_commit")
            if not red_head or not self._is_ancestor(red_head, evidence.get("head_commit", "")):
                raise Rejected("green_does_not_contain_red")
            allowed = state["intake"]["public_scope"].get("allowed_files", [])
            excluded = state["intake"]["public_scope"].get("excluded_files", [])
            changed = self._changed_paths(state["intake"]["base_commit"], evidence["head_commit"])
            if (not isinstance(allowed, list) or not allowed or
                    any(not any(fnmatch.fnmatchcase(path, pattern) for pattern in allowed) or
                        any(fnmatch.fnmatchcase(path, pattern) for pattern in excluded)
                        for path in changed)):
                raise Rejected("green_out_of_scope")
            replay = self._replay("green", evidence, expected_rc=0, commit=evidence["head_commit"])
            if evidence.get("status") != "PASS" or replay.get("ok") is not True:
                raise Rejected("green_not_replayed")
            red = state["receipts"].get("red", {})
            if evidence.get("test_manifest") != red.get("test_manifest"):
                raise Rejected("red_manifest_changed")
            self._verify_manifest(evidence["head_commit"], evidence["test_manifest"])
            if not evidence.get("proof_digest") or not evidence.get("snapshot_digest"):
                raise Rejected("green_evidence_missing")
            evidence["replay_receipt"] = replay
        elif stage == "review":
            if evidence.get("verdict") not in ("PASS", "FAIL", "NEEDS-BOSS") or not evidence.get("reviewed_head"):
                raise Rejected("invalid_review")
            if evidence.get("reviewed_head") != git(self.project, "rev-parse", "HEAD"):
                raise Rejected("head_drift")
            if evidence.get("spec_digest") != state["receipts"]["spec"].get("spec_digest"):
                raise Rejected("review_spec_mismatch")
            if evidence.get("tdd_digest") != state["receipts"]["green"].get("evidence_digest"):
                raise Rejected("review_tdd_mismatch")
            if evidence.get("holdout_receipt") != state["receipts"]["holdout"].get("digest"):
                raise Rejected("holdout_binding")
        data = dict(evidence)
        if stage != "spec-check":
            # Only a spec-check receipt binds the spec revision it checked.
            data.pop("parent_spec_digest", None)
        if stage != "spec":
            # Only a Spec revision binds the rejected receipt it answers.
            data.pop("parent_receipt", None)
        if stage == "review":
            data["review_payload"] = submitted_evidence
        data["evidence_digest"] = digest(evidence)
        data["controller_status"] = {"spec": "SPEC_PENDING", "spec-check": "SPEC_PENDING",
            "red": "RED_ACCEPTED", "green": "GREEN_ACCEPTED",
            "review": "REVIEW_PENDING"}[stage]
        if stage == "spec":
            data["spec_version"] = spec_version
            data["versioned"] = evidence.get("spec_version") is not None
            parent = self._revision_parent(state)
            if parent:
                data["parent_receipt"] = parent
        elif stage == "spec-check":
            data["parent_spec_digest"] = binding
            data["spec_version"] = state["spec_version"]
            if (evidence.get("verdict") == "FAIL" and
                    state["spec_revisions"] >= MAX_SPEC_REVISIONS):
                data["controller_status"] = "NEEDS-BOSS"
        result = self._record(change_id, attempt, stage, data, dedupe_digest=event_digest)
        return self._public(result, change_id=change_id, attempt=attempt)

    def _parent_binding(self, state: dict[str, Any] | None, stage: str) -> str | None:
        """The parent a receipt binds to; part of the receipt identity."""
        if not state:
            return None
        if stage == "spec-check":
            return state["receipts"].get("spec", {}).get("spec_digest")
        if stage == "spec":
            return self._revision_parent(state)
        return None

    @staticmethod
    def _revision_parent(state: dict[str, Any]) -> str | None:
        """The rejected receipt a revision answers, if there is one."""
        rejections = state["rejections"]
        return rejections[-1]["receipt_digest"] if rejections else None

    def _check_spec_revision(self, state: dict[str, Any], evidence: dict[str, Any]) -> int:
        """Validate a Spec receipt against the bounded revision contract.

        The first Spec opens the version line at 1 and has no parent. A
        revision answers a rejected check: it advances the version, changes
        the digest and binds the receipt of the rejection it answers.
        """
        current = state["receipts"].get("spec", {})
        declared = evidence.get("spec_version")
        if declared is not None and (isinstance(declared, bool) or
                                     not isinstance(declared, int) or declared < 1):
            raise Rejected("spec_version_invalid")
        parent = evidence.get("parent_receipt")
        if parent is not None and not DIGEST.fullmatch(str(parent)):
            raise Rejected("parent_receipt_invalid")
        expected = state["spec_version"] + 1
        if state["spec_version"] == 0:
            if declared is not None and declared != expected:
                raise Rejected("spec_version_mismatch")
            if parent is not None:
                raise Rejected("parent_receipt_mismatch")
            return expected
        if declared is not None and declared != expected:
            raise Rejected("spec_version_mismatch")
        if declared is None:
            raise Rejected("spec_version_missing")
        rejections = state["rejections"]
        if not rejections:
            raise Rejected("missing_parent_receipt")
        if parent is None:
            raise Rejected("missing_parent_receipt")
        elif parent != rejections[-1]["receipt_digest"]:
            raise Rejected("parent_receipt_mismatch")
        if evidence.get("spec_digest") == current.get("spec_digest"):
            raise Rejected("spec_not_revised")
        return expected

    def _check_spec_check(self, state: dict[str, Any], evidence: dict[str, Any]) -> None:
        """Bind a spec-check receipt to the spec revision it checked."""
        current = state["receipts"].get("spec", {})
        versioned = bool(current.get("versioned"))
        declared = evidence.get("spec_digest")
        version = evidence.get("spec_version")
        if version is not None and (isinstance(version, bool) or not isinstance(version, int)):
            raise Rejected("spec_check_parent_mismatch")
        if declared is not None and declared != current.get("spec_digest"):
            raise Rejected("spec_check_parent_mismatch")
        if version is not None and version != state["spec_version"]:
            raise Rejected("spec_check_parent_mismatch")
        if versioned and (declared is None or version is None):
            raise Rejected("spec_check_parent_missing")

    def _stale_spec_check(self, change_id: str, attempt: int, event_digest: str,
                           binding: str | None) -> bool:
        """True when a receipt replays an earlier round's spec-check."""
        return any(event["change_id"] == change_id and event["attempt"] == attempt and
                   event["stage"] == "spec-check" and event["input_digest"] == event_digest and
                   event["data"].get("parent_spec_digest") != binding
                   for event in self._events)

    def run_holdout(self, change_id: str, *, attempt: int = 0) -> dict[str, Any]:
        self._refresh()
        state = self.state(change_id, attempt)
        prior = self._event_keys.get((change_id, attempt, "holdout", digest({"suite": state["intake"]["intake_digest"]}) if state else "", None))
        if prior:
            return self._public(prior["data"], change_id=change_id, attempt=attempt)
        if not state or state["next"] != "holdout":
            raise Rejected("controller_order")
        private = state["private_holdout"]
        if not state["intake"]["holdout_required"]:
            result = {"status": "NOT_APPLICABLE", "isolation": "unknown"}
        else:
            if self._evidence is None or not hasattr(self._evidence, "holdout"):
                raise Rejected("holdout_runner_missing")
            try:
                result = self._evidence.holdout(self.project, private, self._holdout_context(state))
            except Exception as error:
                raise Rejected("holdout_runner_failed") from error
        if not isinstance(result, dict):
            raise Rejected("holdout_result_missing")
        if result.get("status") not in ("PASS", "FAIL", "INFRA", "NOT_APPLICABLE"):
            raise Rejected("holdout_status_unknown")
        if result.get("isolation", "unknown") not in ("verified", "unisolated", "unknown"):
            raise Rejected("holdout_isolation_unknown")
        head = git(self.project, "rev-parse", "HEAD")
        self._require_clean_worktree()
        green = state["receipts"]["green"]
        if head != green.get("head_commit"):
            raise Rejected("head_drift")
        private_evidence = {"raw": result, "suite_digest": digest(private), "subject_head": head}
        private_digest = digest(private_evidence)
        receipt = {"change_id": change_id, "attempt": attempt, "subject_head": head,
            "spec_digest": state["receipts"]["spec"]["spec_digest"],
            "suite_digest": digest(private), "required": state["intake"]["holdout_required"],
            "status": result["status"], "isolation": result.get("isolation", "unknown"),
            "l1_eligible": result["status"] == "PASS" and result.get("isolation") == "verified",
            "evidence_digest": private_digest, "controller_status": "REVIEW_PENDING"}
        receipt["digest"] = digest(receipt)
        if receipt["status"] == "PASS" and receipt["isolation"] != "verified":
            receipt["l1_eligible"] = False
        self._record(change_id, attempt, "holdout", receipt, private_evidence=private_evidence,
                     dedupe_digest=digest({"suite": state["intake"]["intake_digest"]}))
        return self._public(receipt, change_id=change_id, attempt=attempt)

    def deliver(self, change_id: str, review: dict[str, Any], *, attempt: int = 0,
                commits: list[str] | None = None) -> dict[str, Any]:
        self._refresh()
        state = self.state(change_id, attempt)
        if not state or state["next"] != "delivery":
            raise Rejected("controller_order")
        accepted_review = state["receipts"].get("review", {})
        if accepted_review.get("review_payload") != review:
            raise Rejected("review_evidence_mismatch")
        green = state["receipts"]["green"]
        base, head = state["intake"]["base_commit"], git(self.project, "rev-parse", "HEAD")
        self._require_clean_worktree()
        chain = git(self.project, "rev-list", "--reverse", f"{base}..{head}").splitlines()
        if head != green.get("head_commit") or review.get("reviewed_base") != base or review.get("reviewed_head") != head:
            raise Rejected("head_drift")
        if (review.get("commits") != chain or not chain or
                (commits is not None and commits != chain)):
            raise Rejected("commit_chain_mismatch")
        if review.get("verdict") != "PASS" or any(f.get("severity") == "BLOCKER" for f in review.get("findings", [])):
            raise Rejected("review_failed")
        holdout = state["receipts"].get("holdout", {})
        if review.get("holdout_receipt") != holdout.get("digest"):
            raise Rejected("holdout_binding")
        if holdout.get("status") == "PASS" and holdout.get("isolation") != "verified":
            raise Rejected("holdout_isolation")
        if holdout.get("required") and holdout.get("status") != "PASS":
            raise Rejected("holdout_failed")
        if holdout.get("status") == "NOT_APPLICABLE" and state["intake"].get("holdout_required"):
            raise Rejected("holdout_not_frozen")
        if self._review_run is not None:
            verdict = self._review_run.review({"review": review, "intake": state["intake"],
                "spec": state["receipts"]["spec"], "green": green, "holdout": state["receipts"]["holdout"],
                "base_commit": base, "head_commit": head, "commits": chain})
            if verdict != "PASS":
                raise Rejected("review_run_rejected")
        data = dict(review, commits=chain, controller_status="VERIFIED")
        data["evidence_digest"] = digest(review)
        result = self._record(change_id, attempt, "delivery", data)
        return self._public(result, change_id=change_id, attempt=attempt)

    def _holdout_context(self, state: dict[str, Any]) -> dict[str, Any]:
        return {"change_id": state["change_id"], "attempt": state["attempt"],
                "head_commit": state["receipts"]["green"].get("head_commit"),
                "spec_digest": state["receipts"]["spec"]["spec_digest"]}

    def _replay(self, phase: str, evidence: dict[str, Any], *, expected_rc: int,
                commit: str) -> dict[str, Any]:
        command = evidence.get("command")
        if (self._evidence is None or not hasattr(self._evidence, "replay") or
                not isinstance(command, list) or not command or
                any(not isinstance(item, str) for item in command)):
            raise Rejected("evidence_replay_unavailable")
        snapshot_digest = evidence.get("snapshot_digest")
        actual_snapshot_digest = _tree_snapshot_digest(self.project, commit)
        if not DIGEST.fullmatch(str(snapshot_digest or "")) or snapshot_digest != actual_snapshot_digest:
            raise Rejected("snapshot_mismatch")
        receipt = self._evidence.replay(phase, self.project, command, expected_rc=expected_rc,
                                        commit=commit, expected_snapshot_digest=actual_snapshot_digest)
        if (not isinstance(receipt, dict) or receipt.get("ok") is not True or
                receipt.get("head_commit") != commit or
                receipt.get("snapshot_digest") != actual_snapshot_digest):
            raise Rejected("evidence_replay_failed")
        return receipt

    def _check_head(self, state: dict[str, Any], evidence: dict[str, Any]) -> None:
        self._require_clean_worktree()
        expected = evidence.get("head_commit")
        actual = git(self.project, "rev-parse", "HEAD")
        if actual != expected:
            raise Rejected("head_drift")

    def _require_clean_worktree(self) -> None:
        result = subprocess.run(["git", "-C", str(self.project), "status", "--porcelain",
                                 "--untracked-files=all"], capture_output=True, text=True)
        if result.returncode or result.stdout.strip():
            raise Rejected("dirty_worktree")

    def _is_ancestor(self, older: str, newer: str) -> bool:
        return subprocess.run(["git", "-C", str(self.project), "merge-base", "--is-ancestor", older, newer],
                              capture_output=True).returncode == 0

    def _changed_paths(self, base: str, head: str) -> list[str]:
        result = subprocess.run(["git", "-C", str(self.project), "diff", "--name-only", f"{base}..{head}"],
                                check=True, capture_output=True, text=True)
        return result.stdout.splitlines()

    def _verify_manifest(self, commit: str, manifest: Any) -> None:
        if not isinstance(manifest, dict) or not manifest:
            raise Rejected("test_manifest_missing")
        for path, expected in manifest.items():
            if (not isinstance(path, str) or path.startswith("/") or ".." in Path(path).parts or
                    not DIGEST.fullmatch(str(expected))):
                raise Rejected("test_manifest_invalid")
            result = subprocess.run(["git", "-C", str(self.project), "show", f"{commit}:{path}"],
                                    capture_output=True)
            if result.returncode or digest(result.stdout) != expected:
                raise Rejected("test_manifest_mismatch")

    @staticmethod
    def _is_test_path(path: str) -> bool:
        return bool(re.search(r"(^|/)(test|tests|spec|__tests__)/|(^|/)[^/]+(_test|\.test|\.spec)\.[^/]+$", path))

    @staticmethod
    def _public(value: dict[str, Any], *, change_id: str, attempt: int) -> dict[str, Any]:
        """Build a receipt from fixed fields; never copy arbitrary lane text.

        Scope and manifest bindings retain their field names, but expose only
        hashes of paths. Raw evidence, including legacy receipts, stays private.
        """
        public = {"change_id": change_id, "attempt": attempt}
        for key in ("intake_digest", "policy_digest", "spec_digest", "suite_digest",
                    "evidence_digest", "digest", "proof_digest", "snapshot_digest",
                    "tdd_digest", "holdout_receipt", "parent_receipt", "parent_spec_digest"):
            item = value.get(key)
            if isinstance(item, str) and DIGEST.fullmatch(item):
                public[key] = item
        for key in ("base_commit", "head_commit", "subject_head", "reviewed_base", "reviewed_head"):
            item = value.get(key)
            if isinstance(item, str) and OID.fullmatch(item):
                public[key] = item
        enums = {
            "status": {"PASS", "FAIL", "INFRA", "NOT_APPLICABLE"},
            "verdict": {"PASS", "FAIL", "NEEDS-BOSS"},
            "isolation": {"verified", "unisolated", "unknown"},
            "controller_status": {"SPEC_PENDING", "RED_ACCEPTED", "GREEN_ACCEPTED",
                                  "REVIEW_PENDING", "VERIFIED", "INFRA", "NEEDS-BOSS"},
        }
        for key, choices in enums.items():
            if isinstance(value.get(key), str) and value[key] in choices:
                public[key] = value[key]
        for key in ("required", "holdout_required", "l1_eligible", "versioned"):
            if type(value.get(key)) is bool:
                public[key] = value[key]
        for key in ("spec_version",):
            if type(value.get(key)) is int:
                public[key] = value[key]
        if "issue_ref" in value:
            public["issue_ref_digest"] = digest(value["issue_ref"])
        if "budget" in value:
            public["budget_digest"] = digest(value["budget"])
        if "run_id" in value:
            public["run_id"] = digest(value["run_id"])
        if "model" in value:
            public["model_digest"] = digest(value["model"])
        scope = value.get("public_scope")
        if isinstance(scope, dict):
            public["public_scope"] = {key: [digest(path) for path in scope.get(key, [])]
                                      for key in ("allowed_files", "excluded_files")
                                      if isinstance(scope.get(key, []), list)}
        manifest = value.get("test_manifest")
        if isinstance(manifest, dict):
            public["test_manifest"] = [{"path_digest": digest(path), "digest": content}
                for path, content in sorted(manifest.items())
                if isinstance(content, str) and DIGEST.fullmatch(content)]
        commits = value.get("commits")
        if isinstance(commits, list):
            public["commits"] = [item for item in commits
                                 if isinstance(item, str) and OID.fullmatch(item)]
        findings = value.get("findings")
        if isinstance(findings, list):
            public["finding_count"] = len(findings)
            public["blocker_count"] = sum(isinstance(item, dict) and item.get("severity") == "BLOCKER"
                                          for item in findings)
        return public

    @staticmethod
    def _public_rejection(record: dict[str, Any]) -> dict[str, Any]:
        """Public summary of one rejected spec-check receipt."""
        public = {"verdict": record["verdict"]}
        for key in ("receipt_digest", "spec_digest"):
            item = record.get(key)
            if isinstance(item, str) and DIGEST.fullmatch(item):
                public[key] = item
        for key in ("spec_version", "blocker_count"):
            if type(record.get(key)) is int:
                public[key] = record[key]
        return public

    def public_state(self, change_id: str, attempt: int = 0) -> dict[str, Any] | None:
        state = self.state(change_id, attempt)
        if state is None:
            return None
        return {"change_id": state["change_id"], "attempt": state["attempt"], "status": state["status"],
                "next": state["next"], "intake": self._public(state["intake"], change_id=change_id, attempt=attempt),
                "receipts": {stage: self._public(receipt, change_id=change_id, attempt=attempt)
                             for stage, receipt in state["receipts"].items()},
                "rejections": [self._public_rejection(item) for item in state["rejections"]]}


class MockSwrm:
    """Fixture-compatible swrm contract: stage name -> actual run receipt."""

    def __init__(self, responses: dict[str, dict[str, Any]]):
        self.responses = responses
        self.calls: list[str] = []

    def run(self, stage: str, context: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(stage)
        response = self.responses.get(stage)
        if response is None:
            return {}
        return dict(response)


class LocalEvidence:
    """Offline evidence adapter used by tests and local acceptance drills."""

    def replay(self, phase: str, project: Path, command: list[str], *, expected_rc: int,
               commit: str, expected_snapshot_digest: str, timeout_s: int = 30) -> dict[str, Any]:
        snapshot_digest = _tree_snapshot_digest(project, commit)
        if snapshot_digest != expected_snapshot_digest:
            return {"phase": phase, "ok": False, "head_commit": commit,
                    "snapshot_digest": snapshot_digest}
        with tempfile.TemporaryDirectory(prefix="evalid-replay-") as temporary:
            snapshot = Path(temporary) / "snapshot"
            snapshot.mkdir()
            archive = subprocess.run(["git", "-C", str(project), "archive", "--format=tar", commit],
                                     check=True, capture_output=True)
            with tarfile.open(fileobj=BytesIO(archive.stdout), mode="r:") as bundle:
                bundle.extractall(snapshot)
            completed = subprocess.run(command, cwd=snapshot, capture_output=True,
                text=True, timeout=timeout_s, check=False,
                env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
        output = completed.stdout + completed.stderr
        count_match = re.search(r"Ran\s+(\d+)\s+tests?", output)
        return {"phase": phase, "rc": completed.returncode,
                "expected_rc": expected_rc, "ok": completed.returncode == expected_rc,
                "head_commit": commit, "snapshot_digest": snapshot_digest,
                "failure_kind": "assertion" if "AssertionError" in output else None,
                "test_count": int(count_match.group(1)) if count_match else 0,
                "output_digest": digest(output)}

    def holdout(self, project: Path, private_suite: Any, context: dict[str, Any]) -> dict[str, Any]:
        command = private_suite["argv"]
        result = self.replay("posthoc", project, command,
            expected_rc=private_suite.get("expected_exit", 0),
            commit=context["head_commit"],
            expected_snapshot_digest=_tree_snapshot_digest(project, context["head_commit"]),
            timeout_s=private_suite.get("timeout_s", 30))
        return {"status": "PASS" if result["ok"] else "FAIL",
                "isolation": "unisolated",
                "private_result": result}


class NativeEvidence(LocalEvidence):
    """One attempt's private adapter to evidence.py and review_run.py.

    ``tools_dir`` contains the existing tools; they are never copied into a lane.
    Red/green share a native evidence record, while holdout uses a distinct
    posthoc change and directory. A private suite contains ``files`` (relative
    names to source text) and ``argv`` with a literal ``{holdout}`` placeholder.
    No semantic Review or isolation guarantee is provided by review_run.py.
    """

    def __init__(self, control_dir: str | os.PathLike[str], evalid: str | os.PathLike[str],
                 tools_dir: str | os.PathLike[str], *, change_id: str, attempt: int = 0):
        if not CHANGE_ID.fullmatch(change_id) or attempt < 0:
            raise Rejected("invalid_change")
        self.root = Path(control_dir).resolve()
        self.evalid = Path(evalid).resolve()
        self.tools = Path(tools_dir).resolve()
        if self.root == self.evalid or self.evalid in self.root.parents:
            raise Rejected("evidence_must_be_outside_evalid")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        self.change_id, self.attempt = change_id, attempt
        self.tool_change = f"{change_id}-attempt-{attempt}"

    def _invoke(self, tool: str, args: list[str]) -> tuple[int, dict[str, Any]]:
        completed = subprocess.run([sys.executable, str(self.tools / tool), *args],
            capture_output=True, text=True, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
        try:
            result = json.loads(completed.stdout)
        except ValueError as error:
            raise Rejected("invalid_native_output") from error
        if not isinstance(result, dict):
            raise Rejected("invalid_native_output")
        return completed.returncode, result

    def _checkout(self, project: Path, destination: Path, commit: str) -> None:
        if self.root == project or project in self.root.parents:
            raise Rejected("evidence_must_be_outside_project")
        subprocess.run(["git", "clone", "--quiet", "--no-hardlinks", "--no-checkout",
                        str(project), str(destination)], check=True, capture_output=True)
        git(destination, "checkout", "--quiet", "--detach", commit)

    def replay(self, phase: str, project: Path, command: list[str], *, expected_rc: int,
               commit: str, expected_snapshot_digest: str, timeout_s: int = 30) -> dict[str, Any]:
        receipt = super().replay(phase, project, command, expected_rc=expected_rc,
            commit=commit, expected_snapshot_digest=expected_snapshot_digest, timeout_s=timeout_s)
        if not receipt["ok"]:
            return receipt
        out = self.root / "tdd"
        record_path = out / "record.json"
        record = json.loads(record_path.read_text()) if record_path.exists() else None
        common = {"change_id": self.tool_change, "command": shlex.join(command),
                  "evalid_commit": git(self.evalid, "rev-parse", "HEAD")}
        if record and any(record.get(key) != value for key, value in common.items()):
            raise Rejected("native_identity_mismatch")
        with tempfile.TemporaryDirectory(prefix="replay-", dir=self.root) as temporary:
            snapshot = Path(temporary) / "project"
            self._checkout(project, snapshot, commit)
            # Restore native proofs alongside their snapshots for I6b. The
            # immutable red record must survive a fresh green checkout/restart.
            if record:
                proofdir = snapshot / "specs" / "changes" / self.tool_change / "tdd"
                for label, info in record["phases"].items():
                    source = out / label
                    hashes = {path.relative_to(source).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                              for path in sorted(source.rglob("*")) if path.is_file() and not path.is_symlink()}
                    if hashes != info["proof_tree"]:
                        raise Rejected("native_sealed_evidence_mismatch")
                    proofdir.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source / f"{label}-0.json", proofdir / f"{label}-0.json")
                    shutil.copytree(source / "snapshot", proofdir / "snapshots" / f"{label}-0")
            args = ["--project", str(snapshot), "--evalid", str(self.evalid),
                    "--evidence", str(out), "--change-id", self.tool_change,
                    "--command", shlex.join(command)]
            if not record or phase not in record["phases"]:
                code, result = self._invoke("evidence.py", ["capture", *args, "--phase", phase])
                if (code != 0 or result.get("ok") is not True or result.get("commit") != commit or
                        result.get("evalid_commit") != common["evalid_commit"] or
                        result.get("phase") != phase):
                    raise Rejected("native_capture_failed")
            elif record["phases"][phase]["commit"] != commit:
                raise Rejected("native_commit_mismatch")
            if phase == "green":
                code, result = self._invoke("evidence.py", ["verify", *args])
                if (code != 0 or result.get("ok") is not True or result.get("commit") != commit or
                        result.get("evalid_commit") != common["evalid_commit"] or
                        result.get("classification") != "tdd"):
                    raise Rejected("native_verify_failed")
        return receipt

    def holdout(self, project: Path, private_suite: Any, context: dict[str, Any]) -> dict[str, Any]:
        if (context["change_id"], context["attempt"]) != (self.change_id, self.attempt):
            raise Rejected("native_attempt_mismatch")
        record = json.loads((self.root / "tdd" / "record.json").read_text())
        if record["phases"].get("green", {}).get("commit") != context["head_commit"]:
            raise Rejected("native_commit_mismatch")
        # review_run archives the suite beneath <change>-holdout/holdout-files,
        # writes result.json/logs, and uses evidence.py for capture and verify.
        # All of these remain in this controller-owned directory.
        out = self.root / "holdout"
        if out.exists():
            # A crash after the native run but before the controller's receipt
            # must be retryable without overwriting any earlier private proof.
            out = Path(tempfile.mkdtemp(prefix="holdout-retry-", dir=self.root))
        with tempfile.TemporaryDirectory(prefix="holdout-", dir=self.root) as temporary:
            work = Path(temporary)
            snapshot, suite = work / "project", work / "suite"
            self._checkout(project, snapshot, context["head_commit"])
            suite.mkdir()
            for name, source in private_suite["files"].items():
                if not name or Path(name).is_absolute() or ".." in Path(name).parts:
                    raise Rejected("invalid_holdout_path")
                target = suite / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(source)
            # review_run substitutes a shell-quoted path. Quote only the other
            # portions of each argument, so paths containing spaces still work.
            command = " ".join("{holdout}".join(shlex.quote(part) if part else ""
                for part in item.split("{holdout}")) or "''" for item in private_suite["argv"])
            if "{holdout}" not in command:
                raise Rejected("holdout_placeholder_required")
            code, result = self._invoke("review_run.py", [
                "--project", str(snapshot), "--evalid", str(self.evalid),
                "--evidence", str(out), "--change-id", self.tool_change + "-acceptance",
                "--cmd-visible", record["command"], "--cmd-holdout", command,
                "--holdout-dir", str(suite), "--phase", "posthoc",
                "--timeout", str(private_suite.get("timeout_s", 30))])
        status = result.get("verdict")
        if (status not in ("PASS", "FAIL", "INFRA") or
                code != (0 if status == "PASS" else 1) or
                result.get("classification") != "posthoc" or
                result.get("commit") != context["head_commit"] or
                (status == "PASS" and (result.get("replay", {}).get("ok") is not True or
                                       result.get("evalid_commit") != record["evalid_commit"] or
                                       result.get("visible", {}).get("exit") != 0 or
                                       result.get("holdout", {}).get("exit") != 0))):
            status = "INFRA"
        return {"status": status, "isolation": "unisolated", "private_result": result,
                "exit_code": code}


def atomic_write_json(path: str | os.PathLike[str], value: Any) -> None:
    """Atomic helper for callers persisting controller-owned snapshots."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".evalid-", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(_canonical(value) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
