"""Acceptance tests for the tester evidence review risk classifier (#1988)."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).with_name("tester_risk.py")
spec = importlib.util.spec_from_file_location("tester_risk", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
SCRATCH = SCRIPT.resolve().parents[2] / ".swrm-tmp"


class TesterRiskTests(unittest.TestCase):
    def test_omitted_history_requires_full_read(self):
        result = module.classify(
            {"files_changed": 1, "additions": 2, "deletions": 1},
            pr_number=1988,
            sample_rate=0,
        )

        self.assertEqual(result["decision"], "full_read")
        self.assertIn("history", result["reason"])

    def test_explicit_empty_history_allows_routine_sampling(self):
        diff = {"files_changed": 1, "additions": 2, "deletions": 1}
        for rate, decision in ((0, "sampled"), (1, "full_read")):
            with self.subTest(rate=rate):
                result = module.classify(diff, [], pr_number=1988, sample_rate=rate)
                self.assertEqual(result["decision"], decision)

    def test_large_diff_thresholds_require_full_read_with_sampling_disabled(self):
        for diff in (
            {"files_changed": 20, "additions": 0, "deletions": 0},
            {"files_changed": 1, "additions": 500, "deletions": 500},
        ):
            with self.subTest(diff=diff):
                result = module.classify(diff, [], pr_number=1988, sample_rate=0)
                self.assertEqual(result["decision"], "full_read")

    def test_sampling_uses_pr_and_seed_without_diff_order_dependence(self):
        diff = {"files_changed": 1, "additions": 2, "deletions": 1}
        decisions = []
        for pr in range(1, 31):
            first = module.classify(diff, [], pr_number=pr, seed="fixed")
            repeated = module.classify(dict(reversed(list(diff.items()))), [],
                                       pr_number=pr, seed="fixed")
            self.assertEqual(first, repeated)
            decisions.append(first["decision"])
        self.assertEqual(set(decisions), {"full_read", "sampled"})

    def classify(self, diff_stats, needs_human_history=(), **config):
        return module.classify(
            diff_stats=diff_stats,
            needs_human_history=needs_human_history,
            pr_number=1988,
            sample_rate=0.2,
            seed="risk-v1",
            **config,
        )

    def test_large_diff_always_requires_full_read(self):
        result = self.classify({"files_changed": 100, "additions": 5000, "deletions": 3000})

        self.assertEqual(result["decision"], "full_read")
        self.assertTrue(result["reason"])

    def test_any_needs_human_history_always_requires_full_read(self):
        result = self.classify(
            {"files_changed": 1, "additions": 2, "deletions": 1},
            needs_human_history=[False, True, False],
        )

        self.assertEqual(result["decision"], "full_read")
        self.assertTrue(result["reason"])

    def test_routine_card_sampling_is_stable_for_same_pr_and_seed(self):
        diff = {"files_changed": 2, "additions": 12, "deletions": 3}

        first = self.classify(diff)
        second = self.classify(diff)

        self.assertEqual(first, second)
        self.assertIn(first["decision"], {"full_read", "sampled"})
        self.assertTrue(first["reason"])

    def test_missing_or_malformed_inputs_fail_closed_to_full_read(self):
        cases = (
            (None, ()),
            ({"files_changed": "many", "additions": 1, "deletions": 1}, ()),
            ({"files_changed": 1, "additions": 1}, ()),
            ({"files_changed": 1, "additions": 1, "deletions": 1}, None),
            ({"files_changed": 1, "additions": 1, "deletions": 1}, "unknown"),
        )
        for diff_stats, history in cases:
            with self.subTest(diff_stats=diff_stats, history=history):
                result = self.classify(diff_stats, needs_human_history=history)
                self.assertEqual(result["decision"], "full_read")
                self.assertTrue(result["reason"])


class TesterRiskDispatchTests(unittest.TestCase):
    """Drive the real queue, dispatch task, and state writer with offline I/O."""

    def run_card(self, pr=None, events=None, *, target="#1988", rate="0", seed="fixed",
                 issue_number=2000, verify=True, broken=False, classifier=True):
        spec = importlib.util.spec_from_file_location(
            "risk_dispatch", SCRIPT.with_name("autodispatch.py"))
        ad = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ad)
        pr = {"changed_files": 1, "additions": 2, "deletions": 1, "labels": []} if pr is None else pr
        events = [[]] if events is None else events
        body = f"## Verify Intent\n目标 PR: {target}\n证据要求:\n- [ ] 测试日志"
        row = {"number": issue_number, "title": "verify evidence", "body": body,
               "labels": ["lane-ready", "verify-intent"] if verify else ["lane-ready"]}
        reads = []

        def fake_sh(cmd, **kwargs):
            if cmd[:3] == ["gh", "issue", "list"]:
                return 0, json.dumps([row]), ""
            if cmd[:3] == ["gh", "issue", "view"]:
                return 0, json.dumps(row), ""
            if cmd[:3] == ["gh", "issue", "edit"]:
                return 0, "", ""
            if cmd[:2] == ["gh", "api"]:
                reads.append(cmd)
                if broken: return None  # a runner that breaks the reply contract
                data = events if "/events" in cmd[2] else pr
                if isinstance(data, Exception):
                    raise data
                if isinstance(data, tuple):
                    return data
                if "/events" in cmd[2] and isinstance(data, list):
                    return 0, "\n".join(json.dumps(page) for page in data), ""
                return 0, json.dumps(data), ""
            if cmd[:2] == ["git", "ls-remote"]:
                return 0, "a" * 40 + "\n", ""
            if cmd[0] == "ssh":
                return 0, '{"id": "risk-run"}', ""
            self.fail(f"unexpected command: {cmd}")

        SCRATCH.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=SCRATCH) as tmp:
            root = Path(tmp)
            (root / "PAUSE-MINT").write_text("test: no replenishment")
            env = {"OPENSWARM_HOME": str(root / "swrm"),
                   "OPENSWARM_CTL_STATE_DIR": str(root / "ctl"),
                   "AD_TESTER_RISK_SEED": seed}
            if rate is not None:
                env["AD_TESTER_RISK_SAMPLE_RATE"] = rate
            with patch.dict(os.environ, env), patch.multiple(
                ad, D=tmp, BRAIN_LOG=str(root / "brain.log"), AD_VERIFY_SHARE_ENABLED=False,
                sh=fake_sh, remote=lambda *args: {"lane": "idle"},
                tester_risk=ad.tester_risk if classifier else None,
                # Isolate the pre-existing pause_reason signature failure in status output.
                status_snapshot=lambda *args: {},
            ):
                ad.tick({"CX2": {"host": "test-host", "backend": "codex"}}, {}, 0)
            state = json.loads((root / "state.json").read_text())
            self.state = state
            task = (root / "tasks" / f"CX2-{issue_number}.md").read_text()
        self.assertEqual(state["CX2"]["issue"], issue_number)
        return state["CX2"], task, reads

    def test_tick_persists_large_diff_decision_and_passes_it_to_review_lane(self):
        state, task, reads = self.run_card(
            {"changed_files": 20, "additions": 2, "deletions": 1, "labels": []})
        risk = state["tester_risk"]
        self.assertEqual(risk["decision"], "full_read")
        self.assertIn("large diff", risk["reason"])
        self.assertIn(risk["decision"], task)
        self.assertIn(risk["reason"], task)
        self.assertTrue(reads)

    def test_tick_reads_removed_needs_human_labels_across_history_pages(self):
        state, _, reads = self.run_card(events=[[], [
            {"event": "labeled", "label": {"name": "needs-human"}},
            {"event": "unlabeled", "label": {"name": "needs-human"}},
        ]])
        self.assertEqual(state["tester_risk"]["decision"], "full_read")
        self.assertIn("history present", state["tester_risk"]["reason"])
        history_read = next(cmd for cmd in reads if "/events" in cmd[2])
        self.assertIn("--paginate", history_read)
        self.assertNotIn("--slurp", history_read)  # supported by older deployed gh versions

    def test_tick_current_needs_human_label_requires_full_read(self):
        state, _, _ = self.run_card(
            {"changed_files": 1, "additions": 2, "deletions": 1,
             "labels": [{"name": "needs-human"}]})
        self.assertEqual(state["tester_risk"]["decision"], "full_read")

    def test_default_sample_rate_keeps_full_read_for_routine_cards(self):
        """Without any env override no card is sampled: the default gate stays full review."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AD_TESTER_RISK_SAMPLE_RATE", None)
            for pr_number in range(1, 40):
                state, _, _ = self.run_card(target=f"https://github.com/surlymochan/phanes/pull/{pr_number}",
                                            rate=None)
                self.assertEqual(state["tester_risk"]["decision"], "full_read", pr_number)

    def test_tick_sampling_uses_target_pr_instead_of_card_number(self):
        for rate, expected in (("0", "sampled"), ("1", "full_read"), ("0.2", None)):
            with self.subTest(rate=rate):
                first, _, _ = self.run_card(rate=rate)
                repeated, _, _ = self.run_card(
                    target="https://github.com/surlymochan/phanes/pull/1988",
                    rate=rate, issue_number=2001)
                self.assertEqual(first["tester_risk"], repeated["tester_risk"])
                expected_risk = module.classify(
                    {"files_changed": 1, "additions": 2, "deletions": 1}, [],
                    pr_number=1988, sample_rate=float(rate), seed="fixed")
                self.assertEqual(first["tester_risk"], expected_risk)
                if expected:
                    self.assertEqual(first["tester_risk"]["decision"], expected)

    def test_tick_missing_or_malformed_remote_data_and_config_fail_closed(self):
        for kwargs in (
            {"pr": {}}, {"pr": {"changed_files": True}},
            {"pr": (1, "", "unavailable")}, {"pr": (0, "null", "")},
            {"pr": subprocess.TimeoutExpired("gh", 60)},
            {"events": {}}, {"events": []}, {"events": [[None]]},
            {"events": (0, "not JSON", "")},
            {"events": [[{"event": "labeled"}]]},
            {"events": [[{"event": "labeled", "label": {"name": ""}}]]},
            {"events": [[{"event": ""}]]},
            {"events": subprocess.TimeoutExpired("gh", 60)},
            {"rate": "invalid"}, {"rate": "nan"}, {"seed": ""},
            {"target": "unknown"},
            {"target": "https://github.com/other/repo/pull/1988"},
        ):
            with self.subTest(kwargs=kwargs):
                state, _, _ = self.run_card(**kwargs)
                self.assertEqual(state["tester_risk"]["decision"], "full_read")
                self.assertTrue(state["tester_risk"]["reason"])

    def test_tick_ordinary_work_does_not_fetch_pr_or_record_tester_risk(self):
        state, _, reads = self.run_card(verify=False)
        self.assertNotIn("tester_risk", state)
        self.assertNotIn("_tester_risk_cards", self.state)
        self.assertEqual(reads, [])

    def test_tick_keeps_the_decision_with_the_card_after_the_lane_is_released(self):
        lane, _, _ = self.run_card()
        card = self.state["_tester_risk_cards"][str(lane["issue"])]
        self.assertEqual(card, lane["tester_risk"])
        self.assertEqual(card["decision"], "sampled")
        self.assertTrue(card["reason"])

        lane, _, _ = self.run_card(
            {"changed_files": 20, "additions": 2, "deletions": 1, "labels": []},
            issue_number=2001)
        card = self.state["_tester_risk_cards"][str(lane["issue"])]
        self.assertEqual(card["decision"], "full_read")
        self.assertTrue(card["reason"])

    def test_tick_a_broken_reader_fails_closed_instead_of_breaking_the_dispatch(self):
        lane, task, _ = self.run_card(broken=True)
        risk = lane["tester_risk"]
        self.assertEqual(risk["decision"], "full_read")
        self.assertIn("unavailable", risk["reason"])
        self.assertIn(risk["decision"], task)

    def test_tick_without_the_classifier_still_dispatches_and_reads_everything(self):
        lane, task, _ = self.run_card(classifier=False)
        risk = lane["tester_risk"]
        self.assertEqual(risk["decision"], "full_read")
        self.assertIn("unavailable", risk["reason"])
        self.assertIn(risk["decision"], task)


if __name__ == "__main__":
    unittest.main()
