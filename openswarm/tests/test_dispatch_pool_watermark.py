"""Tests for lib/dispatch/pool_watermark.py (Refs #1559).

``openswarm/lib`` and ``openswarm/lib/dispatch`` are namespace packages (no
``__init__.py``), so the module is loaded by file path instead of by import.
"""
import importlib.util
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "lib" / "dispatch" / "pool_watermark.py"
spec = importlib.util.spec_from_file_location("dispatch_pool_watermark", MODULE_PATH)
pool_watermark = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pool_watermark)

# 2026-11-02 06:11, the sample of the issue: PAUSE-MINT stopped the minting, the
# dispatchable pool was empty and ten MiniMax (pi) lanes were idle.
AT_0611 = 1793579460.0


def card(number, *labels, **kwargs):
    """One card record: ``{"number", "labels", "deps_open"}``."""
    return {
        "number": number,
        "labels": list(labels),
        "deps_open": kwargs.get("deps_open", 0),
    }


def lane(name, backend, **kwargs):
    """One lane record: ``{"lane", "backend"}`` plus an optional state."""
    record = {"lane": name, "backend": backend}
    record.update(kwargs)
    return record


class PoolWatermarkTests(unittest.TestCase):
    def test_0611_sample_is_empty_pool_with_ten_idle_pi_lanes(self):
        lanes = [lane("pi-{}".format(index), "pi") for index in range(10)]
        result = pool_watermark.pool_watermark([], lanes, 24, AT_0611)

        self.assertEqual(result["ready"], 0)
        self.assertEqual(result["level"], "empty")
        self.assertEqual(result["deficit"], 24)
        self.assertEqual(result["idle_lanes"], 10)
        self.assertEqual(result["idle_by_backend"], {"pi": 10})
        self.assertEqual(result["per_backend_deficit"], {"pi": 10})
        self.assertEqual(result["now"], AT_0611)

    def test_external_cards_are_not_dispatchable(self):
        issues = [
            card(1, "lane-ready", "backend:external"),
            card(2, "lane-ready", "backend:external-muse"),
            card(3, "lane-ready", "backend:chatgpt-agent"),
        ]
        result = pool_watermark.pool_watermark(issues, [], 10, AT_0611)

        self.assertEqual(result["ready"], 0)
        self.assertEqual(result["level"], "empty")
        self.assertEqual(result["blocked"]["external"], 3)

    def test_card_with_open_dependency_is_not_dispatchable(self):
        issues = [
            card(1, "lane-ready", "backend:pi", deps_open=True),
            card(2, "lane-ready", "backend:pi", deps_open=["#7"]),
            card(3, "lane-ready", "backend:pi"),
        ]
        result = pool_watermark.pool_watermark(issues, [], 10, AT_0611)

        self.assertEqual(result["ready"], 1)
        self.assertEqual(result["blocked"]["deps_open"], 2)
        self.assertEqual(result["by_backend"], {"pi": 1})

    def test_in_progress_cards_are_not_dispatchable(self):
        issues = [
            card(1, "lane-ready", "in-progress", "backend:pi"),
            card(2, "p1", "backend:pi"),
        ]
        result = pool_watermark.pool_watermark(issues, [], 10, AT_0611)

        self.assertEqual(result["ready"], 0)
        self.assertEqual(result["blocked"]["in_progress"], 1)
        self.assertEqual(result["blocked"]["not_ready"], 1)

    def test_per_backend_deficit_counts_idle_lanes_without_a_card(self):
        lanes = [
            lane("pi-1", "pi"), lane("pi-2", "pi"), lane("pi-3", "pi"),
            lane("codex-1", "codex"), lane("codex-2", "codex"),
        ]
        issues = [card(1, "lane-ready", "backend:pi"), card(2, "lane-ready", "backend:codex")]
        result = pool_watermark.pool_watermark(issues, lanes, 10, AT_0611)

        self.assertEqual(result["ready"], 2)
        self.assertEqual(result["by_backend"], {"codex": 1, "pi": 1})
        self.assertEqual(result["idle_by_backend"], {"codex": 2, "pi": 3})
        self.assertEqual(result["per_backend_deficit"], {"codex": 1, "pi": 2})
        self.assertEqual(result["deficit"], 8)

    def test_level_ok_low_and_empty(self):
        issues = [card(index, "lane-ready", "backend:pi") for index in (1, 2, 3)]
        lanes = [lane("pi-1", "pi")]

        self.assertEqual(pool_watermark.pool_watermark(issues, lanes, 3, AT_0611)["level"], "ok")
        self.assertEqual(pool_watermark.pool_watermark(issues, lanes, 4, AT_0611)["level"], "low")
        self.assertEqual(pool_watermark.pool_watermark([], lanes, 4, AT_0611)["level"], "empty")

    def test_deficit_is_never_negative(self):
        issues = [card(1, "lane-ready", "backend:pi")]
        result = pool_watermark.pool_watermark(issues, [], 0, AT_0611)

        self.assertEqual(result["deficit"], 0)
        self.assertEqual(result["level"], "ok")

    def test_unpinned_cards_count_in_ready_but_belong_to_no_backend(self):
        issues = [card(1, "lane-ready"), card(2, "lane-ready", "backend:pi")]
        lanes = [lane("pi-1", "pi"), lane("codex-1", "codex")]
        result = pool_watermark.pool_watermark(issues, lanes, 10, AT_0611)

        self.assertEqual(result["ready"], 2)
        self.assertEqual(result["unpinned"], 1)
        self.assertEqual(result["by_backend"], {"codex": 0, "pi": 1})
        self.assertEqual(result["per_backend_deficit"], {"codex": 1, "pi": 0})

    def test_busy_draining_and_paused_lanes_are_not_idle(self):
        lanes = [
            lane("pi-1", "pi", state="busy"),
            lane("pi-2", "pi", issue=42),
            lane("pi-3", "pi", drain=True),
            lane("pi-4", "pi", paused=True),
            lane("pi-5", "pi", idle=True),
        ]
        result = pool_watermark.pool_watermark([], lanes, 10, AT_0611)

        self.assertEqual(result["total_lanes"], 5)
        self.assertEqual(result["idle_lanes"], 1)
        self.assertEqual(result["idle_by_backend"], {"pi": 1})
        self.assertEqual(result["per_backend_deficit"], {"pi": 1})

    def test_lanes_may_be_a_mapping_of_lane_name_to_record(self):
        lanes = {"pi-1": {"backend": "pi"}, "pi-2": {"backend": "pi", "state": "idle"}}
        result = pool_watermark.pool_watermark([], lanes, 10, AT_0611)

        self.assertEqual(result["idle_lanes"], 2)
        self.assertEqual(result["per_backend_deficit"], {"pi": 2})

    def test_issues_may_be_a_mapping_of_issue_number_to_record(self):
        issues = {"1": {"labels": ["lane-ready", "backend:pi"], "deps_open": 0}}
        result = pool_watermark.pool_watermark(issues, [], 10, AT_0611)

        self.assertEqual(result["ready"], 1)
        self.assertEqual(result["by_backend"], {"pi": 1})

    def test_labels_may_be_dicts_and_case_is_ignored(self):
        issues = [{"number": 1, "labels": [{"name": "lane-ready"}, {"name": "backend:pi"}]}]
        result = pool_watermark.pool_watermark(issues, [], 10, AT_0611)

        self.assertEqual(result["ready"], 1)
        self.assertEqual(result["by_backend"], {"pi": 1})

    def test_invalid_arguments_are_rejected(self):
        with self.assertRaises(ValueError):
            pool_watermark.pool_watermark([], [], 10, "now")
        with self.assertRaises(ValueError):
            pool_watermark.pool_watermark([], [], -1, AT_0611)
        with self.assertRaises(ValueError):
            pool_watermark.pool_watermark([], [], 1.5, AT_0611)


if __name__ == "__main__":
    unittest.main()
