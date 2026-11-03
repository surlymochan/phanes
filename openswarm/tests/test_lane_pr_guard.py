"""The delivery guard of the lane dispatcher (Refs #1772).

A card with an open pull request is delivered: it must not be requeued and it must
not be dispatched a second time. The delivery labels are verified with a fresh
`gh` read, never with a local cache.

The tests drive the shipped code: the open PR lookup, the label transition, the
failed run requeue and the dispatch tick.
"""

import importlib.util
import json
import os
from pathlib import Path
import tempfile
import time
import unittest


AUTODISPATCH = Path(__file__).parents[2] / "tools" / "lane-autodispatch" / "autodispatch.py"
_spec = importlib.util.spec_from_file_location("autodispatch_guard_under_test", AUTODISPATCH)
autodispatch = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(autodispatch)

REPO = autodispatch.REPO


def graphql_page(prs, cursor=None, has_next=False, end_cursor=None):
    """One page of the open PR query."""
    connection = {"nodes": prs, "pageInfo": {"hasNextPage": has_next, "endCursor": end_cursor}}
    return json.dumps({"data": {"repository": {"pullRequests": connection}}})


def pr(number, body="", head="", closing=()):
    """One open pull request of the fake GraphQL answer."""
    return {"number": number, "title": f"#{number}", "body": body, "headRefName": head,
            "closingIssuesReferences": {"nodes": [{"number": issue} for issue in closing]}}


class FakeGh:
    """A `gh` stand-in for the delivery guard.

    It answers the open PR query, the issue reads and the label edits, and it keeps
    the labels of the issues so that the read back really shows the edit."""

    def __init__(self, prs=None, pages=None, labels=None, view_error=False, edit_error=False, pr_error=None):
        self.prs = prs or []
        self.pages = pages  # a callable(cursor) -> the next page
        self.labels = {issue: set(names) for issue, names in (labels or {}).items()}
        self.view_error = view_error
        self.edit_error = edit_error
        self.pr_error = pr_error
        self.calls = []
        self.reads = []
        self.payloads = []

    # -- the runner contract of the guard: runner(args, payload=None) -> (rc, out, err)
    def __call__(self, args, payload=None):
        self.calls.append(list(args))
        if args[:3] == ["api", "graphql", "--input"]:
            self.payloads.append(payload)
            return self.graphql(payload)
        if args[:2] == ["issue", "view"]:
            issue = int(args[2])
            self.reads.append(issue)
            if self.view_error:
                return 1, "", "gh: could not read the issue"
            return 0, json.dumps({"number": issue, "state": "OPEN",
                                   "labels": [{"name": name} for name in sorted(self.labels.get(issue, set()))]}), ""
        if args[:2] == ["issue", "edit"]:
            if self.edit_error:
                return 1, "", "gh: could not edit the issue"
            issue = int(args[2])
            labels = self.labels.setdefault(issue, set())
            for index, arg in enumerate(args):
                if arg == "--add-label":
                    labels.add(args[index + 1])
                if arg == "--remove-label":
                    labels.discard(args[index + 1])
            return 0, "", ""
        return 0, "", ""

    def graphql(self, payload):
        if self.pr_error:
            return 1, "", self.pr_error
        if self.pages:
            cursor = json.loads(payload)["variables"]["after"]
            return 0, self.pages(cursor), ""
        return 0, graphql_page(self.prs), ""

    def edits(self):
        return [call for call in self.calls if call[:2] == ["issue", "edit"]]

    def added_labels(self):
        labels = []
        for edit in self.edits():
            for index, arg in enumerate(edit):
                if arg == "--add-label":
                    labels.append(edit[index + 1])
        return labels


class OpenPullRequestLookupTests(unittest.TestCase):
    """The open PR lookup must not miss a PR that is not on the first page."""

    def test_every_page_is_walked(self):
        pages = {
            None: graphql_page([pr(1, "Closes #11")], has_next=True, end_cursor="cursor-1"),
            "cursor-1": graphql_page([pr(2, "Closes #12")]),
        }
        fake = FakeGh(pages=lambda cursor: pages[cursor])

        prs = autodispatch.open_pull_requests(fake, REPO)

        self.assertEqual([item["number"] for item in prs], [1, 2])
        payloads = [json.loads(payload) for payload in fake.payloads]
        self.assertEqual([payload["variables"]["after"] for payload in payloads], [None, "cursor-1"])

    def test_the_query_asks_for_open_pull_requests_only(self):
        fake = FakeGh()

        autodispatch.open_pull_requests(fake, REPO)

        payload = json.loads(fake.payloads[0])
        self.assertIn("states:OPEN", payload["query"],
                      "a merged PR must not count as an open PR")
        self.assertNotIn("--limit", payload["query"])

    def test_a_failed_read_raises(self):
        class Broken:
            def __call__(self, args, payload=None):
                return 1, "", "gh: connection reset"

        with self.assertRaises(RuntimeError):
            autodispatch.open_pull_requests(Broken(), REPO)

    def test_an_unexpected_shape_raises(self):
        class Odd:
            def __call__(self, args, payload=None):
                return 0, json.dumps({"data": {"repository": {"pullRequests": {"nodes": {}}}}}), ""

        with self.assertRaises(RuntimeError):
            autodispatch.open_pull_requests(Odd(), REPO)


def self_payloads(fake):
    """The GraphQL payloads the fake received."""
    return fake.payloads

class LinkedIssueTests(unittest.TestCase):
    """Which issue a pull request delivers."""

    def test_the_closing_reference_of_github_counts(self):
        self.assertEqual(autodispatch.linked_issue_numbers(pr(1, closing=[1772])), {1772})

    def test_the_closing_keyword_of_the_body_counts(self):
        self.assertEqual(autodispatch.linked_issue_numbers(pr(1, body="Closes #1772\n\n验收：ok")), {1772})

    def test_the_fleet_branch_counts(self):
        self.assertEqual(autodispatch.linked_issue_numbers(pr(1, head="fleet/CX10-issue1772-20261103-023820")), {1772})

    def test_a_plain_mention_is_not_a_delivery(self):
        self.assertEqual(autodispatch.linked_issue_numbers(pr(1, body="refs #1772, see also")), set())


class DeliveryStateTests(unittest.TestCase):
    """The delivery terminal state is the delivery label `lane-pr` alone."""

    def test_other_labels_are_removed(self):
        fake = FakeGh(labels={1772: {"lane-pr", "p1", "backend:codex"}})
        self.assertFalse(autodispatch.is_delivered(fake.labels[1772]))
        self.assertTrue(autodispatch.ensure_lane_pr(1772, runner=fake, repo=REPO))
        self.assertEqual(fake.labels[1772], {"lane-pr"})

    def test_a_pending_label_is_not_the_terminal_state(self):
        self.assertFalse(autodispatch.is_delivered({"lane-pr", "lane-ready"}))
        self.assertFalse(autodispatch.is_delivered({"lane-pr", "in-progress"}))
        self.assertFalse(autodispatch.is_delivered({"lane-ready"}))

    def test_the_transition_is_verified_with_a_fresh_read(self):
        fake = FakeGh(labels={1772: {"lane-ready", "in-progress"}})

        ok = autodispatch.ensure_lane_pr(1772, runner=fake, repo=REPO)

        self.assertTrue(ok)
        self.assertEqual(fake.labels[1772], {"lane-pr"})
        self.assertGreaterEqual(len(fake.reads), 2, "the terminal state must be read back")

    def test_a_failing_transition_is_retried_and_reported(self):
        notes = []
        fake = FakeGh(labels={1772: {"lane-ready"}}, edit_error=True)

        ok = autodispatch.ensure_lane_pr(1772, runner=fake, repo=REPO, note=notes.append)

        self.assertFalse(ok)
        self.assertEqual(len(fake.edits()), autodispatch.DELIVERY_ATTEMPTS)
        self.assertEqual(len(notes), autodispatch.DELIVERY_ATTEMPTS)

    def test_an_unreadable_issue_is_not_a_terminal_state(self):
        fake = FakeGh(view_error=True)

        ok = autodispatch.ensure_lane_pr(1772, runner=fake, repo=REPO)

        self.assertFalse(ok, "a label read that failed is never a verified terminal state")
        self.assertEqual(len(fake.edits()), autodispatch.DELIVERY_ATTEMPTS)


class GuardedAutodispatchTests(unittest.TestCase):
    """The dispatcher itself: the failed run requeue and the dispatch tick."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.logs = []
        self.gh = FakeGh()
        for name, value in (("D", self.tmp.name), ("gh", self.gh), ("gh_api", self.gh), ("log", self.log)):
            self.addCleanup(setattr, autodispatch, name, getattr(autodispatch, name))
            setattr(autodispatch, name, value)

    def log(self, message, brain=True):
        self.logs.append((message, brain))

    def brain_logs(self):
        return [message for message, brain in self.logs if brain]

    def use_gh(self, fake):
        """The fake `gh` of the test (both the raw and the `-R` flavour)."""
        self.gh = fake
        autodispatch.gh = fake
        autodispatch.gh_api = fake

    def test_a_failed_run_of_a_delivered_card_is_not_requeued(self):
        """The card has an open PR: it must not go back into lane-ready."""
        self.use_gh(FakeGh(prs=[pr(1768, "Closes #1734", closing=[1734])],
                           labels={1734: {"lane-ready", "in-progress"}}))
        state = {"issue": 1734, "run": "run-a", "at": self.old()}

        requeued = autodispatch.reconcile("M8", {"host": "h1"}, state,
                                          {"run": "running", "lane": "idle", "current_run": None,
                                           "runner_alive": False, "run_state": "missing"}, {})

        self.assertTrue(requeued)
        self.assertNotIn("lane-ready", self.gh.added_labels(),
                         "a card with an open PR must not be put back into lane-ready")
        self.assertIn("lane-pr", self.gh.added_labels(),
                      "the delivery terminal state must be restored")
        self.assertTrue(any("open PR #1768" in message for message, _ in self.logs),
                        f"the skip reason must be visible: {self.logs}")

    def test_a_failed_run_without_an_open_pr_is_requeued(self):
        self.use_gh(FakeGh(labels={1734: {"lane-ready", "in-progress"}}))
        state = {"issue": 1734, "run": "run-a", "at": self.old()}

        requeued = autodispatch.reconcile("M8", {"host": "h1"}, state,
                                          {"run": "running", "lane": "idle", "current_run": None,
                                           "runner_alive": False, "run_state": "missing"}, {})

        self.assertTrue(requeued)
        self.assertIn("lane-ready", self.gh.added_labels())

    def test_a_failed_run_is_not_requeued_when_the_open_pr_lookup_fails(self):
        self.use_gh(FakeGh(pr_error="gh: connection reset", labels={1734: {"lane-ready"}}))
        state = {"issue": 1734, "run": "run-a", "at": self.old()}

        requeued = autodispatch.reconcile("M8", {"host": "h1"}, state,
                                          {"run": "running", "lane": "idle", "current_run": None,
                                           "runner_alive": False, "run_state": "missing"}, {})

        self.assertTrue(requeued)
        self.assertNotIn("lane-ready", self.gh.added_labels(),
                         "an unknown open PR state must not requeue the card")
        self.assertTrue(self.brain_logs(), "the blocked requeue must be reported")

    def old(self):
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 3600))


class DispatchTickGuardTests(unittest.TestCase):
    """The dispatch cycle skips a card that an open pull request already delivers."""

    LANES = {"L1": {"host": "h1", "backend": "pi"}}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.logs = []
        self.dispatched = []
        self.gh = FakeGh(labels={7: {"lane-ready"}})
        for name in ("D", "gh", "gh_api", "remote", "queue", "dispatch", "replenish", "save_state", "log"):
            self.addCleanup(setattr, autodispatch, name, getattr(autodispatch, name))
        autodispatch.D = self.tmp.name
        autodispatch.gh = self.gh
        autodispatch.gh_api = self.gh
        autodispatch.remote = lambda lane, cfg, run="": {"lane": "idle"}
        autodispatch.queue = lambda: [self.card()]
        autodispatch.dispatch = self.dispatch
        autodispatch.replenish = lambda k, state: []
        autodispatch.save_state = lambda state: None
        autodispatch.log = self.log
        autodispatch.sh = lambda cmd, timeout=120, inp=None: (0, "0123456789abcdef0123456789abcdef01234567\n", "")
        autodispatch.OPEN_PR_CACHE.update({"at": 0.0, "issues": {}, "loaded": False})
        home = Path(self.tmp.name) / "openswarm-home"
        home.mkdir()
        self.addCleanup(os.environ.pop, "OPENSWARM_HOME", None)
        os.environ["OPENSWARM_HOME"] = str(home)

    def log(self, message, brain=True):
        self.logs.append((message, brain))

    def card(self):
        return {"n": 7, "title": "card 7", "body": "", "prio": 3, "backend": None,
                "strict": False, "verify": False}

    def use_gh(self, fake):
        """The fake `gh` of the test (both the raw and the `-R` flavour)."""
        self.gh = fake
        autodispatch.gh = fake
        autodispatch.gh_api = fake

    def dispatch(self, lane, cfg, item, base):
        self.dispatched.append(item["n"])
        return "run-a", None

    def tick(self):
        return autodispatch.tick(self.LANES, {}, 0)

    def test_a_card_with_an_open_pr_is_not_dispatched(self):
        self.use_gh(FakeGh(prs=[pr(1768, "Closes #7", closing=[7])], labels={7: {"lane-ready"}}))

        self.tick()

        self.assertEqual(self.dispatched, [], "a card with an open PR must not be dispatched")
        self.assertTrue(any("open PR #1768" in message and "not dispatched" in message
                            for message, _ in self.logs),
                        f"the skip reason must be visible: {self.logs}")

    def test_a_delivered_card_is_not_counted_as_ready(self):
        self.use_gh(FakeGh(prs=[pr(1768, "Closes #7", closing=[7])], labels={7: {"lane-ready"}}))

        self.tick()

        status = json.loads((Path(self.tmp.name) / "lanes-status.json").read_text(encoding="utf-8"))
        self.assertEqual(status["_ready"], 0, "a delivered card must not inflate the pool depth")

    def test_a_card_without_an_open_pr_is_dispatched(self):
        self.tick()

        self.assertEqual(self.dispatched, [7])

    def test_the_dispatch_is_skipped_when_the_open_pr_lookup_fails(self):
        self.use_gh(FakeGh(pr_error="gh: connection reset", labels={7: {"lane-ready"}}))

        self.tick()

        self.assertEqual(self.dispatched, [], "an unknown open PR state must not be dispatched on")


class DeliveryTransitionTests(unittest.TestCase):
    """The delivery transition after the pull request was opened."""

    ISSUE = 1704

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.logs = []
        self.pr_creates = []
        self.gh = FakeGh(labels={self.ISSUE: {"lane-ready", "in-progress"}})
        for name, value in (("D", self.tmp.name), ("gh", self.gh), ("gh_api", self.gh), ("log", self.log)):
            self.addCleanup(setattr, autodispatch, name, getattr(autodispatch, name))
            setattr(autodispatch, name, value)

    def log(self, message, brain=True):
        self.logs.append((message, brain))

    def finish(self, pr_rc=0, open_prs=None):
        def gh(args, timeout=60):
            if args[:2] == ["issue", "view"]:
                return 0, json.dumps({"labels": [{"name": name} for name in sorted(self.gh.labels.get(self.ISSUE, set()))]}), ""
            if args[:2] == ["issue", "edit"]:
                return self.gh(args)
            if args[:2] == ["pr", "create"]:
                self.pr_creates.append(list(args))
                return (pr_rc, "" if pr_rc else "https://github.com/surlymochan/phanes/pull/1", "gh: failed")
            if args[:2] == ["api", "graphql"]:
                return self.gh(args)
            return 0, "", ""

        def sh(cmd, timeout=120, inp=None):
            argv = cmd if isinstance(cmd, list) else [cmd]
            if "--format=%s" in argv: return 0, "fix #1704 parser", ""
            if "--format=%B" in argv: return 0, "Closes #1704\n\n验收：python3 -m unittest (passed)", ""
            return 0, "", ""

        self.gh.prs = open_prs or []
        autodispatch.gh, autodispatch.gh_api, autodispatch.sh = gh, gh, sh
        st = {"issue": self.ISSUE, "run": "run-a", "base": "0123456789abcdef", "host": "h1"}
        info = {"status": "done", "branch": "work", "commit": "c0ffee", "repo": "~/work/phanes-fleet",
                "backend": "codex", "model": "gpt", "summary": "Rewrote the parser.",
                "tests": "python3 -m unittest (passed)", "verify_rc": 0}
        return autodispatch.finish("codex", {"host": "h1", "backend": "codex"}, st, info, {})

    def test_the_labels_are_lane_pr_after_the_pull_request(self):
        result = self.finish()

        self.assertEqual(result["status"], "pr-created")
        self.assertEqual(self.gh.labels[self.ISSUE], {"lane-pr"},
                         "the gh read back must show the terminal state")

    def test_a_failing_transition_is_reported(self):
        self.gh.edit_error = True

        result = self.finish()

        self.assertEqual(result["status"], "pr-created")
        self.assertTrue([message for message, brain in self.logs if brain],
                        "a failing delivery transition must be reported")

    def test_a_pull_request_that_could_not_be_opened_is_not_lane_pr(self):
        result = self.finish(pr_rc=1)

        self.assertEqual(result["status"], "pr-create-failed")
        self.assertNotIn("lane-pr", self.gh.labels[self.ISSUE],
                         "a card without a pull request must not be labelled lane-pr")

    def test_a_card_with_an_open_pr_gets_no_second_pull_request(self):
        result = self.finish(open_prs=[pr(1768, "Closes #1704", closing=[1704])])

        self.assertEqual(result["status"], "duplicate")
        self.assertEqual(self.pr_creates, [], "a delivered card must not get a second pull request")
        self.assertEqual(self.gh.labels[self.ISSUE], {"lane-pr"},
                         "the delivery terminal state must be restored")


class PrValidationRequeueTests(unittest.TestCase):
    """Handing a card back after a failed PR validation is a requeue too."""

    ISSUE = 1704

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.logs = []
        self.gh = FakeGh(labels={self.ISSUE: {"lane-ready", "in-progress"}})
        for name in ("D", "gh", "gh_api", "sh", "log"):
            self.addCleanup(setattr, autodispatch, name, getattr(autodispatch, name))
        autodispatch.D = self.tmp.name
        autodispatch.gh = self.gh
        autodispatch.gh_api = self.gh
        autodispatch.sh = lambda cmd, timeout=120, inp=None: (0, "", "")
        autodispatch.log = self.log

    def log(self, message, brain=True):
        self.logs.append((message, brain))

    def block(self):
        st = {"issue": self.ISSUE, "run": "run-a", "base": "0123456789abcdef", "host": "h1"}
        return autodispatch.block_pr_validation("codex", {"host": "h1"}, st, {}, {}, "title", "body", ["缺 Closes"])

    def test_a_delivered_card_is_not_put_back_into_lane_ready(self):
        self.gh.prs = [pr(1768, "Closes #1704", closing=[1704])]

        result = self.block()

        self.assertEqual(result["status"], "pr-validation-failed")
        self.assertNotIn("lane-ready", self.gh.added_labels(),
                         "a card with an open PR must not go back into lane-ready")
        self.assertIn("lane-pr", self.gh.added_labels(),
                      "the delivery terminal state must be restored")

    def test_a_card_without_an_open_pr_is_handed_back(self):
        result = self.block()

        self.assertEqual(result["status"], "pr-validation-failed")
        self.assertIn("lane-ready", self.gh.added_labels())


if __name__ == "__main__":
    unittest.main()
