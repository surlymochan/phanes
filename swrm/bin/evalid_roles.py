#!/usr/bin/env python3
"""Role profiles and subprocess adapter for the evalid workflow."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence


ROLES = {"evalid-spec", "evalid-tdd", "evalid-review"}
ROLE_STAGES = {
    "evalid-spec": {"spec"},
    "evalid-tdd": {"red", "green"},
    "evalid-review": {"spec-check", "delivery"},
}
CHANGE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
EVIDENCE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
SWRM_TERMINAL_STATUSES = {"done", "partial", "failed", "stopped", "budget_exceeded", "unroutable"}


class RoleError(ValueError):
    """A role request conflicts with the approved evalid contract."""


class NeedsBoss(RoleError):
    """The bounded quality-repair budget has been exhausted."""


@dataclass(frozen=True)
class Profile:
    role: str
    stage: str
    backend: str
    model: str
    effort: Optional[str]
    planned_backend: Optional[str] = None
    planned_model: Optional[str] = None
    fallback_reason: Optional[str] = None


def _yaml_flow_strings(value: str) -> list[str]:
    """Parse the header's single-line YAML string-sequence subset, without dependencies."""
    token = re.compile(r'''\s*(?:'((?:[^']|'')*)'|"((?:[^"\\]|\\.)*)"|([^,\[\]{}]+?))\s*(?=,|\])''')
    escapes = {"0": "\0", "a": "\a", "b": "\b", "t": "\t", "n": "\n", "v": "\v",
               "f": "\f", "r": "\r", "e": "\x1b", " ": " ", '"': '"', "/": "/", "\\": "\\",
               "N": "\x85", "_": "\xa0", "L": "\u2028", "P": "\u2029"}

    def unescape(match):
        escape = match.group()[1:]
        if escape in escapes:
            return escapes[escape]
        if escape[0] in "xuU" and len(escape) == {"x": 3, "u": 5, "U": 9}[escape[0]]:
            try:
                return chr(int(escape[1:], 16))
            except ValueError as error:
                raise RoleError("invalid YAML character escape") from error
        raise RoleError("invalid YAML string escape")

    items, index = [], 1
    while True:
        while index < len(value) and value[index].isspace():
            index += 1
        if index < len(value) and value[index] == "]":
            if not re.fullmatch(r"(?:\s+#.*)?\s*", value[index + 1:]):
                raise RoleError("unexpected text after YAML list")
            return items
        match = token.match(value, index)
        if not match:
            raise RoleError("invalid YAML string list")
        single, double, plain = match.groups()
        if single is not None:
            item = single.replace("''", "'")
        elif double is not None:
            item = re.sub(r"\\(?:x[0-9a-fA-F]{2}|u[0-9a-fA-F]{4}|U[0-9a-fA-F]{8}|.)",
                          unescape, double)
        else:
            item = plain.strip()
            # Collections, tags, aliases and implicit non-string scalars are outside this contract.
            if (not item or item[0] in "'\"&*!|>@`%" or item.startswith(("- ", "? ", ": "))
                    or re.search(r":(?:\s|$)|\s#", item) or item.startswith("#")
                    or item.lower() in {"null", "~", "true", "false", ".nan", ".inf", "+.inf", "-.inf"}
                    or re.fullmatch(r"[-+]?(?:0o[0-7]+|0x[0-9a-fA-F]+|(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][-+]?[0-9]+)?)", item)):
                raise RoleError("YAML list must contain strings")
        items.append(item)
        index = match.end()
        if value[index] == ",":
            index += 1


def parse_role_header(task_text: str) -> Optional[dict]:
    """Read the small, controlled YAML front matter used by role task cards."""
    lines = task_text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    try:
        end = next(index for index in range(1, len(lines)) if lines[index].strip() == "---")
    except StopIteration as error:
        raise RoleError("unterminated evalid task header") from error

    header = {}
    current_list = None
    for raw in lines[1:end]:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("-"):
            if current_list is None:
                raise RoleError("unexpected list item in evalid task header")
            value = line[1:].strip().strip("\"'")
            header[current_list].append(value)
            continue
        if ":" not in line:
            raise RoleError("invalid evalid task header field")
        key, value = line.split(":", 1)
        key, value = key.strip(), value.strip()
        if key in header:
            raise RoleError(f"duplicate evalid task header field: {key}")
        if value.startswith("["):
            try:
                parsed = _yaml_flow_strings(value)
            except RoleError as error:
                raise RoleError(f"invalid list in evalid task header: {key}") from error
            header[key] = parsed
            current_list = None
        elif value == "":
            header[key] = []
            current_list = key
        else:
            header[key] = value.strip("\"'")
            current_list = None
    return header


def resolve_role(task_text: str, lane_role: Optional[str] = None) -> Optional[dict]:
    """Resolve a card/lane role; return None when the legacy path is requested."""
    controlled_fields = {"evalid_version", "role", "change_id", "input_evidence"}
    lines = task_text.splitlines()
    has_declaration = False
    if lines and lines[0].strip() == "---":
        for line in lines[1:]:
            if line.strip() == "---":
                break
            if line.split(":", 1)[0].strip() in controlled_fields:
                has_declaration = True
                break
    # Legacy metadata is outside the controlled header's string-list contract.
    header = (parse_role_header(task_text) or {}) if has_declaration else {}
    card_role = header.get("role")
    if controlled_fields.intersection(header) and card_role is None:
        raise RoleError("role task card requires role")
    if card_role is not None and card_role not in ROLES:
        raise RoleError(f"unknown evalid role: {card_role}")
    if lane_role is not None and lane_role not in ROLES:
        raise RoleError(f"unknown lane evalid role: {lane_role}")
    if card_role and lane_role and card_role != lane_role:
        raise RoleError("task card role conflicts with lane role")
    role = card_role or lane_role
    if role is None:
        return None

    if card_role and not {"evalid_version", "change_id", "input_evidence"}.issubset(header):
        raise RoleError("role task card requires evalid_version, change_id, and input_evidence")
    if "evalid_version" in header and str(header["evalid_version"]) != "1":
        raise RoleError("unsupported evalid_version")
    change_id = header.get("change_id")
    if card_role and (not isinstance(change_id, str) or not CHANGE_ID.fullmatch(change_id)):
        raise RoleError("invalid evalid change_id")
    evidence = header.get("input_evidence")
    if evidence is not None:
        if not isinstance(evidence, list) or any(not EVIDENCE_ID.fullmatch(item) for item in evidence):
            raise RoleError("input_evidence must contain sha256 content addresses")
        if len(evidence) != len(set(evidence)):
            raise RoleError("input_evidence must be deduplicated")
    if role == "evalid-spec" and evidence is not None and not evidence:
        raise RoleError("evalid-spec requires frozen intake evidence")
    if card_role and (not isinstance(evidence, list) or not evidence):
        raise RoleError("role task card requires non-empty input_evidence")
    return {"role": role, "evalid_version": 1, "change_id": change_id, "input_evidence": evidence}


def _profile(role: str, stage: str, backend: str, model: str, effort: Optional[str], **metadata) -> Profile:
    if role not in ROLE_STAGES or stage not in ROLE_STAGES[role]:
        raise RoleError(f"stage {stage!r} is not allowed for role {role!r}")
    return Profile(role, stage, backend, model, effort, **metadata)


def resolve_profile(role: str, stage: str, *, assignment_index: Optional[int] = None,
                    minimax_available: bool = True, agy_model: Optional[str] = None,
                    backend: Optional[str] = None, model: Optional[str] = None,
                    effort: Optional[str] = None) -> Profile:
    """Resolve the approved role profile and reject caller model overrides."""
    if role == "evalid-spec" and stage == "spec":
        expected = _profile(role, stage, "codex", "gpt-6.1-sol", "high")
    elif role == "evalid-review" and stage in ("spec-check", "delivery"):
        expected = _profile(role, stage, "codex", "gpt-6.1-terra", "high")
    elif role == "evalid-tdd" and stage in ("red", "green"):
        if not isinstance(assignment_index, int) or assignment_index < 0:
            raise RoleError("TDD profile requires a non-negative first-round assignment index")
        if assignment_index % 2 == 0:
            if minimax_available:
                expected = _profile(role, stage, "pi", "MiniMax-M3.1-Flash", None,
                                    planned_backend="pi", planned_model="MiniMax-M3.1-Flash")
            else:
                if not agy_model or not agy_model.strip():
                    raise RoleError("MiniMax is unavailable and no approved agy model is configured")
                expected = _profile(role, stage, "agy", agy_model.strip(), None,
                                    planned_backend="pi", planned_model="MiniMax-M3.1-Flash",
                                    fallback_reason="MiniMax unavailable")
        else:
            expected = _profile(role, stage, "codex", "gpt-6-luna", "high",
                                planned_backend="codex", planned_model="gpt-6-luna")
    else:
        raise RoleError(f"unsupported role stage: {role}/{stage}")

    requested = {"backend": backend, "model": model, "effort": effort}
    for key, value in requested.items():
        if value is not None and value != getattr(expected, key):
            raise RoleError(f"{key} override conflicts with frozen profile")
    return expected


def repair_profile(repair_round: int, *, stage: str = "green", backend: Optional[str] = None,
                   model: Optional[str] = None, effort: Optional[str] = None) -> Profile:
    """Return the two allowed sol repair turns; then stop at NEEDS-BOSS."""
    if repair_round not in (0, 1):
        raise NeedsBoss("NEEDS-BOSS: quality repair budget exhausted")
    expected = _profile("evalid-tdd", stage, "codex", "gpt-6.1-sol",
                        "medium" if repair_round == 0 else "high")
    for key, value in {"backend": backend, "model": model, "effort": effort}.items():
        if value is not None and value != getattr(expected, key):
            raise RoleError(f"{key} override conflicts with frozen repair profile")
    return expected


def build_swrm_command(swrm_bin: str, *, task_file: str, cwd: str, base: str,
                       verify: str, profile: Profile, python: str = sys.executable) -> list[str]:
    """Build the existing one-shot swrm command without shell interpolation."""
    if not task_file or not cwd or not base or not verify:
        raise RoleError("task_file, cwd, base, and public verify command are required")
    command = [python, swrm_bin, "run", "--cwd", cwd, "--task-file", task_file,
               "--base", base, "--backend", profile.backend, "--model", profile.model,
               "--verify", verify, "--no-auto-review"]
    if profile.effort is not None:
        command.extend(("--effort", profile.effort))
    if profile.role == "evalid-review":
        command.append("--read-only")
    return command


def run_swrm(command: Sequence[str], profile: Profile, *, runner: Callable = subprocess.run,
             timeout: Optional[float] = None) -> dict:
    """Preserve terminal swrm receipts, checking the frozen profile for executed runs."""
    try:
        completed = runner(list(command), capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as error:
        raise RoleError(f"swrm subprocess failed: {error}") from error
    try:
        result = json.loads(completed.stdout)
    except (TypeError, json.JSONDecodeError) as error:
        raise RoleError("swrm did not return a JSON run result") from error
    if not isinstance(result, dict) or not result.get("id"):
        raise RoleError("swrm result has no actual run ID")
    if result.get("status") not in SWRM_TERMINAL_STATUSES:
        raise RoleError("swrm result is not terminal")
    actual = (result.get("backend"), result.get("model"), result.get("effort"))
    expected = (profile.backend, profile.model, profile.effort)
    # Some backends do not report an effort; normalize only a missing/null effort.
    # Unroutable receipts have only id/status/decision: no executor was selected.
    unroutable = result["status"] == "unroutable" and not any(key in result for key in ("backend", "model", "effort"))
    if not unroutable and actual != expected:
        raise RoleError("actual swrm model profile differs from the frozen role profile")
    if completed.returncode and result["status"] == "done":
        raise RoleError(f"swrm exited {completed.returncode} despite a done result")
    return result


def build_evalid_command(xflow_bin: str, *, request_file: str, output_file: str) -> list[str]:
    """Pass a controller-owned public request to the stage wrapper as argv."""
    if not xflow_bin or not request_file or not output_file:
        raise RoleError("xflow_bin, request_file, and output_file are required")
    if Path(request_file).resolve() == Path(output_file).resolve():
        raise RoleError("role output must not overwrite the controlled request")
    return [xflow_bin, "role", "run", "--request", request_file, "--output", output_file]


def run_evalid(command: Sequence[str], *, runner: Callable = subprocess.run,
               timeout: Optional[float] = None) -> dict:
    """Preserve wrapper rc 0/1/2/3 and public output; never infer a model verdict."""
    try:
        output = Path(command[command.index("--output") + 1])
    except (ValueError, IndexError) as error:
        raise RoleError("evalid command requires an output file") from error
    if output.exists() or output.is_symlink():
        raise RoleError("evalid output must be fresh; existing evidence cannot be overwritten")
    try:
        completed = runner(list(command), capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as error:
        raise RoleError(f"evalid subprocess failed: {error}") from error
    if completed.returncode not in (0, 1, 2, 3):
        raise RoleError(f"unexpected evalid exit code: {completed.returncode}")
    artifact = None
    if output.is_symlink():
        raise RoleError("evalid output must not be a symbolic link")
    if output.exists():
        try:
            artifact = json.loads(output.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise RoleError("evalid did not write a JSON role result") from error
        if not isinstance(artifact, dict):
            raise RoleError("evalid role result must be an object")
    if completed.returncode == 0 and artifact is None:
        raise RoleError("evalid succeeded without a role result")
    return {"rc": completed.returncode, "artifact": artifact,
            "stdout": completed.stdout, "stderr": completed.stderr}


def _main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    profile_parser = sub.add_parser("profile")
    profile_parser.add_argument("role", choices=sorted(ROLES))
    profile_parser.add_argument("stage")
    profile_parser.add_argument("--assignment-index", type=int)
    profile_parser.add_argument("--minimax-unavailable", action="store_true")
    profile_parser.add_argument("--agy-model")
    args = parser.parse_args(argv)
    try:
        profile = resolve_profile(args.role, args.stage, assignment_index=args.assignment_index,
                                  minimax_available=not args.minimax_unavailable, agy_model=args.agy_model)
        print(json.dumps(asdict(profile), ensure_ascii=False, sort_keys=True))
        return 0
    except RoleError as error:
        print(str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(_main())
