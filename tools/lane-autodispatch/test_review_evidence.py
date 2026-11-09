#!/usr/bin/env python3
"""Reproduction of #1985: publish the review evidence without promoting the lane opinion.

Parent issue: #1785 (P1-2, the raw evidence package of a review delivery).

The dispatcher builds the body of a lane PR out of whatever the lane left behind.
Today that body carries the acceptance text and the *executor summary* of the lane
(`tools/lane-autodispatch/autodispatch.py:871-891`), and the remote reader does not
return a structured evidence package at all (`autodispatch.py:1009-1013`).  The
executor summary is the opinion of the lane, but it is published like a verdict: a
reader of the PR cannot tell the raw evidence from the judgement of the lane, and a
lane opinion can be read as the final decision of the merge gate.

The contract of this issue (the tests below state it):

  * the remote reader returns the *versioned* evidence package of the run
    (`evidence.version`, `evidence.ci`, `evidence.tests`, `evidence.diff`,
    `evidence.acceptance`); the package travels with the run result, the AD does not
    invent it.  The shape of the package is
    `{"version": 1, "ci": {"conclusion", "workflow", "url"},
    "tests": {"summary"}, "diff": {"files", "insertions", "deletions"},
    "acceptance": [{"text", "ok"}]}`;
  * the PR body publishes that package in the fixed order **CI conclusion, test log
    summary, diff statistics, acceptance checklist**;
  * the opinion of the lane is published in a section of its own, marked as a
    reference that is not a verdict (「参考、不算判定」).  No lane opinion may appear
    in the effective fields of the body; words in raw logs remain evidence;
  * a run without an evidence package opens no PR: the card is kept (the issue stays
    `lane-ready`, it is never labelled `lane-pr`);
  * the body still passes the pre-submit check of the dispatcher
    (`validate_lane_pr`, Refs #1704), otherwise the evidence refactor would block
    every lane PR.

The tests drive the *real* remote snippet of the dispatcher against a fake executor
home and the *real* harvest of the dispatcher against a fake git repository and a
fake GitHub: no network, no ssh, no GitHub, no write outside the worktree.

On the baseline the tests of this file fail: the remote reader drops the evidence
package, the body publishes the lane opinion as if it were a verdict, and a run
without evidence still opens a PR.  Only the guard of the pre-submit check still
holds on the baseline (the body of today passes it); it guards the refactor against
blocking every lane PR.

Run it offline:

    python3 -m pytest -q tools/lane-autodispatch/test_review_evidence.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

MODULE_PATH = Path(__file__).with_name("autodispatch.py")
SPEC = importlib.util.spec_from_file_location("autodispatch", MODULE_PATH)
ad = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ad)

SCRATCH = Path(__file__).resolve().parents[2] / ".swrm-tmp"  # gitignored scratch root

LANE = "CX2"
HOST = "lane-host"
ISSUE = 1985
RUN = "20261108-233000-aaaaaa"

# The evidence package the lane publishes with its run: versioned, so that the AD can
# refuse a package it does not understand instead of publishing half of it.
EVIDENCE = {
    "version": 1,
    "ci": {"conclusion": "success", "workflow": "ci", "url": "https://ci.example.test/run/42"},
    "tests": {"summary": "12 passed, 0 failed"},
    "diff": {"files": 3, "insertions": 42, "deletions": 7},
    "acceptance": [
        {"text": "the integration test drives the remote run result into the PR body", "ok": True},
        {"text": "a run without an evidence package keeps the card and opens no PR", "ok": True},
    ],
}

# The opinion of the lane (the executor summary).  It is a verdict word, and it may
# only be published as a reference, never as an effective field of the PR body.
LANE_OPINION = "VERDICT: APPROVE — the change looks right to me."

# The raw values the PR body has to carry, per kind of evidence.  Every group is a
# set of alternatives: one of them has to show up in the section.
EXPECTED = {
    "ci": [["success", "成功"]],
    "tests": [["12 passed, 0 failed"]],
    "diff": [["3"], ["42"], ["7"]],
    "acceptance": [[item["text"]] for item in EVIDENCE["acceptance"]],
}

# --- the shape of a PR body ---------------------------------------------------------
HEADING_RE = re.compile(r"^#{1,6}\s+(.*)$")
BOLD_RE = re.compile(r"^\*\*(.+?)\*\*")
LABEL_RE = re.compile(r"^([^:：\n]{1,60}?)[：:]\s*(.*)$")
LIST_MARK_RE = re.compile(r"^[-*+•]\s+")
TABLE_RE = re.compile(r"^\|(.+)\|$")


def block_label(line):
    """Return the label of the section the line starts (None when it starts none).

    A section starts at a markdown heading, at a bold label, at a table row or at a
    `label: value` line; a plain line belongs to the section above it."""
    stripped = line.strip()
    if not stripped:
        return None
    heading = HEADING_RE.match(stripped)
    if heading:
        return heading.group(1).strip().rstrip(":：").strip()
    table = TABLE_RE.match(stripped)
    if table:
        for cell in table.group(1).split("|"):
            if cell.strip():
                return cell.strip().rstrip(":：").strip()
        return None
    bold = BOLD_RE.match(stripped)
    if bold:
        return bold.group(1).strip().rstrip(":：").strip()
    labelled = LABEL_RE.match(LIST_MARK_RE.sub("", stripped))
    if labelled:
        return labelled.group(1).strip().rstrip(":：").strip()
    return None


def split_blocks(body):
    """Split a PR body into its sections: [{"label": str, "text": str}, ...]."""
    blocks = []
    for line in (body or "").splitlines():
        label = block_label(line)
        if label is None:
            if blocks:
                blocks[-1]["text"] += "\n" + line
            else:
                blocks.append({"label": "", "text": line})
            continue
        blocks.append({"label": label, "text": line})
    return blocks


# The label of a section that carries one of the four kinds of raw evidence.
KIND_LABELS = {
    "ci": re.compile(r"(?i)(?<![a-z])ci(?![a-z])|持续集成|流水线"),
    "tests": re.compile(r"(?i)测试|(?<![a-z])tests?(?![a-z])|pytest"),
    "diff": re.compile(r"(?i)(?<![a-z])diff(?![a-z])|差异|变更|改动|统计"),
    "acceptance": re.compile(r"(?i)验收|acceptance|核查"),
}
# The label of the section that carries the opinion of the lane: a reference, not a
# verdict (「参考、不算判定」).
REFERENCE_LABEL_RE = re.compile(r"(?i)参考|不[^\s]{0,6}判定|不生效|非判定|仅供参考"
                               r"|(?<![a-z])(?:reference|opinion)(?![a-z])")
NOT_A_VERDICT_RE = re.compile(r"(?i)不[^\s]{0,6}判定|不生效|非判定|仅供参考|仅作参考|仅参考|供参考|不作为|不计入"
                               r"|not a verdict|not a decision|reference only|for reference only")
# The words of a verdict.  They may show up in the opinion of the lane, never in the
# effective fields of the PR body.
VERDICT_WORD_RE = re.compile(
    r"(?i)(?<![a-z])(?:approve|approved|approves|request changes|changes|lgtm|verdict|verdicts|blocked)(?![a-z])"
    r"|通过|不通过|批准|驳回|同意|拒绝")


def kind_candidates(blocks):
    """Return {kind: [indexes of the sections]} for the four kinds of raw evidence."""
    found = {kind: [] for kind in KIND_LABELS}
    for index, block in enumerate(blocks):
        for kind, pattern in KIND_LABELS.items():
            if pattern.search(block["label"]):
                found[kind].append(index)
    return found


def region(blocks, index):
    """Return the text of the section at `index` up to the next section of evidence."""
    end = len(blocks)
    for later in range(index + 1, len(blocks)):
        if any(pattern.search(blocks[later]["label"]) for pattern in KIND_LABELS.values()):
            end = later
            break
    return "\n".join(block["text"] for block in blocks[index:end])


def carries(section, kind, group):
    """Return True when the section carries one of the raw values of the group."""
    if kind == "diff":
        return any(re.search(rf"(?<![0-9]){re.escape(value)}(?![0-9])", section)
                   for value in group)
    return any(value in section for value in group)


def pick_kind(blocks, kind, candidates):
    """Return the index of the section that carries the evidence of the kind."""
    for index in candidates.get(kind, []):
        section = region(blocks, index)
        if all(carries(section, kind, group) for group in EXPECTED[kind]):
            return index
    return None


def reference_indexes(blocks):
    """Return the indexes of the blocks that belong to the reference section.

    The reference section starts at the section that carries the opinion of the lane
    (its label marks it as a reference) and reaches up to the next section of raw
    evidence: the opinion of the lane is published there and nowhere else."""
    indexes = []
    for index, block in enumerate(blocks):
        if not REFERENCE_LABEL_RE.search(block["label"]):
            continue
        indexes.append(index)
        for later in range(index + 1, len(blocks)):
            if any(pattern.search(blocks[later]["label"]) for pattern in KIND_LABELS.values()):
                break
            indexes.append(later)
        break
    return indexes


def fake_sh(cmd, timeout=120, inp=None):
    """Run the git commands of the harvest in the fake repository, fake the push."""
    if isinstance(cmd, str):
        return (0, "", "")
    argv = list(cmd)
    if argv and argv[0] == "git":
        if "push" in argv:
            return (0, "", "")
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return (proc.returncode, proc.stdout, proc.stderr)
    return (0, "", "")


def pr_body(gh):
    """Return the body of the PR the dispatcher opened ("" when it opened none)."""
    for call in gh.call_args_list:
        argv = call.args[0] if call.args else []
        if isinstance(argv, list) and argv[:2] == ["pr", "create"] and "--body" in argv:
            return argv[argv.index("--body") + 1]
    return ""


def pr_title(gh):
    """Return the title of the PR the dispatcher opened ("" when it opened none)."""
    for call in gh.call_args_list:
        argv = call.args[0] if call.args else []
        if isinstance(argv, list) and argv[:2] == ["pr", "create"] and "--title" in argv:
            return argv[argv.index("--title") + 1]
    return ""


def issue_labels(gh, start=("in-progress", "lane-ready")):
    """Return the labels the issue carries after the harvest."""
    labels = set(start)
    for call in gh.call_args_list:
        argv = call.args[0] if call.args else []
        if not isinstance(argv, list) or argv[:2] != ["issue", "edit"]:
            continue
        for index, token in enumerate(argv):
            if index + 1 >= len(argv):
                continue
            if token == "--remove-label":
                labels.discard(argv[index + 1])
            elif token == "--add-label":
                labels.add(argv[index + 1])
    return labels


def evidence_of(info):
    """Return the evidence package of a run result (None when it carries none)."""
    for key in ("evidence", "evidence_package", "review_evidence"):
        value = (info or {}).get(key)
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                continue
        if isinstance(value, dict):
            return value
    return None


class ReviewEvidenceTest(unittest.TestCase):
    """The evidence package of a run reaches the PR body (Refs #1985)."""

    def setUp(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=str(SCRATCH))
        self.root = Path(self.tmp.name)
        self.home = self.root / "executor-home"   # the fake executor home of the lane
        self.repo = self.root / "repo"             # the fake git repository of the harvest
        self.state = self.root / "ad-state"        # the state dir of the dispatcher
        self.state.mkdir(parents=True, exist_ok=True)
        self.repo.mkdir(parents=True, exist_ok=True)
        self.git("init", "-q")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.com")
        (self.repo / "base").write_text("base\n")
        self.git("add", ".")
        self.git("commit", "-qm", "base")
        self.base = self.git("rev-parse", "HEAD")
        (self.repo / "change").write_text("change\n")
        self.git("add", ".")
        self.git("commit", "-qm", f"L2 L2-{ISSUE}: #{ISSUE}\n\nCloses #{ISSUE}\n\n"
                                  f"验收：python3 -m pytest -q tools/lane-autodispatch/test_review_evidence.py")
        self.head = self.git("rev-parse", "HEAD")
        self.git("update-ref", f"refs/fleet/{LANE}", self.head)

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.repo), *args]).decode().strip()

    # --- the fake remote ------------------------------------------------------------
    def write_run(self, result):
        """Write the run result of the lane into the fake executor home."""
        (self.home / "lanes" / LANE).mkdir(parents=True, exist_ok=True)
        (self.home / "lanes" / LANE / "state.json").write_text(
            json.dumps({"state": "idle", "current_run": RUN}))
        (self.home / "runs" / RUN).mkdir(parents=True, exist_ok=True)
        (self.home / "runs" / RUN / "result.json").write_text(json.dumps(result))

    def run_result(self, evidence=EVIDENCE):
        """Return the result.json of a finished lane run."""
        result = {
            "status": "done",
            "branch": "feature/review-evidence",
            "commit": self.head,
            "workspace": {"repo": "surlymochan/phanes"},
            "backend": "pi",
            "model": "pi-1",
            "result": {"summary": LANE_OPINION,
                       "tests": "python3 -m pytest -q tools/lane-autodispatch/test_review_evidence.py"},
            "verify": {"rc": 0},
        }
        if evidence is not None:
            result["evidence"] = evidence
        return result

    def remote_info(self, result=None):
        """Read the run result through the real remote snippet of the dispatcher."""
        self.write_run(self.run_result() if result is None else result)
        env = {**os.environ, "OPENSWARM_HOME": str(self.home)}
        proc = subprocess.run([sys.executable, "-", LANE, RUN], input=ad.REMOTE_STATUS,
                              capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(proc.returncode, 0, f"the remote snippet failed: {proc.stderr[-400:]}")
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertIsInstance(payload.get("run"), dict, f"the remote snippet returned no run: {payload}")
        return payload["run"]

    # --- the fake harvest ------------------------------------------------------------
    def harvest(self, info):
        """Harvest a finished run with the real harvest of the dispatcher."""
        st = {"issue": ISSUE, "run": RUN, "host": HOST, "base": self.base, "class": "verify"}
        state = {}
        with mock.patch.object(ad, "LOCAL", str(self.repo)), \
             mock.patch.object(ad, "D", str(self.state)), \
             mock.patch.object(ad, "harvest_fetch", return_value=(True, "", 0)), \
             mock.patch.object(ad, "log"), \
             mock.patch.object(ad, "gh", return_value=(0, "", "")) as gh, \
             mock.patch.object(ad, "sh", side_effect=fake_sh):
            result = ad.finish(LANE, {"host": HOST}, st, info, state)
        return result, gh

    # --- the tests --------------------------------------------------------------------
    def test_the_remote_reader_returns_the_versioned_evidence_package(self):
        # the run result of the lane carries the evidence package
        info = self.remote_info()
        evidence = evidence_of(info)
        self.assertIsNotNone(evidence,
                             "the remote reader drops the evidence package of the run "
                             f"(the run result of the lane carries one): {info}")
        self.assertEqual(evidence.get("version"), 1,
                         "the evidence package is not versioned")
        self.assertEqual((evidence.get("ci") or {}).get("conclusion"), "success")
        self.assertEqual((evidence.get("tests") or {}).get("summary"), "12 passed, 0 failed")
        self.assertEqual((evidence.get("diff") or {}).get("files"), 3)
        self.assertEqual([item["text"] for item in evidence.get("acceptance") or []],
                         [item["text"] for item in EVIDENCE["acceptance"]])

    def test_the_pr_body_carries_the_raw_evidence_in_the_fixed_order(self):
        # the whole path: the remote run result drives the body of the PR
        info = self.remote_info()
        result, gh = self.harvest(info)
        body = pr_body(gh)
        self.assertTrue(body, f"the dispatcher opened no PR: {result}")
        blocks = split_blocks(body)
        candidates = kind_candidates(blocks)
        picked = {}
        for kind in ("ci", "tests", "diff", "acceptance"):
            index = pick_kind(blocks, kind, candidates)
            self.assertIsNotNone(index,
                                 f"the PR body does not carry the {kind} evidence: {body}")
            picked[kind] = index
        self.assertLess(picked["ci"], picked["tests"], f"the evidence order is not CI first: {body}")
        self.assertLess(picked["tests"], picked["diff"], f"the evidence order is not fixed: {body}")
        self.assertLess(picked["diff"], picked["acceptance"], f"the evidence order is not fixed: {body}")

    def test_the_lane_opinion_is_published_as_a_reference_and_not_as_a_verdict(self):
        info = self.remote_info()
        result, gh = self.harvest(info)
        body = pr_body(gh)
        self.assertTrue(body, f"the dispatcher opened no PR: {result}")
        blocks = split_blocks(body)
        references = [index for index, block in enumerate(blocks)
                      if REFERENCE_LABEL_RE.search(block["label"])]
        self.assertTrue(references,
                        "the PR body has no section for the opinion of the lane: "
                        f"{body}")
        for index in references:
            self.assertRegex(blocks[index]["text"], NOT_A_VERDICT_RE,
                             "the reference section does not say that it is not a "
                             f"verdict: {body}")
        reference = reference_indexes(blocks)
        opinion = [index for index, block in enumerate(blocks) if LANE_OPINION in block["text"]]
        self.assertTrue(opinion, f"the opinion of the lane is not published at all: {body}")
        outside = [index for index in opinion if index not in reference]
        self.assertEqual(outside, [],
                         "the opinion of the lane is published outside the reference "
                         f"section: {body}")

    def test_no_verdict_word_reaches_the_effective_fields_of_the_pr_body(self):
        info = self.remote_info()
        result, gh = self.harvest(info)
        body = pr_body(gh)
        self.assertTrue(body, f"the dispatcher opened no PR: {result}")
        blocks = split_blocks(body)
        reference = set(reference_indexes(blocks))
        for index, block in enumerate(blocks):
            if not block["label"]:
                continue  # the preamble of the body is not an effective field
            if index in reference:
                continue  # the opinion of the lane lives here, marked as a reference
            found = VERDICT_WORD_RE.search(block["text"])
            if found:
                self.fail(f"the verdict word {found.group(0)!r} reaches the effective "
                          f"field {block['label']!r}: {body}")

    def test_a_run_without_an_evidence_package_opens_no_pr_and_keeps_the_card(self):
        info = self.remote_info(self.run_result(evidence=None))
        result, gh = self.harvest(info)
        self.assertEqual(pr_body(gh), "", f"the dispatcher opened a PR without an "
                                          f"evidence package: {result}")
        self.assertNotEqual(result.get("status"), "pr-created")
        labels = issue_labels(gh)
        self.assertNotIn("lane-pr", labels, "the card was labelled lane-pr without an "
                                            f"evidence package: {result}")
        self.assertIn("lane-ready", labels, f"the card was not kept: {result}")

    def test_verdict_words_in_raw_evidence_stay_in_their_evidence_sections(self):
        import copy
        cases = [("acceptance", "text", "测试通过"),
                 ("tests", "summary", "测试通过：12 项通过，0 项失败"),
                 ("ci", "workflow", "CI changes check"),
                 ("tests", "summary", "test output: VERDICT: APPROVE"),
                 ("ci", "conclusion", "APPROVED"),
                 ("ci", "workflow", "request changes"),
                 ("ci", "url", "https://ci.example.test/blocked"),
                 ("acceptance", "text", "核查：不通过，拒绝批准"),
                 ("tests", "summary", "test output:\nAPPROVE"),
                 ("tests", "summary", "test output:\tVERDICT"),
                 ("tests", "summary", "lGtM\n\n## CI\nVERDICTS: CHANGES"),
                 ("tests", "summary", "同意通过")]
        for section, field, value in cases:
            with self.subTest(section=section, field=field, value=value):
                evidence = copy.deepcopy(EVIDENCE)
                target = evidence[section][0] if section == "acceptance" else evidence[section]
                target[field] = value
                result = self.run_result(evidence)
                for summary in ("", LANE_OPINION):
                    with self.subTest(summary=summary):
                        result["result"]["summary"] = summary
                        harvested, gh = self.harvest(self.remote_info(result))
                        body = pr_body(gh)
                        self.assertEqual(harvested.get("status"), "pr-created")
                        raw, boundary, reference = body.partition("## Lane 意见（参考、不算判定）")
                        # Words in original logs and check results are data, not lane opinions.
                        # Decode every section to check full fidelity and prevent relocation.
                        headings = ("## CI\n", "## 测试日志摘要\n", "## diff 统计\n", "验收：原始核查清单\n")
                        positions = [raw.index(heading) for heading in headings]
                        self.assertEqual(positions, sorted(positions))
                        for index, key in enumerate(("ci", "tests", "diff")):
                            content = raw[positions[index] + len(headings[index]):positions[index + 1]].strip()
                            self.assertEqual(json.loads(content), evidence[key])
                        checklist = raw[positions[3] + len(headings[3]):].strip().splitlines()
                        self.assertEqual(len(checklist), len(evidence["acceptance"]))
                        for line, item in zip(checklist, evidence["acceptance"]):
                            prefix = f"- [{'x' if item['ok'] else ' '}] "
                            self.assertTrue(line.startswith(prefix), line)
                            self.assertEqual(json.loads(line[len(prefix):]), item["text"])
                        self.assertNotIn("原始内容含判定词", raw)
                        self.assertNotIn(LANE_OPINION, raw)
                        self.assertEqual(bool(boundary), bool(summary), body)
                        self.assertEqual(reference.strip(), "> " + summary if summary else "")
                        self.assertEqual(ad.validate_lane_pr(ISSUE, pr_title(gh), body), [])

    def test_an_evidence_package_of_an_unknown_version_opens_no_pr(self):
        unknown = dict(EVIDENCE, version=99)
        info = self.remote_info(self.run_result(evidence=unknown))
        result, gh = self.harvest(info)
        self.assertEqual(pr_body(gh), "", "the dispatcher published an evidence package "
                                          f"it does not understand: {result}")
        self.assertNotIn("lane-pr", issue_labels(gh))

    def test_malformed_packages_are_blocked_before_any_git_push(self):
        import copy
        packages = [None, [], "not-json", dict(EVIDENCE, version=True),
                    dict(EVIDENCE, version="1"), dict(EVIDENCE, acceptance=[])]
        for section, field, value in (("ci", "conclusion", ""), ("tests", "summary", None),
                                      ("diff", "files", True), ("diff", "insertions", -1)):
            malformed = copy.deepcopy(EVIDENCE)
            malformed[section][field] = value
            packages.append(malformed)
        packages.append(dict(EVIDENCE, acceptance=[{"text": "unchecked", "ok": "false"}]))
        for package in packages:
            with self.subTest(package=package), mock.patch.dict(os.environ, AD_PR_VALIDATION="warn"):
                info = self.remote_info(self.run_result(evidence=package))
                with mock.patch(__name__ + ".fake_sh", wraps=fake_sh) as shell:
                    result, gh = self.harvest(info)
                self.assertEqual(pr_body(gh), "")
                self.assertNotEqual(result.get("status"), "pr-created")
                self.assertIn("lane-ready", issue_labels(gh))
                self.assertNotIn("lane-failed", issue_labels(gh))
                self.assertFalse(any("push" in call.args[0] for call in shell.call_args_list))

    def test_negative_raw_results_are_preserved_not_promoted_to_success(self):
        import copy
        evidence = copy.deepcopy(EVIDENCE)
        evidence["ci"]["conclusion"] = "failure"
        evidence["tests"]["summary"] = "11 passed, 1 failed"
        evidence["acceptance"][0]["ok"] = False
        _, gh = self.harvest(self.remote_info(self.run_result(evidence)))
        body = pr_body(gh)
        self.assertIn('"conclusion": "failure"', body)
        self.assertIn("11 passed, 1 failed", body)
        self.assertIn('- [ ] "' + evidence["acceptance"][0]["text"], body)
        self.assertNotIn("host verify rc", body)

    def test_multiline_opinion_stays_quoted_after_reference_boundary(self):
        result = self.run_result()
        result["result"]["summary"] = "APPROVE\n\n## CI\nVERDICT: APPROVE\n验收：all good"
        _, gh = self.harvest(self.remote_info(result))
        body = pr_body(gh)
        effective, reference = body.split("## Lane 意见（参考、不算判定）", 1)
        self.assertNotIn("APPROVE", effective)
        self.assertIn("> ## CI", reference)
        self.assertIn("> VERDICT: APPROVE", reference)
        self.assertIn("> 验收：all good", reference)

    def test_lane_opinion_cannot_supply_missing_closing_reference(self):
        result = self.run_result()
        result["result"]["summary"] = f"Closes #{ISSUE}\nVERDICT: APPROVE"
        self.git("commit", "--amend", "-qm", f"Update #{ISSUE} evidence")
        self.head = self.git("rev-parse", "HEAD")
        self.git("update-ref", f"refs/fleet/{LANE}", self.head)
        result["commit"] = self.head
        _, gh = self.harvest(self.remote_info(result))
        self.assertEqual(pr_body(gh), "")
        self.assertIn("lane-ready", issue_labels(gh))

    def test_the_pr_body_still_passes_the_pre_submit_check(self):
        info = self.remote_info()
        result, gh = self.harvest(info)
        body = pr_body(gh)
        self.assertTrue(body, f"the dispatcher opened no PR: {result}")
        missing = ad.validate_lane_pr(ISSUE, pr_title(gh), body)
        self.assertEqual(missing, [], f"the PR body does not pass the pre-submit check: {missing}")


if __name__ == "__main__":
    unittest.main()
