import hashlib
import importlib.util
import inspect
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.evalid_roles_control import (LocalEvidence, MockSwrm, NativeEvidence, Rejected, RoleController,
                                        RECORDED_REJECTIONS, UNRECORDED_JUSTIFICATIONS, UNRECORDED_REJECTIONS,
                                        _tree_snapshot_digest, digest)


ROOT = Path(__file__).resolve().parent
FIXTURES = ROOT / "fixtures" / "evalid-roles"
POLICY = "sha256:" + "a" * 64


class MockReviewRun:
    def __init__(self):
        self.calls = []

    def review(self, context):
        self.calls.append(context)
        return context["review"]["verdict"]


class TestEvalidRolesControl(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="evalid-roles-")
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self._git("init", "-q")
        self._git("config", "user.name", "Fixture Author")
        self._git("config", "user.email", "fixture@example.invalid")
        (self.project / "app.py").write_text("def value():\n    return 0\n")
        self._git("add", ".")
        self._git("commit", "-qm", "base")
        self.base = self._git("rev-parse", "HEAD")
        (self.project / "tests").mkdir()
        (self.project / "tests" / "test_app.py").write_text(
            "import unittest\nfrom app import value\n\n"
            "class ValueTest(unittest.TestCase):\n"
            "    def test_value(self):\n        self.assertEqual(value(), 1)\n")
        self._git("add", "tests/test_app.py")
        self._git("commit", "-qm", "red test")
        self.red_head = self._git("rev-parse", "HEAD")
        red = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests"],
                             cwd=self.project, capture_output=True, text=True,
                             env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
        self.assertEqual(red.returncode, 1)
        self.assertIn("AssertionError", red.stderr)
        self.test_manifest = {"tests/test_app.py": self._file_digest("tests/test_app.py")}
        (self.project / "app.py").write_text("def value():\n    return 1\n")
        shutil.rmtree(self.project / "__pycache__", ignore_errors=True)
        self._git("add", "app.py")
        self._git("commit", "-qm", "implementation")
        self.green_head = self._git("rev-parse", "HEAD")
        green = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests"],
                               cwd=self.project, capture_output=True, text=True,
                               env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
        self.assertEqual(green.returncode, 0, green.stderr)
        self.controller = self._controller()
        self.controller.freeze_intake("offline-demo", issue_ref="issue:demo", base_commit=self.base,
            policy_digest=POLICY, public_scope={"allowed_files": ["app.py", "tests/**"]},
            holdout_required=True, holdout_suite=self._holdout_suite())

    def tearDown(self):
        self.temp.cleanup()

    def _git(self, *args):
        # The fixture commits with an explicit identity, so the tests never
        # depend on the identity of the machine that runs them.
        env = dict(os.environ, GIT_AUTHOR_NAME="Fixture Author",
                   GIT_AUTHOR_EMAIL="fixture@example.invalid",
                   GIT_COMMITTER_NAME="Fixture Author",
                   GIT_COMMITTER_EMAIL="fixture@example.invalid")
        result = subprocess.run(["git", "-C", str(self.project), *args], check=True,
                                capture_output=True, text=True, env=env)
        return result.stdout.strip()

    def _file_digest(self, relative):
        return "sha256:" + hashlib.sha256((self.project / relative).read_bytes()).hexdigest()

    def _holdout_suite(self):
        return json.loads((FIXTURES / "private-holdout.json").read_text())

    def _controller(self, control_dir=None):
        responses = json.loads((FIXTURES / "mock-swrm.json").read_text())
        return RoleController(control_dir or self.root / "control", self.project,
            swrm=MockSwrm(responses), evidence=LocalEvidence(), review_run=MockReviewRun())

    def _accept_through_red(self, controller=None, change_id="offline-demo"):
        controller = controller or self.controller
        self._git("checkout", "-q", "--detach", self.red_head)
        controller.accept(change_id, "spec", {"status": "PASS", "spec_digest": digest({"spec": 1})})
        controller.accept(change_id, "spec-check", {"verdict": "PASS", "blockers": []})
        controller.accept(change_id, "red", {
            "head_commit": self.red_head,
            "command": [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
            "snapshot_digest": _tree_snapshot_digest(self.project, self.red_head),
            "test_manifest": self.test_manifest})
        return controller

    def _accept_through_green(self, controller=None, change_id="offline-demo"):
        controller = self._accept_through_red(controller, change_id)
        self._git("checkout", "-q", "--detach", self.green_head)
        controller.accept(change_id, "green", {
            "head_commit": self.green_head, "status": "PASS",
            "command": [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
            "proof_digest": digest("green proof"),
            "snapshot_digest": _tree_snapshot_digest(self.project, self.green_head),
            "test_manifest": self.test_manifest})
        return controller

    def _make_review(self, controller, receipt, change_id="offline-demo"):
        state = controller.state(change_id)
        return {"reviewed_base": self.base, "reviewed_head": self.green_head,
                "commits": self._git("rev-list", "--reverse", f"{self.base}..{self.green_head}").splitlines(),
                "verdict": "PASS", "findings": [], "holdout_receipt": receipt["digest"],
                "spec_digest": state["receipts"]["spec"]["spec_digest"],
                "tdd_digest": state["receipts"]["green"]["evidence_digest"]}

    def _holdout_and_review(self, controller=None, change_id="offline-demo"):
        controller = controller or self.controller
        receipt = controller.run_holdout(change_id)
        review = self._make_review(controller, receipt, change_id)
        controller.accept(change_id, "review", review)
        return receipt, review

    def _native_evidence(self):
        repository = ROOT.parents[1]
        branch = subprocess.run(["git", "rev-parse", "--verify", "handoff-framework^{commit}"],
                                cwd=repository, capture_output=True, text=True)
        if branch.returncode:
            self.skipTest("handoff-framework branch unavailable; real tools contract cannot be loaded")
        tools = self.root / "native tools"
        tools.mkdir()
        for name in ("evidence.py", "review_run.py"):
            source = subprocess.run(["git", "show", f"handoff-framework:tools/{name}"],
                                    cwd=repository, capture_output=True, check=True)
            path = tools / name
            path.write_bytes(source.stdout)
            spec = importlib.util.spec_from_file_location("contract_" + path.stem, path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            # Both real entry points take argv through argparse, not injected
            # replay/holdout/review Python methods. CLI calls below test flags.
            inspect.signature(module.main).bind()
            if name == "review_run.py":
                self.assertEqual(module.EVIDENCE, tools / "evidence.py")
        ev = self.root / "native evalid"
        atoms = ev / "xflow" / "atoms"
        shutil.copytree(FIXTURES / "native-atoms", atoms,
                        ignore=shutil.ignore_patterns("__pycache__"))
        for args in (("init", "-q"), ("config", "user.name", "Fixture Author"),
                     ("config", "user.email", "fixture@example.invalid"),
                     ("add", "."), ("commit", "-qm", "offline atoms")):
            subprocess.run(["git", "-C", str(ev), *args], check=True, capture_output=True)
        return NativeEvidence(self.root / "native control", ev, tools, change_id="offline-demo")

    def _native_suite(self, assertion="self.assertEqual(value(), 1)"):
        return {"argv": [sys.executable, "{holdout}/test_hidden.py"], "timeout_s": 30,
                "canary": "PRIVATE_HOLDOUT_CANARY_7f2c9",
                "files": {"test_hidden.py":
                    "import sys, unittest\nfrom pathlib import Path\n"
                    "sys.path.insert(0, str(Path.cwd()))\nfrom app import value\n"
                    "# PRIVATE_HOLDOUT_CANARY_7f2c9\n"
                    "class Hidden(unittest.TestCase):\n"
                    f"    def test_value(self): {assertion}\n"
                    "unittest.main()\n"}}

    def test_real_tools_contract_red_green_and_private_posthoc(self):
        adapter = self._native_evidence()
        self.controller._evidence = adapter
        with patch.object(adapter, "_invoke", wraps=adapter._invoke) as calls:
            self._accept_through_green()
            # A fresh adapter can verify the sealed green bundle after restart.
            recovered = NativeEvidence(adapter.root, adapter.evalid, adapter.tools,
                                       change_id="offline-demo")
            recovered.replay("green", self.project,
                [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
                expected_rc=0, commit=self.green_head,
                expected_snapshot_digest=_tree_snapshot_digest(self.project, self.green_head))
            # Freeze a separate controller's private files before running the
            # native chain; no suite mutation after intake is permitted.
            native = self._controller(self.root / "native-events")
            native._evidence = adapter
            native.freeze_intake("offline-demo", issue_ref="issue:demo", base_commit=self.base,
                policy_digest=POLICY, public_scope={"allowed_files": ["app.py", "tests/**"]},
                holdout_required=True, holdout_suite=self._native_suite())
            self._accept_through_green(native)
            receipt = native.run_holdout("offline-demo")
        self.assertEqual((receipt["status"], receipt["isolation"], receipt["l1_eligible"]),
                         ("PASS", "unisolated", False))
        tdd = json.loads((adapter.root / "tdd" / "record.json").read_text())
        self.assertEqual(set(tdd["phases"]), {"red", "green"})
        for phase in ("red", "green"):
            self.assertTrue((adapter.root / "tdd" / phase / f"{phase}-0.json").is_file())
            self.assertTrue((adapter.root / "tdd" / phase / "snapshot" / "app.py").is_file())
        out = adapter.root / "holdout"
        raw = json.loads((out / "result.json").read_text())
        self.assertEqual((raw["classification"], raw["replay"]["ok"]), ("posthoc", True))
        self.assertEqual(raw["holdout"]["exit"], 0)
        for label in ("visible", "holdout"):
            record = json.loads((out / (adapter.tool_change + "-acceptance-" + label) / "record.json").read_text())
            self.assertEqual(set(record["phases"]), {"posthoc"})
        archived = out / (adapter.tool_change + "-acceptance-holdout") / "holdout-files" / "test_hidden.py"
        self.assertIn("PRIVATE_HOLDOUT_CANARY_7f2c9", archived.read_text())
        review_call = next(call for call in calls.call_args_list if call.args[0] == "review_run.py")
        args = review_call.args[1]
        self.assertEqual(args[args.index("--phase") + 1], "posthoc")
        for flag in ("--holdout-dir", "--cmd-holdout", "--cmd-visible", "--evidence"):
            self.assertIn(flag, args)
        review = self._make_review(native, receipt)
        native.accept("offline-demo", "review", review)
        self.assertNotIn("PRIVATE_HOLDOUT_CANARY_7f2c9", json.dumps(native.public_state("offline-demo")))
        self.assertNotIn(str(adapter.root), json.dumps(native.public_state("offline-demo")))
        with self.assertRaisesRegex(Rejected, "holdout_isolation"):
            native.deliver("offline-demo", review)
        sealed = (out / "result.json").read_bytes()
        retried = adapter.holdout(self.project, self._native_suite(),
            {"change_id": "offline-demo", "attempt": 0, "head_commit": self.green_head})
        self.assertEqual(retried["status"], "PASS")
        self.assertEqual((out / "result.json").read_bytes(), sealed)
        self.assertEqual(len(list(adapter.root.glob("holdout-retry-*/result.json"))), 1)

    def test_real_review_run_exit_and_verdict_contract(self):
        adapter = self._native_evidence()
        self.controller._evidence = adapter
        self._accept_through_green()
        for expected in ("FAIL", "INFRA"):
            with self.subTest(verdict=expected):
                # Separate immutable posthoc evidence for each execution.
                separate = NativeEvidence(adapter.root / expected, adapter.evalid, adapter.tools,
                                          change_id="offline-demo")
                shutil.copytree(adapter.root / "tdd", separate.root / "tdd")
                suite = self._native_suite("self.fail()")
                if expected == "INFRA":
                    suite["files"]["test_hidden.py"] = "import unittest\nunittest.main()\n"
                result = separate.holdout(self.project, suite,
                    {"change_id": "offline-demo", "attempt": 0, "head_commit": self.green_head})
                self.assertEqual((result["status"], result["exit_code"]), (expected, 1))
                self.assertFalse(result["private_result"]["replay"]["ok"])

    def test_real_evidence_rejects_green_without_red(self):
        adapter = self._native_evidence()
        code, result = adapter._invoke("evidence.py", ["capture", "--project", str(self.project),
            "--evalid", str(adapter.evalid), "--evidence", str(adapter.root / "missing-red"),
            "--change-id", adapter.tool_change, "--command", "python3 -m unittest discover -s tests",
            "--phase", "green"])
        self.assertEqual((code, result["ok"], result["error"]),
                         (1, False, "red_required_use_posthoc"))

    def test_offline_red_green_and_mocked_full_chain_without_required_holdout(self):
        controller = self._controller(self.root / "control-no-holdout")
        controller.freeze_intake("offline-no-holdout", issue_ref="issue:demo", base_commit=self.base,
            policy_digest=POLICY, public_scope={"allowed_files": ["app.py", "tests/**"]},
            holdout_required=False)
        self._accept_through_green(controller, "offline-no-holdout")
        receipt, review = self._holdout_and_review(controller, "offline-no-holdout")
        delivered = controller.deliver("offline-no-holdout", review)
        self.assertEqual(delivered["controller_status"], "VERIFIED")
        self.assertEqual(receipt["status"], "NOT_APPLICABLE")
        self.assertFalse(receipt["l1_eligible"])
        self.assertEqual(controller.public_state("offline-no-holdout")["next"], "complete")
        self.assertEqual(len(controller._review_run.calls), 1)

    def test_duplicate_event_is_idempotent(self):
        evidence = {"status": "PASS", "spec_digest": digest({"spec": 1})}
        first = self.controller.accept("offline-demo", "spec", evidence)
        again = self.controller.accept("offline-demo", "spec", evidence)
        self.assertEqual(first, again)
        self.assertEqual(len(self.controller._events), 2)  # intake plus one spec receipt

    def test_crash_recovery_resumes_from_durable_receipts(self):
        self.controller.accept("offline-demo", "spec", {"status": "PASS", "spec_digest": digest("spec")})
        recovered = self._controller()
        self.assertEqual(recovered.public_state("offline-demo")["next"], "spec-check")
        recovered.accept("offline-demo", "spec-check", {"verdict": "PASS", "blockers": []})
        self.assertEqual(self._controller().public_state("offline-demo")["next"], "red")

    def test_missing_evidence_is_rejected(self):
        with self.assertRaisesRegex(Rejected, "missing_evidence"):
            self.controller.accept("offline-demo", "spec", {})

    def test_incomplete_swrm_run_cannot_accept_evidence(self):
        evidence = {"status": "PASS", "spec_digest": digest("spec")}
        before = self.controller._log.read_bytes()
        for status in ("queued", "running", "partial", "failed", None, "accepted"):
            with self.subTest(status=status):
                self.controller._swrm.responses["spec"]["status"] = status
                with self.assertRaisesRegex(Rejected, "run_not_complete"):
                    self.controller.accept("offline-demo", "spec", evidence)
                self.assertEqual(self.controller._log.read_bytes(), before)
                self.assertEqual(self._controller().public_state("offline-demo")["next"], "spec")
        self.controller._swrm.responses["spec"]["status"] = "done"
        self.controller.accept("offline-demo", "spec", evidence)
        self.assertEqual(self._controller().public_state("offline-demo")["next"], "spec-check")

    def test_accepted_swrm_run_does_not_replay_red(self):
        self._git("checkout", "-q", "--detach", self.red_head)
        self.controller.accept("offline-demo", "spec", {"status": "PASS", "spec_digest": digest("spec")})
        self.controller.accept("offline-demo", "spec-check", {"verdict": "PASS", "blockers": []})
        evidence = {"head_commit": self.red_head,
                    "command": [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
                    "snapshot_digest": _tree_snapshot_digest(self.project, self.red_head),
                    "test_manifest": self.test_manifest}
        self.controller._swrm.responses["red"]["status"] = "accepted"
        before = self.controller._log.read_bytes()
        with patch.object(self.controller._evidence, "replay") as replay:
            with self.assertRaisesRegex(Rejected, "run_not_complete"):
                self.controller.accept("offline-demo", "red", evidence)
            replay.assert_not_called()
        self.assertEqual(self.controller._log.read_bytes(), before)
        self.assertEqual(self.controller.public_state("offline-demo")["next"], "red")

    def test_head_drift_invalidates_delivery(self):
        self._accept_through_green()
        receipt, review = self._holdout_and_review()
        (self.project / "drift.txt").write_text("drift\n")
        self._git("add", "drift.txt")
        self._git("commit", "-qm", "unreviewed drift")
        with self.assertRaisesRegex(Rejected, "head_drift"):
            self.controller.deliver("offline-demo", review)

    def test_green_rejects_uncommitted_source_and_test_changes(self):
        self._accept_through_red()
        self._git("checkout", "-q", "--detach", self.green_head)
        (self.project / "app.py").write_text("def value():\n    return 2\n")
        (self.project / "tests" / "test_app.py").write_text(
            "import unittest\nfrom app import value\n\n"
            "class ValueTest(unittest.TestCase):\n"
            "    def test_value(self):\n        self.assertEqual(value(), 2)\n")
        with self.assertRaisesRegex(Rejected, "dirty_worktree"):
            self.controller.accept("offline-demo", "green", {
                "head_commit": self.green_head, "status": "PASS",
                "command": [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
                "proof_digest": digest("green proof"),
                "snapshot_digest": _tree_snapshot_digest(self.project, self.green_head),
                "test_manifest": self.test_manifest})

    def test_green_rejects_unbound_snapshot_digest(self):
        self._accept_through_red()
        self._git("checkout", "-q", "--detach", self.green_head)
        with self.assertRaisesRegex(Rejected, "snapshot_mismatch"):
            self.controller.accept("offline-demo", "green", {
                "head_commit": self.green_head, "status": "PASS",
                "command": [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
                "proof_digest": digest("green proof"), "snapshot_digest": digest("unbound snapshot"),
                "test_manifest": self.test_manifest})

    def test_every_stage_rejection_is_recorded_in_the_attempt_history(self):
        # Not one code at a time: any refusal of the awaited stage lands in the history.
        self._accept_through_red()
        self._git("checkout", "-q", "--detach", self.green_head)
        with self.assertRaisesRegex(Rejected, "snapshot_mismatch"):
            self.controller.accept("offline-demo", "green", {
                "head_commit": self.green_head, "status": "PASS",
                "command": [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
                "proof_digest": digest("green proof"), "snapshot_digest": digest("unbound snapshot"),
                "test_manifest": self.test_manifest})
        item = self.controller._attempt_history("offline-demo")[0]
        self.assertEqual(item["stages"][-1], "green")
        self.assertEqual((item["terminal"], item["rejection_reason"]), ("REJECTED", "GREEN_REJECTED"))
        self.controller._validate_attempt_history("offline-demo", [item])

    def test_every_rejection_code_is_classified_as_recorded_or_not(self):
        source = inspect.getsource(sys.modules[Rejected.__module__])
        raised = set(re.findall(r'Rejected\("([a-z_]+)"\)', source))
        self.assertFalse(RECORDED_REJECTIONS & UNRECORDED_REJECTIONS)
        self.assertEqual(raised, RECORDED_REJECTIONS | UNRECORDED_REJECTIONS)

    def test_every_unrecorded_code_carries_a_written_reason(self):
        # An UNRECORDED code stays out of the attempt history only because it is a
        # precondition or a malformed call, never a verdict on the evidence: the
        # written reason says which one it is, for every code.
        self.assertEqual(set(UNRECORDED_JUSTIFICATIONS), set(UNRECORDED_REJECTIONS))
        for code, reason in sorted(UNRECORDED_JUSTIFICATIONS.items()):
            with self.subTest(code=code):
                self.assertTrue(reason.startswith(("precondition: ", "malformed call: ")), reason)
                self.assertGreater(len(reason.split(": ", 1)[1]), 20)

    def test_delivery_rejects_caller_supplied_short_commit_chain(self):
        controller = self._controller(self.root / "control-short-chain")
        controller.freeze_intake("offline-short-chain", issue_ref="issue:demo", base_commit=self.base,
            policy_digest=POLICY, public_scope={"allowed_files": ["app.py", "tests/**"]},
            holdout_required=False)
        self._accept_through_green(controller, "offline-short-chain")
        receipt = controller.run_holdout("offline-short-chain")
        review = self._make_review(controller, receipt, "offline-short-chain")
        review["commits"] = review["commits"][-1:]
        controller.accept("offline-short-chain", "review", review)
        with self.assertRaisesRegex(Rejected, "commit_chain_mismatch"):
            controller.deliver("offline-short-chain", review, commits=review["commits"])

    def test_caller_cannot_supply_holdout_result(self):
        self._accept_through_green()
        with self.assertRaises(TypeError):
            self.controller.run_holdout("offline-demo", result={"status": "PASS", "isolation": "verified"})

    def test_holdout_runner_failure_does_not_issue_receipt(self):
        class RaisingEvidence(LocalEvidence):
            def holdout(self, project, private_suite, context):
                raise RuntimeError("runner failed")

        self._accept_through_green()
        self.controller._evidence = RaisingEvidence()
        before = len(self.controller._events)
        with self.assertRaisesRegex(Rejected, "holdout_runner_failed"):
            self.controller.run_holdout("offline-demo")
        self.assertEqual(len(self.controller._events), before)
        state = self.controller.public_state("offline-demo")
        self.assertEqual(state["next"], "holdout")
        self.assertNotIn("holdout", state["receipts"])

    def test_unverified_holdout_is_rejected(self):
        self._accept_through_green()
        receipt, review = self._holdout_and_review()
        self.assertEqual(receipt["status"], "PASS")
        self.assertEqual(receipt["isolation"], "unisolated")
        self.assertFalse(receipt["l1_eligible"])
        with self.assertRaisesRegex(Rejected, "holdout_isolation"):
            self.controller.deliver("offline-demo", review)

    def test_review_canary_is_rejected_and_never_public(self):
        self._accept_through_green()
        receipt = self.controller.run_holdout("offline-demo")
        review = self._make_review(self.controller, receipt)
        review["holdout_debug"] = "PRIVATE_HOLDOUT_CANARY_7f2c9"
        with self.assertRaisesRegex(Rejected, "review_field_unknown"):
            self.controller.accept("offline-demo", "review", review)
        review.pop("holdout_debug")
        review["findings"] = [{"severity": "FOLLOW-UP", "reason": "PRIVATE_HOLDOUT_CANARY_7f2c9"}]
        with self.assertRaisesRegex(Rejected, "private_evidence_leak"):
            self.controller.accept("offline-demo", "review", review)
        public = json.dumps(self.controller.public_state("offline-demo"))
        self.assertNotIn("PRIVATE_HOLDOUT_CANARY_7f2c9", public)

    def test_holdout_canary_is_private(self):
        self._accept_through_green()
        receipt, review = self._holdout_and_review()
        public = json.dumps({"receipt": receipt, "state": self.controller.public_state("offline-demo"),
                             "review": review})
        self.assertNotIn("PRIVATE_HOLDOUT_CANARY_7f2c9", public)
        self.assertIn("PRIVATE_HOLDOUT_CANARY_7f2c9", self.controller._log.read_text())

    def test_review_rejects_all_private_suite_content_in_findings(self):
        self._accept_through_green()
        receipt = self.controller.run_holdout("offline-demo")
        review = self._make_review(self.controller, receipt)
        suite = self._holdout_suite()
        path, source = next(iter(suite["files"].items()))
        leaks = (suite["argv"][3], path, Path(path).name, "test_reserved_boundary_case",
                 source, source.splitlines()[1].strip(), "reserved boundary sample")
        before = self.controller._log.read_bytes()
        for field, leak in [("reason", leak) for leak in leaks] + [
                ("requirement_ref", suite["argv"][0]), ("artifact_ref", path),
                ("path", path), ("line", 739281)]:
            with self.subTest(field=field, leak=leak):
                finding = {"severity": "FOLLOW-UP", "reason": "Details available"}
                finding[field] = "Details: " + leak if field == "reason" else leak
                review["findings"] = [finding]
                with self.assertRaisesRegex(Rejected, "private_evidence_leak"):
                    self.controller.accept("offline-demo", "review", review)
                self.assertEqual(self.controller._log.read_bytes(), before)
                self.assertNotIn(str(leak), json.dumps(self.controller.public_state("offline-demo")))

    def test_public_boundary_scrubs_all_fields_and_survives_recovery(self):
        leak = "test_reserved_boundary_case"
        # Intake, producer metadata, arbitrary nested evidence and dictionary
        # keys all cross the same boundary, including duplicate returns.
        controller = self._controller(self.root / "control-public-boundary")
        for _ in range(2):
            intake = controller.freeze_intake("offline-demo", issue_ref="issue:demo", base_commit=self.base,
                policy_digest=POLICY, public_scope={"allowed_files": ["app.py", "tests/**"]},
                holdout_required=True, holdout_suite=self._holdout_suite(), budget={"note": leak})
            self.assertNotIn(leak, json.dumps(intake))
        controller._swrm.responses["spec"]["model"] = leak
        controller._swrm.responses["spec"]["run_id"] = leak
        evidence = {"status": "PASS", "spec_digest": digest("spec"),
                    "notes": [{"text": "ordinary unpublished diagnostic"}]}
        first = controller.accept("offline-demo", "spec", evidence)
        duplicate = controller.accept("offline-demo", "spec", evidence)
        recovered = self._controller(controller.control_dir)
        for public in (first, duplicate, recovered.public_state("offline-demo")):
            self.assertNotIn(leak, json.dumps(public))
        self.assertEqual(first, duplicate)
        self.assertEqual(first["run_id"], digest(leak))
        self.assertIn(leak, controller._log.read_text())
        self.assertNotIn("notes", first)

    def test_private_fragments_rejected_at_every_lane_stage_before_side_effects(self):
        stages = [
            ("spec", {"status": "PASS", "spec_digest": digest("spec")}),
            ("spec-check", {"verdict": "PASS", "blockers": []}),
            ("red", {"head_commit": self.red_head,
                     "command": [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
                     "snapshot_digest": _tree_snapshot_digest(self.project, self.red_head),
                     "test_manifest": self.test_manifest}),
            ("green", {"head_commit": self.green_head, "status": "PASS",
                       "command": [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
                       "proof_digest": digest("green proof"),
                       "snapshot_digest": _tree_snapshot_digest(self.project, self.green_head),
                       "test_manifest": self.test_manifest}),
            ("review", None),
        ]
        for stage, clean in stages:
            if stage in ("red", "green"):
                self._git("checkout", "-q", "--detach", clean["head_commit"])
            if stage == "review":
                receipt = self.controller.run_holdout("offline-demo")
                clean = self._make_review(self.controller, receipt)
            for fragment in ("reserved_bound", "boundary sample", "PRIVATE_HOLDOUT_CANARY_7f2c9"):
                with self.subTest(stage=stage, fragment=fragment):
                    text = "prefix" + fragment + "suffix"
                    evidence = dict(clean)
                    if stage == "review":
                        evidence["findings"] = [{"severity": "FOLLOW-UP", "reason": text}]
                    else:
                        evidence["notes"] = [{"text": text}]
                    before = self.controller._log.read_bytes()
                    calls = list(self.controller._swrm.calls)
                    with patch.object(self.controller._evidence, "replay") as replay:
                        with self.assertRaisesRegex(Rejected, r"^private_evidence_leak:sha256:[0-9a-f]{64}$") as error:
                            self.controller.accept("offline-demo", stage, evidence)
                        replay.assert_not_called()
                    self.assertNotIn(fragment, str(error.exception))
                    self.assertEqual(self.controller._swrm.calls, calls)
                    self.assertEqual(self.controller._log.read_bytes(), before)
                    self.assertEqual(self._controller().public_state("offline-demo")["next"], stage)
            if stage != "review":
                # A private mapping key is still free text when its value is a
                # well-formed hash; only named binding fields bypass scanning.
                with self.assertRaisesRegex(Rejected, "private_evidence_leak"):
                    self.controller.accept("offline-demo", stage, dict(clean, reserved_bound=digest("binding")))
            self.controller.accept("offline-demo", stage, clean)

    def test_scope_and_manifest_bindings_survive_without_paths_or_arbitrary_output(self):
        scope = {"allowed_files": ["app.py", "tests/**"], "excluded_files": ["tests/excluded.py"]}
        controller = self._controller(self.root / "control-bindings")
        intake = controller.freeze_intake("offline-demo", issue_ref="issue:demo", base_commit=self.base,
            policy_digest=POLICY, public_scope=scope, holdout_required=True,
            holdout_suite=self._holdout_suite())
        expected_scope = {key: [digest(path) for path in paths] for key, paths in scope.items()}
        self.assertEqual(intake["public_scope"], expected_scope)
        self._accept_through_green(controller)
        raw_output = "unrelated private runner diagnostic"
        with patch.object(controller._evidence, "holdout", return_value={
                "status": "PASS", "isolation": "verified", "logs": raw_output,
                "command": ["hidden-runner"], "files": {"private-file": "private-source"}}):
            receipt, review = self._holdout_and_review(controller)
        self.assertEqual((receipt["change_id"], receipt["attempt"]), ("offline-demo", 0))
        delivered = controller.deliver("offline-demo", review)
        recovered = self._controller(controller.control_dir)
        state = recovered.public_state("offline-demo")
        self.assertEqual(state["intake"]["public_scope"], expected_scope)
        expected_manifest = [{"path_digest": digest(path), "digest": content}
                             for path, content in self.test_manifest.items()]
        for stage in ("red", "green"):
            self.assertEqual(state["receipts"][stage]["test_manifest"], expected_manifest)
            self.assertNotIn("command", state["receipts"][stage])
            self.assertNotIn("replay_receipt", state["receipts"][stage])
        public = json.dumps([intake, receipt, delivered, state, recovered.run_holdout("offline-demo")])
        for private in (raw_output, "hidden-runner", "private-file", "private-source",
                        "app.py", "tests/**", "tests/test_app.py", "test_reserved_boundary_case",
                        "PRIVATE_HOLDOUT_CANARY_7f2c9"):
            self.assertNotIn(private, public)
        self.assertIn(raw_output, controller._log.read_text())

    def test_public_boundary_scrubs_replay_and_legacy_review_receipts(self):
        leak = "reserved boundary sample"
        replay = self.controller._evidence.replay

        def replay_with_diagnostic(*args, **kwargs):
            return dict(replay(*args, **kwargs), diagnostic={"message": leak})

        with patch.object(self.controller._evidence, "replay", side_effect=replay_with_diagnostic):
            self._accept_through_green()
        receipt, review = self._holdout_and_review()
        # Simulate a receipt written by the former canary-only implementation.
        # Recovery must apply today's boundary even to already stored text.
        events = [json.loads(line) for line in self.controller._log.read_text().splitlines()]
        events[-1]["data"]["findings"] = [{"severity": "FOLLOW-UP", "reason": leak}]
        self.controller._log.write_text("".join(json.dumps(event) + "\n" for event in events))
        recovered = self._controller()
        self.assertNotIn(leak, json.dumps(recovered.public_state("offline-demo")))
        self.assertNotIn(leak, json.dumps(recovered.accept("offline-demo", "review", review)))


class ReviewerHistoryTests(unittest.TestCase):
    """Canaries for the public, bounded attempt summary supplied to Review."""

    def setUp(self):
        self.fixture = TestEvalidRolesControl()
        self.fixture.setUp()

    def tearDown(self):
        self.fixture.tearDown()

    def _deliver_repair(self, controller):
        fixture = self.fixture
        controller.freeze_intake("offline-demo", attempt=1, issue_ref="repair", base_commit=fixture.base,
            policy_digest=POLICY, public_scope={"allowed_files": ["app.py", "tests/**"]},
            holdout_required=False)
        fixture._git("checkout", "-q", "--detach", fixture.red_head)
        controller.accept("offline-demo", "spec", {"status": "PASS", "spec_digest": digest({"spec": 1})}, attempt=1)
        controller.accept("offline-demo", "spec-check", {"verdict": "PASS", "blockers": []}, attempt=1)
        command = [sys.executable, "-m", "unittest", "discover", "-s", "tests"]
        controller.accept("offline-demo", "red", {"head_commit": fixture.red_head, "command": command,
            "snapshot_digest": _tree_snapshot_digest(fixture.project, fixture.red_head),
            "test_manifest": fixture.test_manifest}, attempt=1)
        fixture._git("checkout", "-q", "--detach", fixture.green_head)
        controller.accept("offline-demo", "green", {"head_commit": fixture.green_head, "status": "PASS",
            "command": command, "proof_digest": digest("green proof"),
            "snapshot_digest": _tree_snapshot_digest(fixture.project, fixture.green_head),
            "test_manifest": fixture.test_manifest}, attempt=1)
        receipt = controller.run_holdout("offline-demo", attempt=1)
        state = controller.state("offline-demo", 1)
        review = {"reviewed_base": fixture.base, "reviewed_head": fixture.green_head,
            "commits": fixture._git("rev-list", "--reverse", f"{fixture.base}..{fixture.green_head}").splitlines(),
            "verdict": "PASS", "findings": [], "holdout_receipt": receipt["digest"],
            "spec_digest": state["receipts"]["spec"]["spec_digest"],
            "tdd_digest": state["receipts"]["green"]["evidence_digest"]}
        controller.accept("offline-demo", "review", review, attempt=1)
        controller.deliver("offline-demo", review, attempt=1)
        return controller._review_run.calls[-1]["attempt_history"]

    def test_rejected_spec_survives_restart_in_repair_history(self):
        fixture = self.fixture
        controller = fixture.controller
        with self.assertRaisesRegex(Rejected, "spec_not_accepted"):
            controller.accept("offline-demo", "spec", {"status": "FAIL", "detail": "secret detail"})
        controller = fixture._controller(controller.control_dir)
        history = self._deliver_repair(controller)
        self.assertEqual((history[0]["terminal"], history[0]["rejection_reason"]),
                         ("REJECTED", "SPEC_REJECTED"))
        self.assertNotIn("secret detail", json.dumps(history))

    def test_rejected_specs_preserve_public_stage_and_run_ids_after_restart(self):
        fixture = self.fixture
        controller = fixture.controller
        run_ids = ["failed-spec-run-a", "failed-spec-run-b"]
        for index, run_id in enumerate(run_ids):
            controller._swrm.responses["spec"]["run_id"] = run_id
            with self.subTest(run_id=run_id), self.assertRaisesRegex(Rejected, "spec_not_accepted"):
                controller.accept("offline-demo", "spec", {
                    "status": "FAIL", "spec_digest": digest(f"failed spec {index}")})

        recovered = fixture._controller(controller.control_dir)
        history = self._deliver_repair(recovered)
        first = history[0]
        self.assertEqual(first["stages"], ["intake", "spec"])
        self.assertEqual(first["run_id"], digest(run_ids))
        self.assertEqual((first["terminal"], first["rejection_reason"]),
                         ("REJECTED", "SPEC_REJECTED"))
        public_history = json.dumps(history)
        for run_id in run_ids:
            self.assertNotIn(run_id, public_history)

    def test_failed_green_replay_is_preserved_in_repair_history(self):
        fixture = self.fixture
        controller = fixture.controller
        fixture._accept_through_red(controller)

        # This commit is a valid green candidate in scope and descends from
        # red, but the actual public test suite fails on its snapshot.
        fixture._git("checkout", "-q", "--detach", fixture.green_head)
        (fixture.project / "app.py").write_text("def value():\n    return 2\n")
        fixture._git("add", "app.py")
        fixture._git("commit", "-qm", "broken implementation")
        failed_green_head = fixture._git("rev-parse", "HEAD")
        controller._swrm.responses["green"]["run_id"] = "failed-green-run"
        command = [sys.executable, "-m", "unittest", "discover", "-s", "tests"]
        with self.assertRaisesRegex(Rejected, "evidence_replay_failed"):
            controller.accept("offline-demo", "green", {
                "head_commit": failed_green_head, "status": "PASS", "command": command,
                "proof_digest": digest("green proof"),
                "snapshot_digest": _tree_snapshot_digest(fixture.project, failed_green_head),
                "test_manifest": fixture.test_manifest})

        recovered = fixture._controller(controller.control_dir)
        history = self._deliver_repair(recovered)
        first = history[0]
        self.assertEqual(first["stages"], ["intake", "spec", "spec-check", "red", "green"])
        self.assertEqual(first["run_id"], digest([
            fixture.controller._swrm.responses[stage]["run_id"]
            for stage in ("spec", "spec-check", "red")
        ] + ["failed-green-run"]))
        self.assertEqual((first["terminal"], first["rejection_reason"]),
                         ("REJECTED", "GREEN_REJECTED"))
        self.assertNotIn("failed-green-run", json.dumps(history))

    def test_rejected_green_remains_auditable_after_same_attempt_repair_and_restart(self):
        fixture = self.fixture
        controller = fixture.controller
        fixture._accept_through_red(controller)

        # Record an actual failed green replay, then retry the valid green
        # candidate after controller recovery in the same attempt.
        fixture._git("checkout", "-q", "--detach", fixture.green_head)
        (fixture.project / "app.py").write_text("def value():\n    return 2\n")
        fixture._git("add", "app.py")
        fixture._git("commit", "-qm", "broken implementation")
        failed_green_head = fixture._git("rev-parse", "HEAD")
        controller._swrm.responses["green"]["run_id"] = "failed-green-run"
        command = [sys.executable, "-m", "unittest", "discover", "-s", "tests"]
        with self.assertRaisesRegex(Rejected, "evidence_replay_failed"):
            controller.accept("offline-demo", "green", {
                "head_commit": failed_green_head, "status": "PASS", "command": command,
                "proof_digest": digest("green proof"),
                "snapshot_digest": _tree_snapshot_digest(fixture.project, failed_green_head),
                "test_manifest": fixture.test_manifest})

        recovered = fixture._controller(controller.control_dir)
        fixture._git("checkout", "-q", "--detach", fixture.green_head)
        recovered.accept("offline-demo", "green", {
            "head_commit": fixture.green_head, "status": "PASS", "command": command,
            "proof_digest": digest("green proof"),
            "snapshot_digest": _tree_snapshot_digest(fixture.project, fixture.green_head),
            "test_manifest": fixture.test_manifest})
        with patch.object(recovered._evidence, "holdout", return_value={
                "status": "PASS", "isolation": "verified"}):
            receipt = recovered.run_holdout("offline-demo")
        review = fixture._make_review(recovered, receipt)
        recovered.accept("offline-demo", "review", review)
        recovered.deliver("offline-demo", review)

        history = recovered._review_run.calls[-1]["attempt_history"]
        self.assertEqual([(item["attempt"], item["terminal"], item["rejection_reason"])
                          for item in history], [(0, "PASS", "GREEN_REJECTED")])
        self.assertNotIn("failed-green-run", json.dumps(history))

    def test_spec_not_revised_refusal_is_auditable_by_the_reviewer(self):
        fixture = self.fixture
        controller = fixture.controller
        controller.accept("offline-demo", "spec", {"status": "PASS", "spec_digest": digest({"spec": 1})})
        controller.accept("offline-demo", "spec-check", {"verdict": "FAIL", "blockers": ["exit code missing"]})
        state = controller.state("offline-demo")
        current = state["receipts"]["spec"]["spec_digest"]
        with self.assertRaisesRegex(Rejected, "spec_not_revised"):
            controller.accept("offline-demo", "spec", {
                "status": "PASS", "spec_digest": current, "spec_version": 2,
                "parent_receipt": state["rejections"][-1]["receipt_digest"]})

        history = self._deliver_repair(controller)
        first = history[0]
        self.assertEqual((first["terminal"], first["rejection_reason"]),
                         ("REJECTED", "spec_not_revised"))
        self.assertEqual(first["run_id"], digest([
            fixture.controller._swrm.responses[stage]["run_id"]
            for stage in ("spec", "spec-check", "spec")]))
        self.assertNotIn(fixture.controller._swrm.responses["spec"]["run_id"], json.dumps(history))

    def test_semantic_rejection_survives_restart(self):
        fixture = self.fixture
        controller = fixture.controller
        fixture._accept_through_green()
        with patch.object(controller._evidence, "holdout", return_value={"status": "PASS", "isolation": "verified"}):
            _, review = fixture._holdout_and_review()
        with patch.object(controller._review_run, "review", return_value="FAIL"):
            with self.assertRaisesRegex(Rejected, "review_run_rejected"):
                controller.deliver("offline-demo", review)
        recovered = fixture._controller(controller.control_dir)
        history = self._deliver_repair(recovered)
        self.assertEqual((history[0]["terminal"], history[0]["rejection_reason"]),
                         ("REJECTED", "REVIEW_REJECTED"))

    def test_pass_review_does_not_override_failed_holdout(self):
        fixture = self.fixture
        controller = fixture.controller
        fixture._accept_through_green()
        with patch.object(controller._evidence, "holdout", return_value={"status": "FAIL", "isolation": "verified"}):
            _, review = fixture._holdout_and_review()
        history = self._deliver_repair(controller)
        self.assertEqual((history[0]["terminal"], history[0]["rejection_reason"]),
                         ("FAIL", "HOLDOUT_REJECTED"))

    def _assert_holdout_rejection_survives_review(self, status, isolation, delivery_error):
        fixture = self.fixture
        controller = fixture.controller
        fixture._accept_through_green()
        with patch.object(controller._evidence, "holdout", return_value={
                "status": status, "isolation": isolation}):
            _, review = fixture._holdout_and_review()
        with self.assertRaisesRegex(Rejected, delivery_error):
            controller.deliver("offline-demo", review)

        recovered = fixture._controller(controller.control_dir)
        history = self._deliver_repair(recovered)
        self.assertEqual((history[0]["terminal"], history[0]["rejection_reason"]),
                         ("FAIL", "HOLDOUT_REJECTED"))

    def test_infra_holdout_rejection_survives_pass_review_and_restart(self):
        self._assert_holdout_rejection_survives_review("INFRA", "verified", "holdout_failed")

    def test_unisolated_pass_holdout_rejection_survives_pass_review_and_restart(self):
        self._assert_holdout_rejection_survives_review("PASS", "unisolated", "holdout_isolation")

    def test_reviewer_receives_bounded_ordered_public_attempt_history(self):
        fixture = self.fixture
        controller = fixture.controller
        fixture._accept_through_green()
        with patch.object(controller._evidence, "holdout", return_value={
                "status": "PASS", "isolation": "verified"}):
            _, review = fixture._holdout_and_review()
        controller.deliver("offline-demo", review)

        context = controller._review_run.calls[-1]
        history = context["attempt_history"]
        self.assertLessEqual(len(history), 8)
        self.assertEqual([item["attempt"] for item in history], sorted(
            item["attempt"] for item in history))
        self.assertEqual(history[0]["attempt"], 0)
        self.assertEqual(history[0]["parent_attempt"], None)
        self.assertEqual(history[0]["terminal"], "PASS")
        self.assertIn("run_id", history[0])
        self.assertIn("rejection_reason", history[0])
        allowed = {"attempt", "stages", "run_id", "terminal", "parent_attempt", "rejection_reason"}
        self.assertTrue(all(set(item) <= allowed for item in history))
        public_text = json.dumps(history)
        for private in ("PRIVATE_HOLDOUT_CANARY_7f2c9", "test_reserved_boundary_case",
                        str(fixture.root), "hidden-runner"):
            self.assertNotIn(private, public_text)

    def test_reviewer_history_rejects_broken_parent_chain(self):
        # A repair attempt must identify the immediately preceding attempt;
        # reviewers must not receive a plausible summary of a disconnected chain.
        controller = self.fixture.controller
        broken = [{"attempt": 0, "stages": ["intake", "spec"], "run_id": digest("r0"),
                   "terminal": "REJECTED", "parent_attempt": None,
                   "rejection_reason": "SPEC_REJECTED"},
                  {"attempt": 2, "stages": ["intake", "spec"], "run_id": digest("r2"),
                   "terminal": "PASS", "parent_attempt": 1,
                   "rejection_reason": None}]
        with self.assertRaisesRegex(Rejected, "attempt_history_chain"):
            controller._validate_attempt_history("offline-demo", broken)

    def test_reviewer_history_is_stable_for_same_attempt_log(self):
        fixture = self.fixture
        controller = fixture.controller
        fixture._accept_through_green()
        with patch.object(controller._evidence, "holdout", return_value={
                "status": "PASS", "isolation": "verified"}):
            _, review = fixture._holdout_and_review()
        controller.deliver("offline-demo", review)
        first = controller._review_run.calls[-1]["attempt_history"]
        recovered = fixture._controller(controller.control_dir)
        context = recovered._review_context("offline-demo", review)
        self.assertEqual(context["attempt_history"], first)



class SpecRevisionTests(TestEvalidRolesControl):
    def test_unversioned_initial_spec_cannot_bypass_revision_bindings(self):
        self._spec(declare=False)
        rejected = self._check(verdict="FAIL", bind=False)
        with self.assertRaises(Rejected):
            self._spec(version=2, declare=False, parent=None)
        with self.assertRaises(Rejected):
            self._spec(version=2, parent=None)
        self._spec(version=2, parent=rejected["evidence_digest"])
        with self.assertRaises(Rejected):
            self._check(bind=False)

    def test_concurrent_stale_check_cannot_advance_new_spec(self):
        self._spec(version=1)
        other = self._controller()
        append = self.controller._append

        def race(event):
            self._check(other, verdict="FAIL")
            self._spec(other, version=2)
            return append(event)

        with patch.object(self.controller, "_append", side_effect=race):
            with self.assertRaises(Rejected):
                self._check(verdict="PASS")
        state = self._controller().state("offline-demo")
        self.assertEqual(state["spec_version"], 2)
        self.assertEqual(state["next"], "spec-check")
        self.assertEqual(state["receipts"]["spec-check"]["verdict"], "FAIL")

    def test_replayed_check_from_earlier_version_is_rejected_after_digest_repeats(self):
        first_digest = digest({"spec": "shared"})
        second_digest = digest({"spec": "revised"})
        self._spec(version=1, spec_digest=first_digest)
        first_check = {"verdict": "FAIL", "blockers": ["first rejection"],
                       "spec_digest": first_digest, "spec_version": 1}
        self.controller.accept("offline-demo", "spec-check", first_check)

        self._spec(version=2, spec_digest=second_digest)
        self._check(verdict="FAIL", blockers=["second rejection"])
        self._spec(version=3, spec_digest=first_digest)

        with self.assertRaises(Rejected):
            self.controller.accept("offline-demo", "spec-check", first_check)

        state = self.controller.state("offline-demo")
        self.assertEqual(state["spec_version"], 3)
        self.assertEqual(state["receipts"]["spec"]["spec_digest"], first_digest)
        self.assertEqual(state["next"], "spec-check")
        self.assertEqual(len(state["rejections"]), 2)

    def test_concurrent_stale_versioned_initial_spec_is_rejected(self):
        other = self._controller()
        spec_digest = digest({"spec": "shared"})
        append = self.controller._append

        def race(event):
            self._spec(other, spec_digest=spec_digest, declare=False)
            self._check(other, verdict="FAIL", bind=False)
            return append(event)

        with patch.object(self.controller, "_append", side_effect=race):
            with self.assertRaises(Rejected):
                self._spec(version=1, spec_digest=spec_digest)

        state = self._controller().state("offline-demo")
        self.assertEqual(state["spec_version"], 1)
        self.assertEqual(state["receipts"]["spec"]["versioned"], False)
        self.assertEqual(state["receipts"]["spec-check"]["verdict"], "FAIL")
        self.assertEqual(state["next"], "spec")

    def test_concurrent_stale_unversioned_initial_spec_is_rejected(self):
        other = self._controller()
        spec_digest = digest({"spec": "shared"})
        append = self.controller._append

        def race(event):
            self._spec(other, version=1, spec_digest=spec_digest)
            self._check(other, verdict="FAIL")
            return append(event)

        with patch.object(self.controller, "_append", side_effect=race):
            with self.assertRaises(Rejected):
                self._spec(declare=False, spec_digest=spec_digest)

        state = self._controller().state("offline-demo")
        self.assertEqual(state["spec_version"], 1)
        self.assertEqual(state["receipts"]["spec"]["versioned"], True)
        self.assertEqual(state["receipts"]["spec-check"]["verdict"], "FAIL")
        self.assertEqual(state["next"], "spec")

    def test_recorded_spec_check_fail_is_a_public_rejection(self):
        self._spec()
        self._check(verdict="FAIL")
        history = self.controller._attempt_history("offline-demo")
        self.assertEqual(history[0]["terminal"], "REJECTED")
        self.assertEqual(history[0]["rejection_reason"], "SPEC_CHECK_REJECTED")
        self.controller._validate_attempt_history("offline-demo", history)

    def _spec(self, controller=None, version=1, spec_digest=None, parent="auto", declare=True):
        """Submit a Spec receipt, binding the rejection it answers by default."""
        controller = controller or self.controller
        state = controller.state("offline-demo")
        evidence = {"status": "PASS", "spec_digest": spec_digest or digest({"spec": version})}
        if declare:
            evidence["spec_version"] = version
        if parent == "auto":
            rejections = state.get("rejections") or []
            if rejections:
                evidence["parent_receipt"] = rejections[-1]["receipt_digest"]
        elif parent is not None:
            evidence["parent_receipt"] = parent
        return controller.accept("offline-demo", "spec", evidence)

    def _check(self, controller=None, verdict="PASS", blockers=None, spec_digest=None,
               spec_version=None, bind=True):
        """Submit a spec-check receipt bound to the current Spec revision."""
        controller = controller or self.controller
        state = controller.state("offline-demo")
        evidence = {"verdict": verdict, "blockers": blockers or []}
        if bind and state["receipts"].get("spec", {}).get("versioned"):
            evidence["spec_digest"] = spec_digest or state["receipts"]["spec"]["spec_digest"]
            evidence["spec_version"] = (spec_version if spec_version is not None
                                         else state["spec_version"])
        return controller.accept("offline-demo", "spec-check", evidence)

    def test_failed_spec_check_is_preserved_and_allows_revision(self):
        first_spec = {"status": "PASS", "spec_digest": digest({"spec": 1})}
        self.controller.accept("offline-demo", "spec", first_spec)

        rejected_spec = {"verdict": "FAIL", "blockers": []}
        failed_receipt = self.controller.accept("offline-demo", "spec-check", rejected_spec)
        state = self.controller.state("offline-demo")
        self.assertEqual(failed_receipt["verdict"], "FAIL")
        self.assertEqual(state["next"], "spec")
        self.assertEqual(state["receipts"]["spec-check"]["verdict"], "FAIL")

        revised_spec = {"status": "PASS", "spec_digest": digest({"spec": 2}),
                        "spec_version": 2, "parent_receipt": failed_receipt["evidence_digest"]}
        self.controller.accept("offline-demo", "spec", revised_spec)
        self.controller.accept("offline-demo", "spec-check", {"verdict": "PASS", "blockers": [],
            "spec_digest": revised_spec["spec_digest"], "spec_version": 2})
        state = self.controller.state("offline-demo")
        self.assertEqual(state["receipts"]["spec"]["spec_digest"], revised_spec["spec_digest"])
        self.assertEqual(state["receipts"]["spec-check"]["verdict"], "PASS")
        self.assertEqual([event["data"]["verdict"] for event in self.controller._events
                          if event["stage"] == "spec-check"], ["FAIL", "PASS"])

    def test_two_bounded_revisions_advance_to_red(self):
        self._spec(version=1)
        self._check(verdict="FAIL", blockers=["exit code missing"])
        self._spec(version=2)
        self._check(verdict="FAIL", blockers=["acceptance command absent"])
        self._spec(version=3)
        self._check(verdict="PASS")
        state = self.controller.state("offline-demo")
        self.assertEqual(state["next"], "red")
        self.assertEqual((state["spec_version"], state["spec_revisions"]), (3, 2))
        self.assertEqual(state["receipts"]["spec"]["spec_version"], 3)
        self.assertEqual(state["receipts"]["spec-check"]["verdict"], "PASS")
        self.assertEqual([item["verdict"] for item in state["rejections"]], ["FAIL", "FAIL"])
        self.assertEqual([item["spec_version"] for item in state["rejections"]], [1, 2])

    def test_revised_spec_still_reaches_delivery(self):
        self._spec(version=1)
        self._check(verdict="FAIL", blockers=["exit code missing"])
        revised = self._spec(version=2)
        self._check(verdict="PASS")
        self._git("checkout", "-q", "--detach", self.red_head)
        self.controller.accept("offline-demo", "red", {
            "head_commit": self.red_head,
            "command": [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
            "snapshot_digest": _tree_snapshot_digest(self.project, self.red_head),
            "test_manifest": self.test_manifest})
        self._git("checkout", "-q", "--detach", self.green_head)
        self.controller.accept("offline-demo", "green", {
            "head_commit": self.green_head, "status": "PASS",
            "command": [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
            "proof_digest": digest("green proof"),
            "snapshot_digest": _tree_snapshot_digest(self.project, self.green_head),
            "test_manifest": self.test_manifest})
        receipt = self.controller.run_holdout("offline-demo")
        review = self._make_review(self.controller, receipt)
        self.controller.accept("offline-demo", "review", review)
        state = self.controller.state("offline-demo")
        self.assertEqual(state["receipts"]["spec"]["spec_digest"], revised["spec_digest"])
        self.assertEqual(state["receipts"]["review"]["spec_digest"], revised["spec_digest"])
        self.assertEqual(len(state["rejections"]), 1)
        # The offline seam cannot prove isolation, so delivery stays refused.
        with self.assertRaisesRegex(Rejected, "holdout_isolation"):
            self.controller.deliver("offline-demo", review)

    def test_third_failure_enters_needs_boss_without_touching_the_spec(self):
        self._spec(version=1)
        self._check(verdict="FAIL", blockers=["exit code missing"])
        self._spec(version=2)
        self._check(verdict="FAIL", blockers=["acceptance command absent"])
        self._spec(version=3)
        self._check(verdict="FAIL", blockers=["scenario not executable"])
        state = self.controller.state("offline-demo")
        self.assertEqual(state["status"], "NEEDS-BOSS")
        self.assertEqual(state["next"], "needs-boss")
        self.assertEqual(state["receipts"]["spec"]["spec_version"], 3)
        self.assertEqual(state["receipts"]["spec-check"]["verdict"], "FAIL")
        self.assertEqual(len(state["rejections"]), 3)
        with self.assertRaisesRegex(Rejected, "spec_revision_limit"):
            self._spec(version=4)
        with self.assertRaisesRegex(Rejected, "spec_revision_limit"):
            self._check(verdict="PASS")
        with self.assertRaisesRegex(Rejected, "controller_order"):
            self.controller.accept("offline-demo", "red", {"head_commit": self.red_head})
        state = self.controller.state("offline-demo")
        self.assertEqual(state["next"], "needs-boss")
        self.assertEqual(state["receipts"]["spec"]["spec_version"], 3)

    def test_revision_must_change_version_and_digest(self):
        self._spec(version=1)
        self._check(verdict="FAIL", blockers=["exit code missing"])
        current = self.controller.state("offline-demo")["receipts"]["spec"]["spec_digest"]
        with self.assertRaisesRegex(Rejected, "spec_not_revised"):
            self._spec(version=2, spec_digest=current)
        with self.assertRaisesRegex(Rejected, "spec_version_mismatch"):
            self._spec(version=1)
        with self.assertRaisesRegex(Rejected, "spec_version_missing"):
            self._spec(version=2, declare=False)
        with self.assertRaisesRegex(Rejected, "spec_version_invalid"):
            self._spec(version="two")
        state = self.controller.state("offline-demo")
        self.assertEqual(state["receipts"]["spec"]["spec_digest"], current)
        self.assertEqual(state["next"], "spec")

    def test_spec_not_revised_refusal_enters_the_attempt_history(self):
        # A revision that repeats the accepted digest is a verdict on the
        # evidence: the refusal is public, so a reviewer can audit it and the
        # run it belongs to.
        self._spec(version=1)
        self._check(verdict="FAIL", blockers=["exit code missing"])
        current = self.controller.state("offline-demo")["receipts"]["spec"]["spec_digest"]
        before = self.controller._log.read_bytes()
        with self.assertRaisesRegex(Rejected, "spec_not_revised"):
            self._spec(version=2, spec_digest=current)
        self.assertNotEqual(self.controller._log.read_bytes(), before)

        history = self.controller._attempt_history("offline-demo")
        item = history[0]
        self.assertEqual(item["terminal"], "REJECTED")
        self.assertIn("spec_not_revised", item["rejection_reason"])
        expected_runs = [self.controller._swrm.responses[stage]["run_id"]
                         for stage in ("spec", "spec-check", "spec")]
        self.assertEqual(item["run_id"], digest(expected_runs))
        self.assertNotIn(self.controller._swrm.responses["spec"]["run_id"], json.dumps(history))
        self.controller._validate_attempt_history("offline-demo", history)

        # The refusal survives a restart, so the reviewer still sees it.
        recovered = self._controller()
        self.assertEqual(recovered._attempt_history("offline-demo"), history)

    def test_valid_spec_revision_after_a_refused_one_clears_the_terminal(self):
        self._spec(version=1)
        self._check(verdict="FAIL", blockers=["exit code missing"])
        current = self.controller.state("offline-demo")["receipts"]["spec"]["spec_digest"]
        with self.assertRaisesRegex(Rejected, "spec_not_revised"):
            self._spec(version=2, spec_digest=current)
        self._spec(version=2, spec_digest=digest({"spec": 2}))
        history = self.controller._attempt_history("offline-demo")
        self.assertEqual(history[0]["terminal"], "IN_PROGRESS")
        self.assertEqual(history[0]["rejection_reason"], "spec_not_revised")
        self.controller._validate_attempt_history("offline-demo", history)

    def test_legacy_revision_must_change_the_digest(self):
        self._spec(version=1, declare=False)
        self._check(verdict="FAIL", blockers=["exit code missing"], bind=False)
        current = self.controller.state("offline-demo")["receipts"]["spec"]["spec_digest"]
        with self.assertRaisesRegex(Rejected, "spec_not_revised"):
            self._spec(version=2, spec_digest=current)
        state = self.controller.state("offline-demo")
        self.assertEqual(state["receipts"]["spec"]["spec_digest"], current)
        self.assertEqual(state["next"], "spec")

    def test_revision_must_bind_the_rejected_receipt(self):
        self._spec(version=1)
        rejected = self._check(verdict="FAIL", blockers=["exit code missing"])
        with self.assertRaisesRegex(Rejected, "missing_parent_receipt"):
            self._spec(version=2, parent=None)
        with self.assertRaisesRegex(Rejected, "parent_receipt_mismatch"):
            self._spec(version=2, parent=digest({"foreign": True}))
        accepted = self._spec(version=2, parent=rejected["evidence_digest"])
        self.assertEqual(accepted["parent_receipt"], rejected["evidence_digest"])
        self.assertEqual(self.controller.state("offline-demo")["next"], "spec-check")

    def test_stale_spec_check_receipt_cannot_advance_the_revision(self):
        first = self._spec(version=1)
        self._check(verdict="FAIL", blockers=["exit code missing"])
        self._spec(version=2)
        with self.assertRaisesRegex(Rejected, "spec_check_parent_mismatch"):
            self._check(verdict="PASS", spec_digest=first["spec_digest"], spec_version=1)
        with self.assertRaisesRegex(Rejected, "spec_check_parent_mismatch"):
            self._check(verdict="PASS", spec_version=1)
        with self.assertRaisesRegex(Rejected, "spec_check_parent_missing"):
            self._check(verdict="PASS", bind=False)
        state = self.controller.state("offline-demo")
        self.assertEqual(state["next"], "spec-check")
        self.assertEqual(state["receipts"]["spec-check"]["verdict"], "FAIL")

    def test_replayed_rejection_from_an_earlier_round_is_rejected(self):
        self._spec(version=1, declare=False)
        self._check(verdict="FAIL", blockers=["exit code missing"], bind=False)
        events = [json.loads(line) for line in self.controller._log.read_text().splitlines()]
        check_event = next(event for event in events if event["stage"] == "spec-check")
        check_event["data"].pop("parent_spec_digest", None)
        check_event["data"].pop("spec_version", None)
        self.controller._log.write_text("".join(
            json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n"
            for event in events))
        self._spec(version=2)
        with self.assertRaisesRegex(Rejected, "stale_spec_check_receipt"):
            self._check(verdict="FAIL", blockers=["exit code missing"], bind=False)
        state = self.controller.state("offline-demo")
        self.assertEqual(len(state["rejections"]), 1)
        self.assertEqual(state["next"], "spec-check")

    def test_replayed_rejection_receipt_is_idempotent(self):
        self._spec(version=1)
        rejected = self._check(verdict="FAIL", blockers=["exit code missing"])
        again = self._check(verdict="FAIL", blockers=["exit code missing"])
        state = self.controller.state("offline-demo")
        self.assertEqual(rejected, again)
        self.assertEqual(len(state["rejections"]), 1)
        self.assertEqual(len([event for event in self.controller._events
                              if event["stage"] == "spec-check"]), 1)
        self.assertEqual(state["next"], "spec")

    def test_legacy_spec_check_receipt_remains_idempotent_after_upgrade(self):
        self._spec(version=1, declare=False)
        evidence = {"verdict": "PASS", "blockers": []}
        self._check(verdict="PASS", bind=False)

        # Baseline receipts did not store revision metadata on Spec checks.
        events = [json.loads(line) for line in self.controller._log.read_text().splitlines()]
        spec_event = next(event for event in events if event["stage"] == "spec")
        check_event = next(event for event in events if event["stage"] == "spec-check")
        spec_event["data"].pop("spec_version", None)
        spec_event["data"].pop("versioned", None)
        check_event["data"].pop("parent_spec_digest", None)
        check_event["data"].pop("spec_version", None)
        self.controller._log.write_text("".join(
            json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n"
            for event in events))

        recovered = self._controller()
        event_count = len(recovered._events)
        retried = recovered.accept("offline-demo", "spec-check", evidence)
        self.assertEqual(retried["verdict"], "PASS")
        self.assertEqual(retried["evidence_digest"], check_event["data"]["evidence_digest"])
        self.assertEqual(recovered.state("offline-demo")["next"], "red")
        self.assertEqual(len(recovered._events), event_count)

    def test_rejected_evidence_survives_recovery(self):
        self._spec(version=1)
        rejected = self._check(verdict="FAIL", blockers=["exit code missing"])
        recovered = self._controller()
        state = recovered.state("offline-demo")
        self.assertEqual(state["next"], "spec")
        self.assertEqual(len(state["rejections"]), 1)
        self.assertEqual(state["rejections"][0]["receipt_digest"], rejected["evidence_digest"])
        self.assertEqual(state["rejections"][0]["spec_version"], 1)
        public = recovered.public_state("offline-demo")
        self.assertEqual(public["rejections"][0]["verdict"], "FAIL")
        self.assertEqual(public["rejections"][0]["blocker_count"], 1)
        self.assertNotIn("exit code missing", json.dumps(public))
        # The recovered controller still accepts the bounded revision.
        recovered.accept("offline-demo", "spec", {"status": "PASS",
            "spec_digest": digest({"spec": 2}), "spec_version": 2,
            "parent_receipt": rejected["evidence_digest"]})
        self.assertEqual(recovered.state("offline-demo")["next"], "spec-check")


if __name__ == "__main__":
    unittest.main()
