#!/usr/bin/env python3
"""Offline checker for the review packet of a pull request (Refs #1789, Epic #1785).

The lane reviews of this fleet run in a sandbox that often has no network: the
pull request diff cannot be fetched, ``gh`` is not installed and the reviewed
head may not be in the local object store.  A review that cannot read the
material cannot approve anything, so every pull request that goes through a
review round ships its own review packet: a plain text manifest, the complete
patch of the round and the evidence of the shadow runs.

This tool checks that packet without a network:

  manifest      every file of the packet exists with the size and the sha256
                the manifest records
  patch         the patch is structurally complete: every hunk holds exactly
                the number of lines its header declares, so a truncated patch
                is reported instead of being read as a smaller diff
  patch-vs-git  the patch is byte-identical to ``git diff`` between the
                previous head and the current head (skipped when the previous
                head object is not readable)
  heads         the blob shas of the manifest match the committed tree
                (skipped when git is not usable)
  evidence      every verdict row of the divergence log names a shadow report
                that exists and carries the same pull request, head and verdict
  statistics    the committed statistics are the ones the log yields now

Usage:
  check-review-packet.py [--root DIR] [--packet FILE] [--head REF] [--previous REF] [--json]
  check-review-packet.py --self-test

Exit code 0 means every check passed, 1 means a check failed.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys


SCHEMA = "phanes.review-packet/1"
HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


# ------------------------------------------------------------------ helpers

def sha256_of(path: Path) -> str:
    """Return the sha256 of one file."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git(root: Path, *args: str) -> tuple[int, str]:
    """Run one git command in the repository and return its code and output."""
    try:
        done = subprocess.run(["git", "-C", str(root)] + list(args), capture_output=True,
                              text=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return 1, ""
    return done.returncode, done.stdout


def parse_patch(text: str) -> list[dict]:
    """Return the file blocks of a unified diff.

    A hunk that does not hold the number of lines its header declares means the
    patch was cut off; that is an error, not a smaller diff.
    """
    blocks: list[dict] = []
    current: dict | None = None
    hunk: dict | None = None
    for number, line in enumerate(text.splitlines(), 1):
        if line.startswith("diff --git "):
            current = {"header": line, "hunks": [], "meta": []}
            blocks.append(current)
            hunk = None
            continue
        if current is None:
            if line.strip():
                raise ValueError("line %d is outside of any file block" % number)
            continue
        match = HUNK_RE.match(line)
        if match:
            hunk = {"line": number,
                    "old": int(match.group(2)) if match.group(2) is not None else 1,
                    "new": int(match.group(4)) if match.group(4) is not None else 1,
                    "seen_old": 0, "seen_new": 0}
            current["hunks"].append(hunk)
            continue
        if hunk is not None:
            if line.startswith("\\"):
                continue
            if line.startswith(" "):
                hunk["seen_old"] += 1
                hunk["seen_new"] += 1
            elif line.startswith("-"):
                hunk["seen_old"] += 1
            elif line.startswith("+"):
                hunk["seen_new"] += 1
            else:
                hunk = None
                current["meta"].append(line)
                continue
            if hunk["seen_old"] > hunk["old"] or hunk["seen_new"] > hunk["new"]:
                raise ValueError("hunk at line %d holds more lines than its header declares" % number)
            continue
        current["meta"].append(line)
    for block in blocks:
        for hunk in block["hunks"]:
            if hunk["seen_old"] != hunk["old"] or hunk["seen_new"] != hunk["new"]:
                raise ValueError("hunk at line %d is truncated: %d of %d old and %d of %d new lines"
                                 % (hunk["line"], hunk["seen_old"], hunk["old"],
                                    hunk["seen_new"], hunk["new"]))
    return blocks


def load_log_tool(root: Path):
    """Return the divergence log tool of the repository as a module.

    The tool of the repository wins; the one next to this checker is the
    fallback, so the checker also works on a packet that was copied elsewhere.
    """
    candidates = [root / "tools" / "review-divergence-log.py",
                  Path(__file__).resolve().parent / "review-divergence-log.py"]
    for path in candidates:
        if not path.is_file():
            continue
        spec = importlib.util.spec_from_file_location("review_divergence_log", path)
        if spec is None or spec.loader is None:
            continue
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    raise ValueError("the divergence log tool is missing")


# ------------------------------------------------------------------- checks

def check_manifest(root: Path, manifest: dict) -> tuple[bool, str]:
    """Return whether every file of the manifest is there with the recorded hash."""
    problems = []
    for entry in manifest.get("files", []):
        path = root / entry["path"]
        if not path.is_file():
            problems.append("%s is missing" % entry["path"])
            continue
        size = path.stat().st_size
        if size != entry["bytes"]:
            problems.append("%s has %d bytes, the manifest says %d" % (entry["path"], size, entry["bytes"]))
            continue
        digest = sha256_of(path)
        if digest != entry["sha256"]:
            problems.append("%s has sha256 %s, the manifest says %s" % (entry["path"], digest, entry["sha256"]))
    if problems:
        return False, "; ".join(problems)
    return True, "%d file(s) match the manifest" % len(manifest.get("files", []))


def check_patch(root: Path, manifest: dict) -> tuple[bool, str]:
    """Return whether the patch of the packet is structurally complete."""
    path = root / manifest["patch"]
    if not path.is_file():
        return False, "%s is missing" % manifest["patch"]
    try:
        blocks = parse_patch(path.read_text(encoding="utf-8"))
    except ValueError as error:
        return False, str(error)
    hunks = sum(len(block["hunks"]) for block in blocks)
    if not blocks:
        return False, "%s holds no file block" % manifest["patch"]
    return True, "%d file block(s), %d hunk(s), none truncated" % (len(blocks), hunks)


def check_patch_against_git(root: Path, manifest: dict, head: str = "HEAD") -> tuple[bool, str]:
    """Return whether the patch is the diff between the two heads of the packet."""
    previous = manifest.get("previous_head") or ""
    path = root / manifest["patch"]
    if not path.is_file():
        return False, "%s is missing" % manifest["patch"]
    code, out = git(root, "cat-file", "-e", "%s^{commit}" % previous)
    if code:
        return True, "skipped: the previous head %s is not in the object store" % previous
    args = ["diff", previous, head, "--binary", "--", "."]
    for excluded in manifest.get("patch_excludes", []):
        args += [":(exclude)%s" % excluded]
    code, out = git(root, *args)
    if code:
        return True, "skipped: git diff failed"
    if out != path.read_text(encoding="utf-8"):
        return False, "the patch is not the diff %s..%s" % (previous, head)
    return True, "byte-identical to git diff %s..%s" % (previous, head)


def check_heads(root: Path, manifest: dict, head: str = "HEAD") -> tuple[bool, str]:
    """Return whether the blob shas of the heads manifest match the committed tree."""
    path = root / manifest["heads"]
    if not path.is_file():
        return False, "%s is missing" % manifest["heads"]
    problems = []
    rows = 0
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) != 4:
            problems.append("line %d does not name a path, two blob shas and a size" % number)
            continue
        name, _previous, blob, size = parts
        rows += 1
        code, out = git(root, "rev-parse", "%s:%s" % (head, name))
        if code:
            problems.append("%s is not in the committed tree" % name)
            continue
        if out.strip() != blob:
            problems.append("%s has blob %s, the manifest says %s" % (name, out.strip(), blob))
            continue
        target = root / name
        if target.is_file() and target.stat().st_size != int(size):
            problems.append("%s has %d bytes, the manifest says %s" % (name, target.stat().st_size, size))
    if problems:
        return False, "; ".join(problems)
    return True, "%d blob sha(s) match the committed tree" % rows


def check_evidence(root: Path, manifest: dict) -> tuple[bool, str]:
    """Return whether every verdict row of the log names its shadow report."""
    log = root / manifest["log"]
    if not log.is_file():
        return False, "%s is missing" % manifest["log"]
    tool = load_log_tool(root)
    problems = []
    rows = 0
    for event in tool.read_events(log):
        if event["kind"] != "verdict":
            continue
        rows += 1
        evidence = event.get("evidence") or ""
        report = root / evidence
        if not evidence:
            problems.append("PR #%d round %d has no evidence" % (event["pr"], event["round"]))
            continue
        if not report.is_file():
            problems.append("PR #%d round %d names the missing report %s" % (event["pr"], event["round"], evidence))
            continue
        try:
            data = json.loads(report.read_text(encoding="utf-8"))
        except ValueError as error:
            problems.append("%s is not JSON: %s" % (evidence, error))
            continue
        if data.get("pr") != event["pr"]:
            problems.append("%s belongs to PR #%s, the row is PR #%d" % (evidence, data.get("pr"), event["pr"]))
        if not same_head(data.get("head"), event["head"]):
            problems.append("%s holds head %s, the row is %s" % (evidence, data.get("head"), event["head"]))
        if data.get("verdict") != event["tester_verdict"]:
            problems.append("%s holds verdict %s, the row is %s"
                            % (evidence, data.get("verdict"), event["tester_verdict"]))
    if problems:
        return False, "; ".join(problems)
    return True, "%d verdict row(s) match their shadow report" % rows


def same_head(one: str, other: str) -> bool:
    """Return whether two head spellings name the same commit."""
    one = (one or "").lower()
    other = (other or "").lower()
    if not one or not other:
        return False
    return one.startswith(other) or other.startswith(one)


def check_statistics(root: Path, manifest: dict) -> tuple[bool, str]:
    """Return whether the committed statistics are the ones the log yields now."""
    log = root / manifest["log"]
    stats_path = root / manifest["stats"]
    if not log.is_file() or not stats_path.is_file():
        return False, "%s or %s is missing" % (manifest["log"], manifest["stats"])
    tool = load_log_tool(root)
    fresh = tool.build_stats(tool.fold(tool.read_events(log)))
    stored = json.loads(stats_path.read_text(encoding="utf-8"))
    for key in ("generated_at", "log"):
        fresh.pop(key, None)
        stored.pop(key, None)
    if fresh != stored:
        differing = sorted(key for key in set(fresh) | set(stored) if fresh.get(key) != stored.get(key))
        return False, "the committed statistics differ in %s" % ", ".join(differing)
    return True, "the committed statistics match a fresh computation"


CHECKS = (("manifest", check_manifest),
          ("patch", check_patch),
          ("patch-vs-git", check_patch_against_git),
          ("heads", check_heads),
          ("evidence", check_evidence),
          ("statistics", check_statistics))


def check_packet(root: Path, manifest: dict, head: str = "HEAD") -> list[dict]:
    """Run every check of the packet and return one result per check."""
    results = []
    for name, check in CHECKS:
        try:
            if name in ("patch-vs-git", "heads"):
                ok, detail = check(root, manifest, head)
            else:
                ok, detail = check(root, manifest)
        except (OSError, ValueError, KeyError, TypeError) as error:
            ok, detail = False, "%s" % error
        results.append({"check": name, "ok": bool(ok), "detail": detail})
    return results


# --------------------------------------------------------------------- main

def self_test() -> None:
    """Run a tiny built-in check of the rules the checker depends on."""
    complete = ("diff --git a/x b/x\n"
                "index 1111111..2222222 100644\n"
                "--- a/x\n"
                "+++ b/x\n"
                "@@ -1,2 +1,3 @@\n"
                " one\n"
                "-two\n"
                "+three\n"
                "+four\n")
    blocks = parse_patch(complete)
    assert len(blocks) == 1 and len(blocks[0]["hunks"]) == 1
    assert blocks[0]["hunks"][0]["old"] == 2 and blocks[0]["hunks"][0]["new"] == 3
    try:
        parse_patch(complete[:complete.index("+four\n")])
        raise AssertionError("a truncated patch was accepted")
    except ValueError as error:
        assert "truncated" in str(error)
    assert same_head("a141b7b371", "a141b7b371bb3e3d4")
    assert same_head("a141b7b371", "A141B7B371")
    assert not same_head("a141b7b371", "57ed1dae5d")
    assert not same_head("", "a141b7b371")
    print("OK: review packet checker self-test")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline checker for a review packet.")
    parser.add_argument("--root", default=str(Path(__file__).resolve().parent.parent),
                        help="repository the packet lives in")
    parser.add_argument("--packet", default=".lane-review/PR-1803-packet.json",
                        help="manifest of the packet")
    parser.add_argument("--head", default="HEAD", help="tree-ish that holds the current head")
    parser.add_argument("--previous", help="tree-ish of the previous head (default: the manifest)")
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--self-test", action="store_true", help="run the built-in check and exit")
    args = parser.parse_args(argv)
    if args.self_test:
        self_test()
        return 0
    root = Path(args.root).resolve()
    packet = root / args.packet
    if not packet.is_file():
        print("check-review-packet: %s is missing" % packet, file=sys.stderr)
        return 1
    try:
        manifest = json.loads(packet.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or manifest.get("schema") != SCHEMA:
            raise ValueError("the packet carries an unknown schema %r" % manifest.get("schema"))
        if args.previous:
            manifest = dict(manifest, previous_head=args.previous)
        results = check_packet(root, manifest, args.head)
    except (OSError, ValueError, KeyError, TypeError) as error:
        print("check-review-packet: %s" % error, file=sys.stderr)
        return 1
    code, head = git(root, "rev-parse", args.head)
    if args.as_json:
        print(json.dumps({"head": head.strip() if not code else "", "checks": results},
                         sort_keys=True, ensure_ascii=False, indent=2))
    else:
        if not code:
            print("head: %s" % head.strip())
        for row in results:
            print("%-14s %-4s %s" % (row["check"], "ok" if row["ok"] else "FAIL", row["detail"]))
    return 0 if all(row["ok"] for row in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
