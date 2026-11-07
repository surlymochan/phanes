import hashlib
import importlib.util
import inspect
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.evalid_roles_control import (LocalEvidence, MockSwrm, NativeEvidence, Rejected, RoleController,
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
        result = subprocess.run(["git", "-C", str(self.project), *args], check=True,
                                capture_output=True, text=True)
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


if __name__ == "__main__":
    unittest.main()
