"""Per-command risk denial: which risks a lane may refuse and continue, and how each one is explained."""
import unittest

import _load

xs = _load.load()

WD = "/w/tree"
RM = {"risk": "rm outside worktree", "command": "rm -rf /etc/x"}
SECRET = {"risk": "secret print", "command": "cat ~/.config/provider.json"}


class SecretPrintDenialTests(unittest.TestCase):
    def test_secret_print_is_deniable_for_pi_and_codex(self):
        for backend in ("pi", "codex"):
            with self.subTest(backend=backend):
                self.assertTrue(xs.lane_risk_deniable({"backend": backend}, SECRET, {}))

    def test_secret_print_still_aborts_copilot(self):
        self.assertFalse(xs.lane_risk_deniable({"backend": "copilot"}, SECRET, {}))

    def test_push_publish_sudo_remain_non_deniable(self):
        lane = {"backend": "pi"}
        for risk in ("push", "publish", "sudo"):
            with self.subTest(risk=risk):
                self.assertFalse(xs.lane_risk_deniable(lane, {"risk": risk}, {}))

    def test_abort_requested_stops_denial(self):
        self.assertFalse(xs.lane_risk_deniable({"backend": "pi"}, SECRET, {"abort_requested": True}))


class PerRiskCountTests(unittest.TestCase):
    def test_limit_is_counted_per_risk_type(self):
        lane = {"backend": "pi"}
        spec = {"risk_denials": [dict(RM) for _ in range(xs.MAX_RISK_DENIALS)]}
        self.assertFalse(xs.lane_risk_deniable(lane, RM, spec))
        self.assertTrue(xs.lane_risk_deniable(lane, SECRET, spec))

    def test_secret_print_limit_is_reached_independently(self):
        lane = {"backend": "codex"}
        spec = {"risk_denials": [dict(SECRET) for _ in range(xs.MAX_RISK_DENIALS - 1)]}
        self.assertTrue(xs.lane_risk_deniable(lane, SECRET, spec))
        spec["risk_denials"].append(dict(SECRET))
        self.assertFalse(xs.lane_risk_deniable(lane, SECRET, spec))
        self.assertTrue(xs.lane_risk_deniable(lane, RM, spec))


class RiskDenialPromptTests(unittest.TestCase):
    def test_rm_outside_worktree_keeps_deletion_wording(self):
        prompt = xs.risk_denial_prompt(RM, WD)
        self.assertIn("deleting paths outside the worktree /w/tree is not allowed", prompt)
        self.assertIn("$TMPDIR", prompt)

    def test_secret_print_prompt_explains_secret_handling(self):
        prompt = xs.risk_denial_prompt(SECRET, WD)
        self.assertIn("printing secrets or configuration file contents is not allowed", prompt)
        self.assertIn("only key names", prompt)
        self.assertNotIn("deleting paths", prompt)
        self.assertNotIn("worktree", prompt)

    def test_generic_risk_prompt_does_not_claim_deletion(self):
        prompt = xs.risk_denial_prompt({"risk": "push", "command": "git push"}, WD)
        self.assertIn("(push)", prompt)
        self.assertNotIn("deleting paths", prompt)
        self.assertNotIn("secret", prompt)

    def test_every_prompt_keeps_the_report_instruction(self):
        for block in (RM, SECRET, {"risk": "push"}):
            with self.subTest(risk=block["risk"]):
                self.assertIn(xs.REPORT_INSTRUCTION, xs.risk_denial_prompt(block, WD))
                self.assertIn("Do not retry it", xs.risk_denial_prompt(block, WD))


if __name__ == "__main__":
    unittest.main()
