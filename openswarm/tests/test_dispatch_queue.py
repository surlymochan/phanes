"""Dispatch queue: ordering, backend pinning and the shadow plan (Refs #1394).

The module is loaded by file path: ``openswarm/lib/dispatch`` is a namespace package
(no ``__init__.py``), so it cannot be imported as a package from the repository root.
"""
import importlib.util
import pathlib
import unittest

HERE = pathlib.Path(__file__).resolve().parent
MODULE = HERE.parent / "lib" / "dispatch" / "queue.py"

spec = importlib.util.spec_from_file_location("dispatch_queue", MODULE)
dispatch_queue = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dispatch_queue)


def issue(number, *labels, **kwargs):
    """One `gh issue list --json number,title,labels,body` row."""
    row = {
        "number": number,
        "title": kwargs.get("title", "issue {}".format(number)),
        "labels": list(labels),
        "body": kwargs.get("body", ""),
    }
    if "state" in kwargs:
        row["state"] = kwargs["state"]
    return row


class BuildQueueTests(unittest.TestCase):
    """The queue keeps the ready issues and orders them by priority, then by number."""

    def test_orders_by_priority_then_by_issue_number(self):
        rows = [
            issue(1, "lane-ready"),
            issue(2, "lane-ready", "p2"),
            issue(3, "lane-ready", "p1"),
            issue(4, "lane-ready", "p0"),
            issue(5, "lane-ready", "p1"),
        ]

        queue = dispatch_queue.build_queue(rows)

        self.assertEqual([entry["number"] for entry in queue], [4, 3, 5, 2, 1])
        self.assertEqual([entry["priority"] for entry in queue], [0, 1, 1, 2, 3])

    def test_unlabelled_priority_sorts_after_p2(self):
        rows = [issue(1, "lane-ready"), issue(2, "lane-ready", "p2")]

        queue = dispatch_queue.build_queue(rows)

        self.assertEqual([entry["number"] for entry in queue], [2, 1])
        self.assertIsNone(queue[1]["priority_label"])

    def test_keeps_title_body_and_labels_of_a_row(self):
        rows = [issue(7, "lane-ready", "p1", title="fix it", body="body text")]

        queue = dispatch_queue.build_queue(rows)

        self.assertEqual(queue[0]["title"], "fix it")
        self.assertEqual(queue[0]["body"], "body text")
        self.assertEqual(queue[0]["labels"], ["lane-ready", "p1"])

    def test_drops_rows_without_the_ready_label(self):
        rows = [issue(1, "p1"), issue(2, "lane-ready")]

        queue = dispatch_queue.build_queue(rows)

        self.assertEqual([entry["number"] for entry in queue], [2])

    def test_drops_rows_that_are_claimed_done_or_failed(self):
        rows = [
            issue(1, "lane-ready", "in-progress"),
            issue(2, "lane-ready", "lane-pr"),
            issue(3, "lane-ready", "lane-failed"),
            issue(4, "lane-ready"),
        ]

        queue = dispatch_queue.build_queue(rows)

        self.assertEqual([entry["number"] for entry in queue], [4])

    def test_drops_closed_rows(self):
        rows = [issue(1, "lane-ready", state="CLOSED"), issue(2, "lane-ready", state="OPEN")]

        queue = dispatch_queue.build_queue(rows)

        self.assertEqual([entry["number"] for entry in queue], [2])

    def test_label_names_are_configurable(self):
        cfg = {"labels": {"ready": "todo", "in_progress": "doing", "pr": "done", "failed": "broken"}}
        rows = [
            issue(1, "todo"),
            issue(2, "lane-ready"),
            issue(3, "todo", "doing"),
            issue(4, "todo", "done"),
            issue(5, "todo", "broken"),
        ]

        queue = dispatch_queue.build_queue(rows, cfg)

        self.assertEqual([entry["number"] for entry in queue], [1])

    def test_flat_label_names_are_configurable_too(self):
        rows = [issue(1, "todo"), issue(2, "lane-ready")]

        queue = dispatch_queue.build_queue(rows, {"ready": "todo"})

        self.assertEqual([entry["number"] for entry in queue], [1])

    def test_parses_the_backend_pin_and_the_strict_label(self):
        rows = [
            issue(1, "lane-ready", "backend:pi"),
            issue(2, "lane-ready", "backend:codex", "strict-backend"),
            issue(3, "lane-ready"),
        ]

        queue = dispatch_queue.build_queue(rows)

        self.assertEqual(queue[0]["backend"], "pi")
        self.assertFalse(queue[0]["strict"])
        self.assertEqual(queue[1]["backend"], "codex")
        self.assertTrue(queue[1]["strict"])
        self.assertIsNone(queue[2]["backend"])
        self.assertFalse(queue[2]["strict"])

    def test_does_not_mutate_the_input_rows(self):
        rows = [issue(1, "lane-ready", "p1", title="fix it")]

        dispatch_queue.build_queue(rows)

        self.assertEqual(rows, [issue(1, "lane-ready", "p1", title="fix it")])


class ShadowPlanTests(unittest.TestCase):
    """The shadow plan routes the queue to the idle lanes without touching the labels."""

    def test_no_idle_lane_returns_an_empty_plan(self):
        queue = dispatch_queue.build_queue([issue(1, "lane-ready", "p0")])

        plan = dispatch_queue.shadow_plan(queue, [])

        self.assertEqual(plan, [])

    def test_a_pinned_issue_goes_to_the_lane_of_its_backend(self):
        queue = dispatch_queue.build_queue([issue(1, "lane-ready", "backend:pi", "p0")])
        lanes = [{"lane": "lane-codex", "backend": "codex"}, {"lane": "lane-pi", "backend": "pi"}]

        plan = dispatch_queue.shadow_plan(queue, lanes)

        self.assertEqual([row["issue"] for row in plan], [1])
        self.assertEqual(plan[0]["lane"], "lane-pi")
        self.assertIn("pinned", plan[0]["reason"])

    def test_an_unpinned_issue_is_preferred_over_a_cross_pinned_one(self):
        queue = dispatch_queue.build_queue([
            issue(1, "lane-ready", "backend:pi", "p0"),
            issue(2, "lane-ready", "p1"),
        ])

        plan = dispatch_queue.shadow_plan(queue, [{"lane": "lane-agy", "backend": "agy"}])

        self.assertEqual([row["issue"] for row in plan], [2])
        self.assertIn("unpinned", plan[0]["reason"])

    def test_a_pinned_issue_is_planned_before_an_unpinned_one(self):
        queue = dispatch_queue.build_queue([
            issue(1, "lane-ready", "p0"),
            issue(2, "lane-ready", "backend:pi", "p1"),
        ])
        lanes = [{"lane": "lane-codex", "backend": "codex"}, {"lane": "lane-pi", "backend": "pi"}]

        plan = dispatch_queue.shadow_plan(queue, lanes)

        self.assertEqual({row["lane"]: row["issue"] for row in plan}, {"lane-pi": 2, "lane-codex": 1})
        self.assertEqual([row["lane"] for row in plan], ["lane-codex", "lane-pi"])

    def test_a_lane_without_a_known_backend_takes_an_unpinned_issue(self):
        queue = dispatch_queue.build_queue([issue(1, "lane-ready", "p0")])

        plan = dispatch_queue.shadow_plan(queue, ["lane-a"])

        self.assertEqual([(row["issue"], row["lane"]) for row in plan], [(1, "lane-a")])

    def test_a_strict_pin_is_never_given_to_another_backend(self):
        queue = dispatch_queue.build_queue([
            issue(1, "lane-ready", "backend:pi", "strict-backend", "p0"),
        ])

        plan = dispatch_queue.shadow_plan(queue, [{"lane": "lane-codex", "backend": "codex"}])

        self.assertEqual(plan, [])

    def test_a_strict_pin_stays_with_its_own_backend(self):
        queue = dispatch_queue.build_queue([
            issue(1, "lane-ready", "backend:pi", "strict-backend", "p0"),
        ])
        lanes = [{"lane": "lane-codex", "backend": "codex"}, {"lane": "lane-pi", "backend": "pi"}]

        plan = dispatch_queue.shadow_plan(queue, lanes)

        self.assertEqual([(row["issue"], row["lane"]) for row in plan], [(1, "lane-pi")])

    def test_a_non_strict_pin_falls_back_to_another_backend(self):
        queue = dispatch_queue.build_queue([
            issue(1, "lane-ready", "backend:pi", "p0"),
        ])

        plan = dispatch_queue.shadow_plan(queue, [{"lane": "lane-codex", "backend": "codex"}])

        self.assertEqual([row["issue"] for row in plan], [1])
        self.assertIn("cross-pin", plan[0]["reason"])

    def test_every_lane_takes_one_issue_and_every_issue_is_taken_once(self):
        queue = dispatch_queue.build_queue([
            issue(1, "lane-ready", "p0"),
            issue(2, "lane-ready", "p1"),
        ])
        lanes = [{"lane": "lane-a", "backend": "pi"}, {"lane": "lane-b", "backend": "pi"}]

        plan = dispatch_queue.shadow_plan(queue, lanes)

        self.assertEqual([row["issue"] for row in plan], [1, 2])
        self.assertEqual([row["lane"] for row in plan], ["lane-a", "lane-b"])

    def test_one_issue_is_not_given_to_two_lanes(self):
        queue = dispatch_queue.build_queue([issue(1, "lane-ready", "p0")])
        lanes = [{"lane": "lane-a", "backend": "pi"}, {"lane": "lane-b", "backend": "pi"}]

        plan = dispatch_queue.shadow_plan(queue, lanes)

        self.assertEqual([row["issue"] for row in plan], [1])

    def test_the_plan_does_not_change_the_labels_of_the_queue(self):
        rows = [issue(1, "lane-ready", "backend:pi", "p0")]
        queue = dispatch_queue.build_queue(rows)

        dispatch_queue.shadow_plan(queue, [{"lane": "lane-pi", "backend": "pi"}])

        self.assertEqual(queue[0]["labels"], ["lane-ready", "backend:pi", "p0"])
        self.assertEqual(rows[0]["labels"], ["lane-ready", "backend:pi", "p0"])


if __name__ == "__main__":
    unittest.main()
