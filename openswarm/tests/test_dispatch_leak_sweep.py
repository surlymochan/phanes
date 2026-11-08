"""In-progress leak sweep decisions: leases, box-resume grace and clock jumps (Refs #1493).

The dispatch loop labels an issue `in-progress` before the run is really alive.
A sweep that judges the label by its age alone strips the label of a card that
is still running (the 05:12 box pause accident behind #1403), so the decision
is taken from the lease of the issue, from the lanes that still hold it and
from the grace window that follows the last box resume.

`openswarm/lib/dispatch/leak_sweep.py` is the module this suite covers.  It does
not exist on this baseline, so the suite is red until the fix lane adds it
(Refs #1493).

Acceptance: `cd openswarm && python3 -m unittest discover -s tests -p 'test_dispatch_leak_sweep.py'`

Mapping (the review blockers of #1493 → the test that reproduces them):

* B1: a non finite timestamp (`updatedAt="nan"`, `now=NaN`) must never strip
  the label → `test_non_finite_timestamps_keep_the_label`
* B2: a `box-resume` marker in the future (the clock jumped back) must never
  strip the label → `test_a_resume_marker_in_the_future_keeps_the_label`
* B3: the naive timestamps of the marker are UTC, so `%F %T` and `%FT%T` read
  the same epoch → `test_load_resume_at_reads_the_first_line_as_utc`
"""
import importlib.util
import pathlib
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
LEASE_MODULE = HERE.parent / "lib" / "dispatch" / "lease.py"
SWEEP_MODULE = HERE.parent / "lib" / "dispatch" / "leak_sweep.py"


def _load(name, path):
    """Load a module by path: `openswarm/lib/dispatch/` is a namespace package."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


lease = _load("dispatch_lease", LEASE_MODULE)
sweep = _load("dispatch_leak_sweep", SWEEP_MODULE)


def row(number, updated):
    """One `in-progress` issue row.

    The issue number is written under both keys the callers use (`issue` and
    `number`), so the fixture does not depend on the spelling the sweep reads.
    """
    return {"issue": number, "number": number, "updatedAt": updated}


class LeakSweepTests(unittest.TestCase):
    """Every `in-progress` label is either kept with a reason or stripped."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="openswarm-leak-sweep-")
        self.addCleanup(self.tmp.cleanup)
        self.leases = lease.LeaseStore(self.tmp.name)
        self.label = row(1403, 1000)

    def decide(self, labelled=None, held=(), resume_at=None, now=3000, **kwargs):
        """Ask the sweep about the labelled issues (one row by default)."""
        rows = [self.label] if labelled is None else labelled
        return sweep.decide(rows, list(held), self.leases, resume_at, now, **kwargs)

    def one(self, *args, **kwargs):
        """The single decision of the default row."""
        decisions = self.decide(*args, **kwargs)
        self.assertEqual(len(decisions), 1)
        return decisions[0]

    def test_every_labelled_issue_gets_one_decision(self):
        decisions = self.decide(labelled=[row(1403, 1000), row(1404, 1000)])

        self.assertEqual([decision["issue"] for decision in decisions], [1403, 1404])
        for decision in decisions:
            for key in ("issue", "action", "reason"):
                self.assertIn(key, decision)
            self.assertIn(decision["action"], ("keep", "strip"))
            self.assertTrue(str(decision["reason"]).strip())

    def test_a_live_lease_keeps_the_label(self):
        # the lease runs until 3100, the label is idle since 1000
        self.leases.grant(1403, "run-1", "M1", 600, 2500)

        result = self.one()

        self.assertEqual(result["issue"], 1403)
        self.assertEqual((result["action"], result["reason"]), ("keep", "lease_alive"))

    def test_an_expired_lease_is_stripped_only_when_no_lane_holds_the_issue(self):
        # the lease expired at 1600, the label is idle since 1000
        self.leases.grant(1403, "run-1", "M1", 600, 1000)

        stripped = self.one()
        self.assertEqual(stripped["action"], "strip")

        held = self.one(held=[1403])
        self.assertEqual((held["action"], held["reason"]), ("keep", "held"))

    def test_a_label_inside_the_grace_window_is_kept(self):
        result = self.one(labelled=[row(1403, 2900)])

        self.assertEqual(result["action"], "keep")

    def test_the_resume_grace_keeps_the_label_and_later_allows_the_strip(self):
        in_grace = self.one(resume_at=2900)
        self.assertEqual((in_grace["action"], in_grace["reason"]), ("keep", "resume_grace"))

        after = self.one(resume_at=2000)
        self.assertEqual(after["action"], "strip")

    def test_a_label_debt_keeps_the_label(self):
        result = self.one(debt=[1403])

        self.assertEqual((result["action"], result["reason"]), ("keep", "label_debt"))

    def test_a_clock_rollback_keeps_the_label(self):
        # the label was updated after `now`: the clock jumped back
        result = self.one(labelled=[row(1403, 3100)])

        self.assertEqual(result["action"], "keep")

    def test_a_resume_marker_in_the_future_keeps_the_label(self):
        # the box resumed at 3500 while `now` is 3000: the clock jumped back
        result = self.one(resume_at=3500)

        self.assertEqual(result["action"], "keep")

    def test_non_finite_timestamps_keep_the_label(self):
        broken_label = self.one(labelled=[row(1403, "nan")])
        self.assertEqual(broken_label["action"], "keep")

        broken_clock = self.one(now=float("nan"))
        self.assertEqual(broken_clock["action"], "keep")

    def test_load_resume_at_reads_the_first_line_as_utc(self):
        path = pathlib.Path(self.tmp.name) / "box-resume"

        self.assertIsNone(sweep.load_resume_at(path))

        expected = 1793620800.0  # 2026-11-02 12:00:00 UTC
        path.write_text("2026-11-02 12:00:00\nignored\n", encoding="utf-8")
        self.assertEqual(sweep.load_resume_at(path), expected)
        path.write_text("2026-11-02T12:00:00\nignored\n", encoding="utf-8")
        self.assertEqual(sweep.load_resume_at(path), expected)
        path.write_text("2026-11-02T12:00:00Z\nignored\n", encoding="utf-8")
        self.assertEqual(sweep.load_resume_at(path), expected)
        path.write_text("1793620800\nignored\n", encoding="utf-8")
        self.assertEqual(sweep.load_resume_at(path), expected)


if __name__ == "__main__":
    unittest.main()
