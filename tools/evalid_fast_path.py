"""Deterministic fast-path gate for simple evalid Issues (#1976).

The gate is opt-in and fails closed. It reports a frozen intake as eligible for
the fast path only when the intake already carries everything an independent
Spec run would have had to add: one unambiguous acceptance command, an explicit
exit code and output, concrete boundaries and a narrow file scope. Eligibility
never depends on the length of the Issue text or on a human "simple" label.

An eligible intake yields a content addressed approved-spec receipt, so the
Build and Reviewer stages can bind to the same bytes the Spec stage would have
produced. The receipt binds the whole frozen intake (issue, base, policy and
public scope), not just the parts the gate inspected.

The gate reads metadata only: acceptance commands are opaque data and are never
executed here. The frozen intake is never rewritten.

Seam:
    evaluate(intake: dict, *, enabled: bool = False) -> dict
    -> {"eligible": bool, "approved_spec": dict | None, "reason": str}
"""

from __future__ import annotations

import copy
import json
import math
import re
import sys
from pathlib import Path, PurePosixPath
from typing import Any

if __package__ in (None, ""):  # pragma: no cover - direct execution fallback
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.evalid_roles_control import DIGEST, OID, digest


IDENTITY_FIELDS = ("issue_ref", "base_commit", "policy_digest")
INTAKE_FIELDS = IDENTITY_FIELDS + ("public_scope", "labels")
SCOPE_FIELDS = ("allowed_files", "requirements", "acceptance_commands",
                "expected_exit_code", "expected_output", "boundaries")
BOUNDARY_FIELDS = ("input", "exit_code", "output")
OUTPUT_FIELDS = ("stream", "match", "value")
OUTPUT_STREAMS = ("stdout", "stderr", "both")
OUTPUT_MATCHES = ("exact", "contains", "prefix", "suffix", "regex")
GLOB_CHARACTERS = "*?[]{}"
# Conservative policy: a simple change may name up to four exact files.
MAX_ALLOWED_FILES = 4
BEHAVIOR_PATTERN = re.compile(r"(?:return|emit|print|output)\s+(.+?)\.?", re.IGNORECASE)
ARITHMETIC_PATTERN = re.compile(
    r"the exact decimal (sum|difference|product) of two integers", re.IGNORECASE)

# Values that mean "not decided yet". A field holding one of them is unknown,
# so the gate fails closed instead of guessing what the Spec run would say.
UNKNOWN_MARKERS = frozenset({
    "", "?", "-", "--", "n/a", "na", "n.a.", "none", "null", "nil", "nan",
    "unknown", "unspecified", "undefined", "undecided", "tbd", "todo",
    "fixme", "xxx", "placeholder", "待定", "待确认", "未定", "不确定", "未知", "占位",
})

# Single words that make a requirement non-deterministic on their own.
AMBIGUOUS_WORDS = frozenset({
    "tbd", "todo", "fixme", "xxx", "placeholder", "etc", "etcetera", "perhaps",
    "maybe", "possibly", "probably", "hopefully", "arguably", "roughly",
    "approximately", "nearly", "somehow", "somewhat", "fairly", "whatever",
})

# Multi word and non-latin markers of a vague requirement.
AMBIGUOUS_PHRASES = (
    "as appropriate", "as needed", "as necessary", "as required", "as usual",
    "as much as possible", "as soon as possible", "at your discretion",
    "best effort", "good enough", "and so on", "and more", "or similar",
    "or the like", "more or less", "reasonable", "reasonably", "appropriate",
    "appropriately", "sensible", "suitably", "suitable", "some kind",
    "something like", "if possible", "when possible", "where possible",
    "roughly speaking", "should be able", "try to", "attempt to",
    "to some extent", "in a timely manner",
    "适当", "合理", "尽量", "合适", "等等", "大致", "酌情", "视情况", "差不多",
    "尽快", "待定", "待确认", "未定", "不确定", "未知",
)


def evaluate(intake: Any, *, enabled: Any = False) -> dict[str, Any]:
    """Return the deterministic fast-path verdict for a frozen intake.

    ``enabled`` must be exactly ``True`` to opt in; every other value keeps
    the full Spec path. The verdict is a plain dict so callers can log it as
    controller state without importing this module's internals.
    """
    if enabled is not True:
        return _closed("fast_path_disabled")
    if not isinstance(intake, dict):
        return _closed("intake_not_a_mapping")
    reason = _blocking_reason(intake)
    if reason is not None:
        return _closed(reason)
    try:
        spec = {field: copy.deepcopy(intake["public_scope"][field]) for field in SCOPE_FIELDS}
        receipt = {
            "source": "evalid_fast_path",
            "issue_ref_digest": digest(intake["issue_ref"]),
            "base_commit": intake["base_commit"],
            "policy_digest": intake["policy_digest"],
            "intake_digest": digest(intake),
            "spec": spec,
            "spec_digest": digest(spec),
        }
    except (TypeError, ValueError):
        return _closed("intake_not_serializable")
    return {"eligible": True, "approved_spec": receipt, "reason": "eligible"}


def _closed(reason: str) -> dict[str, Any]:
    """Reject an intake; a closed verdict never mints an approved receipt."""
    return {"eligible": False, "approved_spec": None, "reason": reason}


def _blocking_reason(intake: dict[str, Any]) -> str | None:
    """Return the first reason the intake cannot take the fast path."""
    # Labels are non-authoritative metadata. Every other extension may add a
    # contract this gate does not understand and therefore needs a Spec run.
    if intake.keys() - INTAKE_FIELDS:
        return "unsupported_intake_field"
    for field in IDENTITY_FIELDS:
        value = intake.get(field)
        if not isinstance(value, str) or not value.strip():
            return "unknown_" + field
        if value.strip().lower() in UNKNOWN_MARKERS:
            return "unknown_" + field
    if not OID.fullmatch(intake["base_commit"]):
        return "invalid_base_commit"
    if not DIGEST.fullmatch(intake["policy_digest"]):
        return "invalid_policy_digest"
    scope = intake.get("public_scope")
    if not isinstance(scope, dict):
        return "missing_public_scope"
    for field in SCOPE_FIELDS:
        if field not in scope:
            return "missing_" + field
    # An extension may carry requirements this gate cannot interpret. Route it
    # through Spec rather than silently omitting it from the approved contract.
    if scope.keys() - SCOPE_FIELDS:
        return "unsupported_public_scope_field"
    checks = (("requirements", _requirements_reason),
              ("acceptance_commands", _commands_reason),
              ("expected_exit_code", _exit_code_reason),
              ("expected_output", _output_reason),
              ("boundaries", _boundaries_reason),
              ("allowed_files", _allowed_files_reason))
    for field, check in checks:
        reason = check(scope[field])
        if reason:
            return reason
    return _behavior_consistency_reason(scope)


def _requirements_reason(value: Any) -> str | None:
    """A vague requirement must go back to Spec instead of being guessed."""
    if not isinstance(value, str) or not value.strip():
        return "unknown_requirements"
    reason = _ambiguity_reason(value)
    if reason:
        return reason
    # Recognizing a verb or excluding vague words cannot prove that arbitrary
    # prose defines a function (e.g. "Return 3 or 4 depending on input"). Only
    # recognize this small grammar of complete behaviors. All other prose,
    # including conditional or compound statements, needs independent Spec.
    behavior = BEHAVIOR_PATTERN.fullmatch(value.strip())
    if behavior is None:
        return "requirements_behavior_unspecified"
    if ARITHMETIC_PATTERN.fullmatch(behavior[1]):
        return None
    # An unconditional JSON string or integer literal is also explicit. Parsing
    # the entire object rejects trailing alternatives, qualifiers and sentences.
    if (not behavior[1].startswith('"') and
            not re.fullmatch(r"-?(?:0|[1-9][0-9]*)", behavior[1])):
        return "requirements_behavior_unspecified"
    try:
        literal = json.loads(behavior[1])
    except ValueError:
        return "requirements_behavior_unspecified"
    if type(literal) in (str, int):
        return None
    return "requirements_behavior_unspecified"


def _behavior_consistency_reason(scope: dict[str, Any]) -> str | None:
    """Cross-check the small behavior grammar after validating each field.

    Literal requirements are unconditional: all boundaries must describe the
    same output/exit, and the acceptance matcher must agree with that output.
    For arithmetic, check every two-integer boundary against the operation.
    Other inputs have explicitly declared boundary outcomes; no input for the
    acceptance command is supplied, so its arithmetic result cannot be inferred.
    """
    behavior = BEHAVIOR_PATTERN.fullmatch(scope["requirements"].strip())[1]
    arithmetic = ARITHMETIC_PATTERN.fullmatch(behavior)
    literal = str(json.loads(behavior)) if arithmetic is None else None
    first = scope["boundaries"][0]
    outcomes = {}
    for boundary in scope["boundaries"]:
        input_digest = digest(boundary["input"])
        outcome = (boundary["exit_code"], boundary["output"])
        if input_digest in outcomes and outcomes[input_digest] != outcome:
            return "contradictory_boundary_outcomes"
        outcomes[input_digest] = outcome
        expected = literal
        if arithmetic:
            values = boundary["input"]
            if (isinstance(values, list) and len(values) == 2 and
                    all(type(value) is int for value in values)):
                left, right = values
                operation = arithmetic[1].lower()
                if operation == "sum":
                    expected = str(left + right)
                elif operation == "difference":
                    expected = str(left - right)
                else:
                    expected = str(left * right)
        else:
            if (boundary["exit_code"] != scope["expected_exit_code"] or
                    boundary["output"] != first["output"]):
                return "contradictory_unconditional_outcomes"
        # The literal/decimal grammar leaves a final line terminator optional;
        # concrete boundary output and the acceptance matcher still must agree.
        if expected is not None and boundary["output"] not in (expected, expected + "\n"):
            return "output_contradicts_requirements"
    if arithmetic is None and not _output_matches(scope["expected_output"], first["output"]):
        return "acceptance_output_contradicts_requirements"
    return None


def _output_matches(expectation: dict[str, str], output: str) -> bool:
    """Compare known literal output without running the acceptance command."""
    value = expectation["value"]
    match = expectation["match"]
    if match == "exact":
        return output == value
    if match == "contains":
        return value in output
    if match == "prefix":
        return output.startswith(value)
    if match == "suffix":
        return output.endswith(value)
    return re.search(value, output) is not None


def _ambiguity_reason(text: str) -> str | None:
    lowered = text.strip().lower()
    if lowered in UNKNOWN_MARKERS:
        return "unknown_requirements"
    words = set(re.findall(r"[a-z]+", lowered))
    if words & AMBIGUOUS_WORDS:
        return "ambiguous_requirements"
    for phrase in AMBIGUOUS_PHRASES:
        if phrase in lowered:
            return "ambiguous_requirements"
    return None


def _commands_reason(value: Any) -> str | None:
    """Exactly one acceptance command is required; zero or many are unknown."""
    if not isinstance(value, list) or len(value) != 1:
        return "acceptance_command_not_unique"
    command = value[0]
    if not isinstance(command, list) or not command:
        return "acceptance_command_invalid"
    for item in command:
        if not isinstance(item, str) or not item.strip():
            return "acceptance_command_invalid"
        # '-' and '--' are concrete argv tokens, not unknown-value markers.
        if item.strip().lower() in UNKNOWN_MARKERS - {"-", "--"}:
            return "acceptance_command_unknown"
    return None


def _exit_code_reason(value: Any) -> str | None:
    if type(value) is not int:
        return "unknown_expected_exit_code"
    return None


def _output_reason(value: Any) -> str | None:
    """The expected output must name a stream, a comparison and a value."""
    if not isinstance(value, dict):
        return "unknown_expected_output"
    for field in OUTPUT_FIELDS:
        if field not in value:
            return "missing_expected_output_" + field
    if value.keys() - OUTPUT_FIELDS:
        return "unsupported_expected_output_field"
    if value["stream"] not in OUTPUT_STREAMS:
        return "unknown_expected_output_stream"
    if value["match"] not in OUTPUT_MATCHES:
        return "unknown_expected_output_match"
    if not isinstance(value["value"], str):
        return "unknown_expected_output_value"
    # An empty or placeholder acceptance output is not an explicit expectation.
    if value["value"].strip().lower() in UNKNOWN_MARKERS:
        return "unknown_expected_output_value"
    if value["match"] == "regex":
        try:
            re.compile(value["value"])
        except (re.error, OverflowError):
            return "invalid_expected_output_regex"
    return None


def _boundaries_reason(value: Any) -> str | None:
    """Every boundary needs an input, an exit code and an output."""
    if not isinstance(value, list) or not value:
        return "boundaries_missing"
    for boundary in value:
        if not isinstance(boundary, dict):
            return "boundary_invalid"
        for field in BOUNDARY_FIELDS:
            if field not in boundary:
                return "boundary_missing_" + field
        if boundary.keys() - BOUNDARY_FIELDS:
            return "unsupported_boundary_field"
        if type(boundary["exit_code"]) is not int:
            return "boundary_exit_code_unknown"
        # A boundary may legitimately expect no output at all.
        if (not isinstance(boundary["output"], str) or
                not _concrete_boundary_value(boundary["output"])):
            return "boundary_output_unknown"
        if not _concrete_boundary_value(boundary["input"]):
            return "boundary_input_unknown"
    return None


def _concrete_boundary_value(value: Any) -> bool:
    """Check JSON boundary data recursively; empty inputs/outputs are concrete."""
    if isinstance(value, str):
        return value == "" or value.strip().lower() not in UNKNOWN_MARKERS
    if type(value) in (bool, int):
        return True
    if type(value) is float:
        return math.isfinite(value)
    if isinstance(value, list):
        return all(_concrete_boundary_value(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(key, str) and _concrete_boundary_value(item)
                   for key, item in value.items())
    return False


def _allowed_files_reason(value: Any) -> str | None:
    """Require literal, canonical repository paths, never directory prefixes.

    At most four files can take the fast path; larger changes need Spec.
    Scope is syntactic, just like the controller's exact (non-glob) matching;
    it does not infer file types from a checkout. Require a filename suffix as
    conservative evidence of file intent. Extensionless or unconventional names
    need Spec. New files need not exist at base_commit or in the working tree.
    """
    if not isinstance(value, list) or not value:
        return "allowed_files_missing"
    if len(value) > MAX_ALLOWED_FILES:
        return "allowed_files_not_narrow"
    for path in value:
        if not isinstance(path, str) or not path.strip():
            return "allowed_files_invalid"
        if path.strip().lower() in UNKNOWN_MARKERS:
            return "allowed_files_invalid"
        if PurePosixPath(path) == PurePosixPath("."):
            return "allowed_files_not_narrow"
        if path.startswith(("/", "~")):
            return "allowed_files_not_relative"
        if ".." in PurePosixPath(path).parts:
            return "allowed_files_escapes_repo"
        if any(character in path for character in GLOB_CHARACTERS):
            return "allowed_files_not_narrow"
        if path.endswith("/"):
            return "allowed_files_not_narrow"
        parsed = PurePosixPath(path)
        if (str(parsed) != path or
                not re.fullmatch(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*", path)):
            return "allowed_files_invalid"
        if not parsed.suffix or parsed.suffix == ".":
            return "allowed_files_not_narrow"
    return None
