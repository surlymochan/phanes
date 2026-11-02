"""Copilot ACP model checks: parsing usage multipliers, the four check outcomes, and the doctor lane check."""
import json
import os
import shlex
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import _load  # noqa: E402

xs = _load.load()
models = xs.copilot_models_module()

SESSION_NEW = {
    "jsonrpc": "2.0", "id": "session-0001",
    "result": {"sessionId": "sess-redacted", "models": {"currentModelId": "claude-haiku-5.5", "availableModels": [
        {"modelId": "claude-haiku-5.5", "name": "Claude Haiku 5.5",
         "_meta": {"copilotUsage": "1x", "copilotEnablement": "enabled", "copilotPriceCategory": "standard"}},
        {"modelId": "claude-haiku-4.5", "name": "Claude Haiku 4.5",
         "_meta": {"copilotUsage": "0.33x", "copilotEnablement": "enabled", "copilotPriceCategory": "low"}},
        {"modelId": "claude-opus-redacted", "name": "Claude Opus (redacted)",
         "_meta": {"copilotUsage": "3x", "copilotEnablement": "disabled", "copilotPriceCategory": "premium"}},
    ]}},
}


def event_log(*events):
    return "\n".join(json.dumps(event) for event in events) + "\n"


class ParseMultiplierTests(unittest.TestCase):
    def test_multiplier_strings(self):
        self.assertEqual(models.parse_multiplier("1x"), 1.0)
        self.assertEqual(models.parse_multiplier("0.33x"), 0.33)
        self.assertEqual(models.parse_multiplier(" 2X "), 2.0)
        self.assertEqual(models.parse_multiplier(1.5), 1.5)

    def test_missing_or_unparsable_multiplier_is_none(self):
        self.assertIsNone(models.parse_multiplier(None))
        self.assertIsNone(models.parse_multiplier(""))
        self.assertIsNone(models.parse_multiplier("cheap"))
        self.assertIsNone(models.parse_multiplier(True))
        self.assertIsNone(models.parse_multiplier("-1x"))


class ParseAvailableModelsTests(unittest.TestCase):
    def test_reads_jsonl_session_reply(self):
        parsed = models.parse_available_models(event_log({"type": "session.start"}, SESSION_NEW))
        self.assertEqual([m["value"] for m in parsed],
                         ["claude-haiku-5.5", "claude-haiku-4.5", "claude-opus-redacted"])
        haiku45 = parsed[1]
        self.assertEqual(haiku45["usage_multiplier"], 0.33)
        self.assertTrue(haiku45["enabled"])
        self.assertEqual(haiku45["price_category"], "low")
        self.assertFalse(parsed[2]["enabled"])

    def test_accepts_json_document_and_event_list(self):
        self.assertEqual(len(models.parse_available_models(json.dumps(SESSION_NEW))), 3)
        self.assertEqual(len(models.parse_available_models([SESSION_NEW])), 3)

    def test_missing_meta_is_not_enabled_and_has_unknown_multiplier(self):
        doc = {"result": {"models": {"availableModels": [{"modelId": "bare-model"}]}}}
        [entry] = models.parse_available_models(doc)
        self.assertFalse(entry["enabled"])
        self.assertIsNone(entry["usage_multiplier"])
        self.assertIsNone(entry["price_category"])

    def test_enablement_is_fail_closed_for_unknown_shapes(self):
        def enabled(value):
            doc = {"availableModels": [{"modelId": "m", "_meta": {"copilotEnablement": value}}]}
            return models.parse_available_models(doc)[0]["enabled"]
        self.assertTrue(enabled("enabled"))
        self.assertTrue(enabled(" Enabled "))
        self.assertTrue(enabled(True))
        self.assertFalse(enabled("pending"))
        self.assertFalse(enabled({"state": "enabled"}))
        self.assertFalse(enabled(False))
        self.assertFalse(enabled(None))

    def test_ignores_non_model_noise(self):
        self.assertEqual(models.parse_available_models("not json\n{}\n"), [])
        self.assertEqual(models.parse_available_models(None), [])
        self.assertEqual(models.parse_available_models({"availableModels": [{"name": "no id"}, "x"]}), [])


class CheckTests(unittest.TestCase):
    def setUp(self):
        self.models = models.parse_available_models(event_log(SESSION_NEW))

    def test_ok_within_cap(self):
        result = models.check("claude-haiku-5.5", self.models, 1.0)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["usage_multiplier"], 1.0)

    def test_zero_cap_allows_only_included_models(self):
        included = models.parse_available_models({"availableModels": [
            {"modelId": "included-model", "_meta": {"copilotUsage": "0x", "copilotEnablement": "enabled"}}]})
        self.assertEqual(models.check("included-model", included, 0.0)["status"], "ok")
        self.assertEqual(models.check("claude-haiku-4.5", self.models, 0.0)["status"], "too_expensive")
        self.assertEqual(models.check("claude-haiku-5.5", self.models, 0.0)["status"], "too_expensive")

    def test_cheaper_model_is_ok_under_fractional_cap(self):
        self.assertEqual(models.check("claude-haiku-4.5", self.models, 0.33)["status"], "ok")

    def test_suffixed_config_value_matches_base_model_id(self):
        suffixed = models.parse_available_models({"availableModels": [
            {"modelId": "composer-2.5[fast=false]", "_meta": {"copilotUsage": "1x", "copilotEnablement": "enabled"}}]})
        self.assertEqual(models.check("composer-2.5", suffixed, 1.0)["status"], "ok")

    def test_missing(self):
        self.assertEqual(models.check("claude-gone", self.models, 1.0)["status"], "missing")

    def test_disabled(self):
        self.assertEqual(models.check("claude-opus-redacted", self.models, 10.0)["status"], "disabled")

    def test_too_expensive(self):
        result = models.check("claude-haiku-5.5", self.models, 0.5)
        self.assertEqual(result["status"], "too_expensive")
        self.assertIn("1x", result["detail"])

    def test_unknown_multiplier_fails_closed(self):
        bare = models.parse_available_models({"availableModels": [
            {"modelId": "bare-model", "_meta": {"copilotEnablement": "enabled"}}]})
        self.assertEqual(models.check("bare-model", bare, 1.0)["status"], "too_expensive")


class DoctorCopilotModelsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="openswarm-copilot-models-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.runs = self.tmp / "runs"
        self.runs.mkdir()
        patcher = mock.patch.object(xs, "RUNS", str(self.runs))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.lane = {"name": "G1", "backend": "copilot", "model": "claude-haiku-5.5"}

    def run_dir(self, rid, lane="G1", backend="copilot", events=None):
        rdir = self.runs / rid
        rdir.mkdir()
        (rdir / "status.json").write_text(json.dumps({"lane": lane, "backend": backend}))
        if events is not None:
            (rdir / "events.1.copilot.jsonl").write_text(events)

    def check(self, policy=None):
        return xs.doctor_copilot_models_check(self.lane, policy or {"copilot": {"max_usage_multiplier": 1.0}})

    def test_warns_without_any_run(self):
        result = self.check()
        self.assertEqual(result["status"], "warn")
        self.assertEqual(result["name"], "copilot_models")

    def test_uses_latest_run_of_this_lane(self):
        self.run_dir("20260101-000000-a", events=event_log(SESSION_NEW))
        self.run_dir("20260102-000000-b", lane="other", events=event_log(SESSION_NEW))
        self.run_dir("20260103-000000-c", lane="G1", events=event_log(SESSION_NEW))
        self.assertEqual(self.check()["status"], "ok")

    def test_fails_when_model_is_too_expensive_for_policy(self):
        self.run_dir("20260101-000000-a", events=event_log(SESSION_NEW))
        result = self.check({"copilot": {"max_usage_multiplier": 0.5}})
        self.assertEqual(result["status"], "fail")
        self.assertIn("above the cap", result["detail"])
        self.assertTrue(result["fix"])

    def test_default_cap_is_one_when_policy_omits_it(self):
        self.run_dir("20260101-000000-a", events=event_log(SESSION_NEW))
        self.lane["model"] = "claude-opus-redacted"
        self.assertEqual(self.check({"copilot": {}})["status"], "fail")
        self.lane["model"] = "claude-haiku-4.5"
        self.assertEqual(self.check({"copilot": {}})["status"], "ok")

    def test_fails_on_missing_model(self):
        self.run_dir("20260101-000000-a", events=event_log(SESSION_NEW))
        self.lane["model"] = "claude-gone"
        self.assertEqual(self.check()["status"], "fail")

    def test_warns_when_last_run_has_no_model_list(self):
        self.run_dir("20260101-000000-a", events=event_log({"type": "error"}))
        self.assertEqual(self.check()["status"], "warn")

    def test_doctor_report_includes_copilot_check_for_copilot_lanes_only(self):
        self.run_dir("20260101-000000-a", events=event_log(SESSION_NEW))
        policy = {**xs.load_policy(), "copilot": {"max_usage_multiplier": 1.0}}
        report = xs.doctor_lane_report(self.lane, policy, system="linux")
        self.assertIn("copilot_models", [c["name"] for c in report["checks"]])
        codex = {"name": "C1", "backend": "codex", "model": "gpt-6-luna"}
        report = xs.doctor_lane_report(codex, policy, system="linux")
        self.assertNotIn("copilot_models", [c["name"] for c in report["checks"]])


class CopilotLaneEndToEndTests(unittest.TestCase):
    """Real LaneChild + lane_begin_turn against a fake `copilot --acp`; doctor reads what the lane logged."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="openswarm-copilot-e2e-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.runs = self.tmp / "runs"
        self.runs.mkdir()
        patcher = mock.patch.object(xs, "RUNS", str(self.runs))
        patcher.start()
        self.addCleanup(patcher.stop)

    def wrapper(self, entries):
        path = self.tmp / "copilot"
        fixture = HERE / "fixtures" / "fake_copilot_acp.py"
        models_json = shlex.quote(json.dumps(entries))
        path.write_text(f"#!/bin/sh\nFAKE_COPILOT_MODELS={models_json} exec {shlex.quote(sys.executable)} "
                        f"{shlex.quote(str(fixture))} \"$@\"\n")
        path.chmod(0o755)
        return str(path)

    def run_lane(self, model, entries):
        """Start one copilot turn through the real ACP path; returns (error or None, doctor result)."""
        rdir = self.runs / "20260103-000000-e2e"
        rdir.mkdir()
        (rdir / "status.json").write_text(json.dumps({"lane": "G1", "backend": "copilot"}))
        event_file = rdir / "events.1.copilot.jsonl"
        event_file.write_text("")
        lane = {"name": "G1", "backend": "copilot", "model": model, "effort": "medium",
                "binary": self.wrapper(entries)}
        policy = xs.load_policy()
        child = xs.LaneChild(lane, str(self.tmp), policy)
        self.addCleanup(child.close)
        error = None
        try:
            xs.lane_begin_turn(lane, child, "prompt", str(self.tmp), str(event_file), {})
        except RuntimeError as exc:
            error = exc
        return error, event_file

    def doctor(self, model, cap=1.0):
        return xs.doctor_copilot_models_check({"name": "G1", "backend": "copilot", "model": model},
                                              {"copilot": {"max_usage_multiplier": cap}})

    def test_listed_enabled_model_within_cap_is_ok(self):
        error, events = self.run_lane("claude-haiku-5.5", [
            {"modelId": "claude-haiku-5.5", "copilotUsage": "1x", "copilotEnablement": "enabled"},
            {"modelId": "claude-haiku-4.5", "copilotUsage": "0.33x", "copilotEnablement": "enabled"}])
        self.assertIsNone(error)
        self.assertIn("availableModels", events.read_text())
        self.assertEqual(self.doctor("claude-haiku-5.5")["status"], "ok")

    def test_model_removed_from_list_fails_doctor_and_lane(self):
        error, events = self.run_lane("claude-gone", [
            {"modelId": "claude-haiku-5.5", "copilotUsage": "1x", "copilotEnablement": "enabled"}])
        self.assertIsNotNone(error)
        self.assertIn("unavailable", str(error))
        self.assertIn("availableModels", events.read_text())
        self.assertEqual(self.doctor("claude-gone")["status"], "fail")

    def test_disabled_model_fails_doctor(self):
        error, _ = self.run_lane("claude-opus-redacted", [
            {"modelId": "claude-opus-redacted", "copilotUsage": "3x", "copilotEnablement": "disabled"}])
        self.assertIsNone(error)
        result = self.doctor("claude-opus-redacted", cap=10.0)
        self.assertEqual(result["status"], "fail")
        self.assertIn("not enabled", result["detail"])

    def test_model_above_policy_cap_fails_doctor(self):
        error, _ = self.run_lane("claude-opus-redacted", [
            {"modelId": "claude-opus-redacted", "copilotUsage": "3x", "copilotEnablement": "enabled"}])
        self.assertIsNone(error)
        result = self.doctor("claude-opus-redacted", cap=1.0)
        self.assertEqual(result["status"], "fail")
        self.assertIn("above the cap", result["detail"])


if __name__ == "__main__":
    unittest.main()
