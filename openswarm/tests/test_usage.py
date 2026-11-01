"""Pure unit tests for executor usage parsers."""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _load  # noqa: E402

xs = _load.load()


class UsageParsers(unittest.TestCase):
    def test_cursor_usage_normalizes_counts(self):
        empty = {"inputTokens": 0, "outputTokens": 0,
                 "cacheReadTokens": 0, "cacheWriteTokens": 0}
        self.assertEqual(xs.cursor_usage(None), empty)
        self.assertEqual(xs.cursor_usage("not an object"), empty)
        self.assertEqual(xs.cursor_usage({"inputTokens": 7, "outputTokens": None,
                                          "cacheReadTokens": "bad"}),
                         {"inputTokens": 7, "outputTokens": 0,
                          "cacheReadTokens": 0, "cacheWriteTokens": 0})
        self.assertEqual(xs.cursor_usage({"inputTokens": "8", "outputTokens": 2.9,
                                          "cacheReadTokens": -1, "cacheWriteTokens": True}),
                         {"inputTokens": 8, "outputTokens": 2,
                          "cacheReadTokens": 0, "cacheWriteTokens": 0})
        self.assertEqual(xs.cursor_usage({"inputTokens": False}), empty)

    def test_pi_usage_reads_assistant_message_end_events(self):
        empty = {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}
        with tempfile.TemporaryDirectory(prefix="openswarm-usage-") as directory:
            missing = os.path.join(directory, "missing.jsonl")
            self.assertEqual(xs.pi_usage(missing), (empty, None, []))

            events_path = os.path.join(directory, "events.jsonl")
            events = [
                "not json",
                {"type": "message_end", "message": {"role": "user",
                                                        "usage": {"input": 999}}},
                {"type": "message_end", "message": {"role": "assistant",
                    "usage": {"input": 2, "output": 3, "cacheRead": 4, "cacheWrite": 5},
                    "content": [{"type": "text", "text": "first"}]}},
                {"type": "message_end", "message": {"role": "assistant",
                    "usage": {"input": 6, "output": 7, "cacheRead": 8, "cacheWrite": 9},
                    "content": [{"type": "text", "text": "last"}],
                    "stopReason": "error", "errorMessage": "e" * 600}},
                {"type": "message_end", "message": {"role": "assistant",
                    "usage": {"input": "bad", "output": None},
                    "content": [{"type": "text", "text": "ignored?"}],
                    "stopReason": "end_turn"}},
                {"type": "message_end", "message": {"role": "assistant",
                    "usage": {"input": True, "output": False, "cacheRead": -3},
                    "content": []}},
                {"type": "message_start", "message": {"role": "assistant",
                    "content": [{"type": "text", "text": "not final"}]}},
            ]
            with open(events_path, "w") as stream:
                for event in events:
                    stream.write(event if isinstance(event, str) else json.dumps(event))
                    stream.write("\n")

            usage, final_text, errors = xs.pi_usage(events_path)
            self.assertEqual(usage, {"input": 8, "output": 10,
                                     "cacheRead": 12, "cacheWrite": 14})
            self.assertEqual(final_text, "ignored?")
            self.assertEqual(errors, ["e" * 500])


if __name__ == "__main__":
    unittest.main()
