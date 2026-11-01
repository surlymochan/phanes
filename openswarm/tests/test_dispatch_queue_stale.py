"""Stale search-index handling for the dispatch queue (Refs #1513)."""
import importlib.util
import pathlib
import time
import unittest

MODULE = pathlib.Path(__file__).resolve().parent.parent / "lib" / "dispatch" / "queue.py"
spec = importlib.util.spec_from_file_location("dispatch_queue_stale", MODULE)
dispatch_queue = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dispatch_queue)


def issue(number, *labels):
    return {"number": number, "title": "issue {}".format(number), "labels": list(labels)}


class StaleQueueTests(unittest.TestCase):
    def test_authoritative_in_progress_label_drops_and_counts_stale(self):
        queue = dispatch_queue.build_queue([issue(1410, "lane-ready")], authoritative={1410: ["lane-ready", "in-progress"]})
        self.assertEqual(queue, [])
        self.assertEqual(queue.dropped, [{"number": 1410, "reason": "stale_index"}])
        self.assertEqual(dispatch_queue.queue_stats(queue), {"ready": 1, "dispatchable": 0, "stale": 1, "external": 0})

    def test_without_authoritative_labels_preserves_search_behavior(self):
        rows = [issue(1, "lane-ready", "in-progress"), issue(2, "lane-ready")]
        queue = dispatch_queue.build_queue(rows)
        self.assertEqual([row["number"] for row in queue], [2])
        self.assertEqual(queue.dropped, [])

    def test_skip_limit_does_not_strip_before_minimum_age(self):
        skips = dispatch_queue.StaleSkips()
        for _ in range(5):
            skips.note(12, time.time())
        self.assertFalse(skips.should_strip(12, min_age_s=600))

    def test_serialization_round_trip(self):
        skips = dispatch_queue.StaleSkips()
        skips.note(12, 1000)
        skips.note(12, 1010)
        restored = dispatch_queue.StaleSkips.from_json(skips.to_json())
        self.assertEqual(restored.to_json(), skips.to_json())

    def test_stats_separate_external_backend_rows(self):
        queue = dispatch_queue.build_queue([issue(1, "lane-ready"), issue(2, "lane-ready", "backend:pi")])
        self.assertEqual(dispatch_queue.queue_stats(queue), {"ready": 2, "dispatchable": 1, "stale": 0, "external": 1})


if __name__ == "__main__":
    unittest.main()
