"""Tests for lib/dispatch/quota.py module."""

import email.utils
import importlib.util
from pathlib import Path
import time
import unittest

QUOTA_PATH = Path(__file__).resolve().parents[1] / "lib" / "dispatch" / "quota.py"
spec = importlib.util.spec_from_file_location("quota", QUOTA_PATH)
quota_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(quota_mod)

classify = quota_mod.classify
extract_cooldown = quota_mod.extract_cooldown

SAMPLE_CFG = {
    "copilot": {
        "quota_error_re": "402|quota exceeded",
        "cooldown_s": 3600,
        "scope": "lane",
    },
    "agy": {
        "quota_error_re": "429|RESOURCE_EXHAUSTED",
        "cooldown_s": 1800,
        "scope": "backend",
    },
}


class TestClassifyCopilot(unittest.TestCase):
    """Test classification for copilot backend with 402 and quota errors."""

    def test_copilot_default_402_match(self):
        text = "HTTP 402 Payment Required: monthly quota exceeded"
        res = classify("copilot", text, SAMPLE_CFG)
        self.assertIsNotNone(res)
        self.assertEqual(res["scope"], "lane")
        self.assertEqual(res["ttl_s"], 3600)
        self.assertEqual(res["reason"], "402")

    def test_copilot_quota_exceeded_phrase_match(self):
        text = "Error: Account quota exceeded for billing cycle."
        res = classify("copilot", text, SAMPLE_CFG)
        self.assertIsNotNone(res)
        self.assertEqual(res["scope"], "lane")
        self.assertEqual(res["ttl_s"], 3600)
        self.assertEqual(res["reason"], "quota exceeded")

    def test_copilot_retry_after_override(self):
        text = "HTTP 402: Payment Required. quota exceeded. Retry-After: 120"
        res = classify("copilot", text, SAMPLE_CFG)
        self.assertIsNotNone(res)
        self.assertEqual(res["scope"], "lane")
        self.assertEqual(res["ttl_s"], 120)

    def test_copilot_resets_in_override(self):
        text = "402 quota exceeded (resets in 45m)"
        res = classify("copilot", text, SAMPLE_CFG)
        self.assertIsNotNone(res)
        self.assertEqual(res["scope"], "lane")
        self.assertEqual(res["ttl_s"], 2700)


class TestClassifyAGY(unittest.TestCase):
    """Test classification for agy backend with 429 and RESOURCE_EXHAUSTED errors."""

    def test_agy_default_429_match(self):
        text = "Error 429: Rate limit reached"
        res = classify("agy", text, SAMPLE_CFG)
        self.assertIsNotNone(res)
        self.assertEqual(res["scope"], "backend")
        self.assertEqual(res["ttl_s"], 1800)
        self.assertEqual(res["reason"], "429")

    def test_agy_resource_exhausted_gemini_sample(self):
        text = (
            "google.api_core.exceptions.ResourceExhausted: 429 Quota exceeded for quota metric "
            "'GenerateContent requests per minute' and limit 'GenerateContent requests per minute per project'... "
            "Resets in 54s."
        )
        res = classify("agy", text, SAMPLE_CFG)
        self.assertIsNotNone(res)
        self.assertEqual(res["scope"], "backend")
        self.assertEqual(res["ttl_s"], 54)
        self.assertEqual(res["reason"], "429")

    def test_agy_grpc_resource_exhausted_long_resets_in(self):
        text = (
            "grpc._channel._InactiveRpcError: <_InactiveRpcError of RPC that terminated with: "
            "status = StatusCode.RESOURCE_EXHAUSTED, details = 'Resource has been exhausted. Resets in 131h29m32s.'>"
        )
        res = classify("agy", text, SAMPLE_CFG)
        self.assertIsNotNone(res)
        self.assertEqual(res["scope"], "backend")
        self.assertEqual(res["ttl_s"], 131 * 3600 + 29 * 60 + 32)
        self.assertEqual(res["reason"], "RESOURCE_EXHAUSTED")

    def test_agy_retry_after_override(self):
        text = "429 RESOURCE_EXHAUSTED: Rate limit exceeded. Retry-After: 300"
        res = classify("agy", text, SAMPLE_CFG)
        self.assertIsNotNone(res)
        self.assertEqual(res["scope"], "backend")
        self.assertEqual(res["ttl_s"], 300)

    def test_agy_case_insensitive_backend(self):
        text = "RESOURCE_EXHAUSTED: quota limit"
        res = classify("AGY", text, SAMPLE_CFG)
        self.assertIsNotNone(res)
        self.assertEqual(res["scope"], "backend")
        self.assertEqual(res["ttl_s"], 1800)


class TestExtractCooldown(unittest.TestCase):
    """Test extract_cooldown parser on various formats."""

    def test_resets_in_composite(self):
        self.assertEqual(extract_cooldown("Resets in 131h29m32s."), 473372)
        self.assertEqual(extract_cooldown("resets in 1h20m30s"), 4830)
        self.assertEqual(extract_cooldown("resets in 2h30m"), 9000)

    def test_resets_in_single_units(self):
        self.assertEqual(extract_cooldown("Resets in 54s"), 54)
        self.assertEqual(extract_cooldown("resets in 10m"), 600)
        self.assertEqual(extract_cooldown("resets in 2h"), 7200)
        self.assertEqual(extract_cooldown("resets in 60 seconds"), 60)
        self.assertEqual(extract_cooldown("resets in 5 minutes"), 300)
        self.assertEqual(extract_cooldown("resets in 1 hour"), 3600)

    def test_resets_in_plain_number(self):
        self.assertEqual(extract_cooldown("resets in 120"), 120)

    def test_retry_after_seconds_and_units(self):
        self.assertEqual(extract_cooldown("Retry-After: 120"), 120)
        self.assertEqual(extract_cooldown("retry-after: 3600"), 3600)
        self.assertEqual(extract_cooldown("Retry-After: 45s"), 45)
        self.assertEqual(extract_cooldown("Retry-After: 5 mins"), 300)
        self.assertEqual(extract_cooldown("Retry-After: 2 hours"), 7200)

    def test_retry_after_json_format(self):
        self.assertEqual(extract_cooldown('{"error": "rate_limited", "retry_after": 60}'), 60)
        self.assertEqual(extract_cooldown('{"retry-after": "180"}'), 180)

    def test_retry_after_http_date(self):
        future_epoch = time.time() + 250
        http_date = email.utils.formatdate(future_epoch, usegmt=True)
        dur = extract_cooldown(f"HTTP/1.1 429 Too Many Requests\r\nRetry-After: {http_date}")
        self.assertIsNotNone(dur)
        self.assertTrue(245 <= dur <= 255)

    def test_extract_cooldown_empty_or_none(self):
        self.assertIsNone(extract_cooldown(""))
        self.assertIsNone(extract_cooldown(None))
        self.assertIsNone(extract_cooldown("No cooldown here"))


class TestUnmatchedAndEdgeCases(unittest.TestCase):
    """Test non-matching samples and edge case handling."""

    def test_successful_output_unmatched(self):
        self.assertIsNone(classify("copilot", "Task completed with exit code 0", SAMPLE_CFG))
        self.assertIsNone(classify("agy", "SUCCESS: all files processed", SAMPLE_CFG))

    def test_other_http_errors_unmatched(self):
        self.assertIsNone(classify("copilot", "HTTP 500: Internal Server Error", SAMPLE_CFG))
        self.assertIsNone(classify("copilot", "401 Unauthorized: Invalid token", SAMPLE_CFG))
        self.assertIsNone(classify("agy", "403 Forbidden: User not permitted", SAMPLE_CFG))
        self.assertIsNone(classify("agy", "404 Not Found: Model not registered", SAMPLE_CFG))

    def test_unknown_backend(self):
        self.assertIsNone(classify("claude", "429 RESOURCE_EXHAUSTED", SAMPLE_CFG))

    def test_empty_or_none_inputs(self):
        self.assertIsNone(classify("copilot", None, SAMPLE_CFG))
        self.assertIsNone(classify("copilot", "", SAMPLE_CFG))
        self.assertIsNone(classify("copilot", "   ", SAMPLE_CFG))
        self.assertIsNone(classify("copilot", "402", {}))
        self.assertIsNone(classify("copilot", "402", None))
        self.assertIsNone(classify(None, "402", SAMPLE_CFG))

    def test_custom_reason_and_scope(self):
        cfg = {
            "custom": {
                "quota_error_re": "out of tokens",
                "cooldown_s": 600,
                "scope": "worktree",
                "reason": "token_exhaustion",
            }
        }
        res = classify("custom", "Fatal: out of tokens in prompt", cfg)
        self.assertIsNotNone(res)
        self.assertEqual(res["scope"], "worktree")
        self.assertEqual(res["ttl_s"], 600)
        self.assertEqual(res["reason"], "token_exhaustion")

    def test_default_scope_fallback(self):
        cfg = {
            "backend_x": {
                "quota_error_re": "quota limit",
                "cooldown_s": 500,
            }
        }
        res = classify("backend_x", "hit quota limit", cfg)
        self.assertIsNotNone(res)
        self.assertEqual(res["scope"], "backend")
        self.assertEqual(res["ttl_s"], 500)


if __name__ == "__main__":
    unittest.main()
