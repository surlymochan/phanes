"""Usage count parsing for Cursor and Pi events."""
import json, os, sys, tempfile, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _load  # noqa: E402

xs = _load.load()


class UsageCounts(unittest.TestCase):
    def test_cursor_missing_null_and_boolean_counts_are_zero(self):
        raw = {
            "inputTokens": 7,
            "outputTokens": None,
            "cacheReadTokens": True,
            "cacheWriteTokens": "12",
        }
        self.assertEqual(xs.cursor_usage(raw), {
            "inputTokens": 7,
            "outputTokens": 0,
            "cacheReadTokens": 0,
            "cacheWriteTokens": 0,
        })

    def test_cursor_negative_counts_do_not_enter_summary(self):
        raw = {"inputTokens": -5, "outputTokens": 3}
        usage = xs.cursor_usage(raw)
        self.assertEqual(usage["inputTokens"], 0)
        self.assertEqual(usage["outputTokens"], 3)
        self.assertTrue(all(value >= 0 for value in usage.values()))

    def test_pi_missing_null_and_boolean_counts_are_zero(self):
        event = {"type": "message_end", "message": {
            "role": "assistant",
            "usage": {"input": 7, "output": None, "cacheRead": True, "cacheWrite": "12"},
            "content": [],
        }}
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8") as fh:
            fh.write(json.dumps(event) + "\n")
            fh.flush()
            usage, _, errors = xs.pi_usage(fh.name)
        self.assertEqual(usage, {"input": 7, "output": 0, "cacheRead": 0, "cacheWrite": 0})
        self.assertEqual(errors, [])

    def test_pi_negative_counts_do_not_enter_summary(self):
        event = {"type": "message_end", "message": {
            "role": "assistant",
            "usage": {"input": -5, "output": 3},
            "content": [],
        }}
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8") as fh:
            fh.write(json.dumps(event) + "\n")
            fh.flush()
            usage, _, errors = xs.pi_usage(fh.name)
        self.assertEqual(usage["input"], 0)
        self.assertEqual(usage["output"], 3)
        self.assertTrue(all(value >= 0 for value in usage.values()))
        self.assertEqual(errors, [])

    def test_valid_fractional_counts_keep_integer_normalization(self):
        cursor = xs.cursor_usage({"inputTokens": 4.8, "outputTokens": 2})
        self.assertEqual(cursor["inputTokens"], 4)
        event = {"type": "message_end", "message": {
            "role": "assistant", "usage": {"input": 4.8, "output": 2}, "content": [],
        }}
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8") as fh:
            fh.write(json.dumps(event) + "\n")
            fh.flush()
            pi, _, _ = xs.pi_usage(fh.name)
        self.assertEqual(pi["input"], 4)
        self.assertEqual(pi["output"], 2)


if __name__ == "__main__":
    unittest.main()
