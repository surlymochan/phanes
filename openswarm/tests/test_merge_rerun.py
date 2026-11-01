"""`openswarm/lib/merge/rerun.py`: a failed CI run is rerun once, and only once (Refs #1445).

The module is loaded by file path: ``openswarm/lib/merge`` is a namespace package
without an ``__init__.py``, so it cannot be imported as a package from the
repository root (same trick as ``tests/test_dispatch_lease.py``).
"""
import importlib.util
import json
import os
import pathlib
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
MODULE = HERE.parent / "lib" / "merge" / "rerun.py"

spec = importlib.util.spec_from_file_location("merge_rerun", MODULE)
rerun = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rerun)


def run_row(run_id, name="CI", conclusion="failure", head_sha="abc123"):
    """One `gh run list --json databaseId,name,conclusion,headSha` row."""
    return {
        "databaseId": run_id,
        "name": name,
        "conclusion": conclusion,
        "headSha": head_sha,
    }


class FakeResult:
    """The little of `subprocess.CompletedProcess` the module looks at."""

    def __init__(self, returncode=0, stderr=""):
        self.returncode = returncode
        self.stdout = ""
        self.stderr = stderr


class FakeRunner:
    """A `subprocess.run` stand-in that records the commands it was given."""

    def __init__(self, returncode=0, stderr=""):
        self.calls = []
        self.returncode = returncode
        self.stderr = stderr

    def __call__(self, command, **kwargs):
        self.calls.append(list(command))
        return FakeResult(self.returncode, self.stderr)


class RerunLedgerTests(unittest.TestCase):
    """The ledger remembers the runs that were already rerun once."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="openswarm-rerun-")
        self.addCleanup(self.tmp.cleanup)
        self.path = pathlib.Path(self.tmp.name) / "reruns.json"
        self.ledger = rerun.RerunLedger(self.path)

    def test_a_recorded_run_is_seen_and_survives_a_reload(self):
        record = self.ledger.record(1234567, 1445, 1000.0)

        self.assertEqual(record, {"pr": 1445, "at": 1000.0})
        self.assertTrue(self.ledger.seen(1234567))
        self.assertTrue(self.ledger.seen("1234567"))
        # a second ledger on the same file sees the same record
        self.assertTrue(rerun.RerunLedger(self.path).seen(1234567))
        self.assertFalse(self.ledger.seen(7654321))

    def test_the_ledger_file_holds_the_runs_and_is_written_atomically(self):
        self.ledger.record(1234567, 1445, 1000.0)

        # no half written temporary file is left behind
        self.assertEqual(sorted(os.listdir(self.tmp.name)), ["reruns.json"])
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(payload, {"runs": {"1234567": {"pr": 1445, "at": 1000.0}}})

    def test_a_missing_ledger_reads_as_an_empty_state(self):
        self.assertEqual(self.ledger.load(), {})
        self.assertFalse(self.ledger.seen(1234567))

    def test_a_damaged_ledger_falls_back_to_an_empty_state(self):
        self.path.write_text("{not json", encoding="utf-8")

        self.assertEqual(self.ledger.load(), {})
        self.assertFalse(self.ledger.seen(1234567))

    def test_a_record_after_a_damaged_file_starts_over(self):
        self.path.write_text("[]", encoding="utf-8")

        self.ledger.record(1234567, 1445, 1000.0)

        payload = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(payload, {"runs": {"1234567": {"pr": 1445, "at": 1000.0}}})

    def test_prune_drops_the_old_records_and_keeps_the_recent_ones(self):
        self.ledger.record(1, 1445, 1000.0)
        self.ledger.record(2, 1445, 2000.0)

        removed = self.ledger.prune(600, 2000.0)

        self.assertEqual(removed, ["1"])
        self.assertFalse(self.ledger.seen(1))
        self.assertTrue(self.ledger.seen(2))
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(sorted(payload["runs"]), ["2"])


class SelectRerunsTests(unittest.TestCase):
    """Only the failed runs that nobody rerun yet are picked."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="openswarm-rerun-")
        self.addCleanup(self.tmp.cleanup)
        self.ledger = rerun.RerunLedger(pathlib.Path(self.tmp.name) / "reruns.json")

    def test_the_failure_conclusions_are_picked(self):
        rows = [
            run_row(1, conclusion="failure"),
            run_row(2, conclusion="timed_out"),
            run_row(3, conclusion="startup_failure"),
        ]

        picked = rerun.select_reruns(rows, self.ledger)

        self.assertEqual([row["run_id"] for row in picked], [1, 2, 3])
        self.assertEqual(picked[0]["name"], "CI")
        self.assertEqual(picked[0]["head_sha"], "abc123")

    def test_cancelled_and_successful_runs_are_not_picked(self):
        rows = [
            run_row(1, conclusion="success"),
            run_row(2, conclusion="cancelled"),
            run_row(3, conclusion="skipped"),
        ]

        self.assertEqual(rerun.select_reruns(rows, self.ledger), [])

    def test_a_macos_job_is_ignored(self):
        rows = [run_row(1, name="build (macOS)"), run_row(2, name="build (ubuntu)")]

        picked = rerun.select_reruns(rows, self.ledger)

        self.assertEqual([row["run_id"] for row in picked], [2])

    def test_a_run_the_ledger_already_saw_is_not_picked(self):
        self.ledger.record(1, 1445, 1000.0)
        rows = [run_row(1), run_row(2)]

        picked = rerun.select_reruns(rows, self.ledger)

        self.assertEqual([row["run_id"] for row in picked], [2])

    def test_a_repeated_run_id_is_picked_only_once(self):
        rows = [run_row(1), run_row(1), run_row(2)]

        picked = rerun.select_reruns(rows, self.ledger)

        self.assertEqual([row["run_id"] for row in picked], [1, 2])


class RerunOnceTests(unittest.TestCase):
    """The failed runs are rerun once, and the decision is remembered."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="openswarm-rerun-")
        self.addCleanup(self.tmp.cleanup)
        self.path = pathlib.Path(self.tmp.name) / "reruns.json"
        self.ledger = rerun.RerunLedger(self.path)

    def test_a_failed_run_is_rerun_and_recorded(self):
        runner = FakeRunner()

        actions = rerun.rerun_once(1445, [run_row(1)], self.ledger, runner=runner, now=1000.0)

        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(runner.calls[0], ["gh", "run", "rerun", "1", "--failed"])
        self.assertEqual([row["action"] for row in actions], ["rerun"])
        self.assertTrue(self.ledger.seen(1))
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(payload["runs"]["1"], {"pr": 1445, "at": 1000.0})

    def test_the_same_run_is_not_rerun_twice(self):
        runner = FakeRunner()
        rows = [run_row(1)]

        rerun.rerun_once(1445, rows, self.ledger, runner=runner, now=1000.0)
        actions = rerun.rerun_once(1445, rows, self.ledger, runner=runner, now=2000.0)

        self.assertEqual(len(runner.calls), 1)
        self.assertEqual([row["action"] for row in actions], ["skip"])
        self.assertIn("already rerun", actions[0]["reason"])

    def test_a_macos_job_is_not_rerun(self):
        runner = FakeRunner()

        actions = rerun.rerun_once(1445, [run_row(1, name="build (macOS)")], self.ledger,
                                   runner=runner, now=1000.0)

        self.assertEqual(runner.calls, [])
        self.assertEqual([row["action"] for row in actions], ["skip"])
        self.assertIn("ignore pattern", actions[0]["reason"])
        self.assertFalse(self.ledger.seen(1))

    def test_cancelled_and_successful_runs_are_not_rerun(self):
        runner = FakeRunner()
        rows = [run_row(1, conclusion="success"), run_row(2, conclusion="cancelled")]

        actions = rerun.rerun_once(1445, rows, self.ledger, runner=runner, now=1000.0)

        self.assertEqual(runner.calls, [])
        self.assertEqual([row["action"] for row in actions], ["skip", "skip"])
        self.assertIn("not a failure", actions[0]["reason"])

    def test_a_failed_rerun_is_not_recorded_and_is_tried_again(self):
        failing = FakeRunner(returncode=1, stderr="gh: some error")
        rows = [run_row(1)]

        actions = rerun.rerun_once(1445, rows, self.ledger, runner=failing, now=1000.0)

        self.assertEqual([row["action"] for row in actions], ["skip"])
        self.assertIn("rerun failed", actions[0]["reason"])
        self.assertFalse(self.ledger.seen(1))
        self.assertFalse(self.path.exists())

        # the next round tries the very same run again
        working = FakeRunner()
        actions = rerun.rerun_once(1445, rows, self.ledger, runner=working, now=2000.0)

        self.assertEqual(len(working.calls), 1)
        self.assertEqual([row["action"] for row in actions], ["rerun"])
        self.assertTrue(self.ledger.seen(1))

    def test_a_dry_run_executes_nothing_and_writes_nothing(self):
        runner = FakeRunner()

        actions = rerun.rerun_once(1445, [run_row(1)], self.ledger, runner=runner,
                                   now=1000.0, dry_run=True)

        self.assertEqual(runner.calls, [])
        self.assertEqual([row["action"] for row in actions], ["rerun"])
        self.assertIn("planned", actions[0]["reason"])
        self.assertFalse(self.path.exists())
        self.assertFalse(self.ledger.seen(1))

    def test_a_damaged_ledger_does_not_stop_the_rerun(self):
        self.path.write_text("{not json", encoding="utf-8")
        runner = FakeRunner()

        actions = rerun.rerun_once(1445, [run_row(1)], self.ledger, runner=runner, now=1000.0)

        self.assertEqual(len(runner.calls), 1)
        self.assertEqual([row["action"] for row in actions], ["rerun"])
        self.assertTrue(self.ledger.seen(1))


if __name__ == "__main__":
    unittest.main()
