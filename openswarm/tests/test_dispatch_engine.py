"""openswarm dispatch engine (lib/dispatch/engine.py): queue/rank/fallback, hot-file limits, pauses, review gate, dry-run."""
import importlib.util
import json
import os
import shutil
import tempfile
import threading
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("dispatch_engine_under_test",
                                               os.path.join(os.path.dirname(HERE), "lib", "dispatch", "engine.py"))
E = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(E)

SHA = "a" * 40
SHA2 = "b" * 40


class FakeRunner:
    """Answers commands by first matching substring rule; records every call."""

    def __init__(self, rules=None):
        self.rules = list(rules or [])
        self.calls = []

    def __call__(self, cmd, timeout=120, inp=None, env=None):
        text = cmd if isinstance(cmd, str) else " ".join(str(c) for c in cmd)
        self.calls.append(text)
        for needle, result in self.rules:
            if needle in text:
                return result(text) if callable(result) else result
        return 0, "", ""

    def called(self, needle):
        return [c for c in self.calls if needle in c]


def issue(n, labels=(), body="", title=None):
    return {"number": n, "title": title or f"card {n}", "labels": [{"name": l} for l in labels], "body": body}


def pr(n, issue_n, mergeable="MERGEABLE", checks=("SUCCESS",), head=SHA, branch=None, labels=()):
    roll = [{"name": f"linux-{i}", "conclusion": c} for i, c in enumerate(checks)]
    roll.append({"name": "OpenSwarm tests (macos-latest)", "conclusion": "FAILURE"})
    return {"number": n, "title": f"pr {n}", "headRefName": branch or f"fleet/M5-issue{issue_n}-r1", "headRefOid": head,
            "isDraft": False, "mergeable": mergeable, "statusCheckRollup": roll,
            "labels": [{"name": l} for l in labels], "body": f"Closes #{issue_n}"}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dispatch-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        raw = {"repo": "acme/project", "local_repo": self.tmp, "state_dir": os.path.join(self.tmp, "state"),
               "ssh": {"control_dir": os.path.join(self.tmp, "cm")}, "local_hosts": ["box"],
               "hot_files": {"limits": {"bin/big": 2}, "union": ["docs/log.md"]},
               "burn": {"file": "BURN-X", "backend": "pi"}}
        p = os.path.join(self.tmp, "cfg.json")
        with open(p, "w") as f:
            json.dump(raw, f)
        self.cfg = E.load_config(p)

    def make(self, rules=None, dry_run=False):
        self.runner = FakeRunner(rules)
        d = E.Dispatcher(self.cfg, runner=self.runner, dry_run=dry_run, out=open(os.devnull, "w"))
        self.addCleanup(d.out.close)
        return d


class QueueAndRank(Base):
    def test_priority_pin_and_fallback_order(self):
        issues = [issue(5, ["lane-ready", "p2"]), issue(3, ["lane-ready", "p1", "backend:codex"]),
                  issue(4, ["lane-ready", "p0", "backend:agy"]), issue(9, ["lane-ready", "in-progress"]),
                  issue(2, ["lane-ready", "p1"])]
        d = self.make([("issue list", (0, json.dumps(issues), ""))])
        q = d.queue()
        self.assertEqual([i["n"] for i in q], [4, 2, 3, 5])  # in-progress dropped; p0 < p1 < p2, then number
        ranked = [i["n"] for i in sorted(q, key=lambda i: d.rank("pi", i))]
        # own pin (none for pi) → unpinned by prio → cross-pinned in FALLBACK order (codex before agy)
        self.assertEqual(ranked, [2, 5, 3, 4])

    def test_strict_and_never_take(self):
        d = self.make()
        strict = {"n": 1, "title": "t", "body": "", "backend": "pi", "strict": True}
        sock = {"n": 2, "title": "bind AF_UNIX socket", "body": "", "backend": "pi", "strict": False}
        self.assertFalse(d.can_take("codex", strict))
        self.assertFalse(d.can_take("codex", sock))
        self.assertTrue(d.can_take("agy", sock))
        self.assertTrue(d.can_take("pi", strict))

    def test_hot_file_limits_and_union(self):
        d = self.make()
        hot = d.hot_of("x\n热文件：`bin/big`， docs/log.md, src/a.py\n")
        self.assertEqual(hot, {"bin/big", "src/a.py"})  # union-merged doc is never locked
        self.assertFalse(d.hot_blocked({"bin/big"}, {"bin/big": 1}))
        self.assertTrue(d.hot_blocked({"bin/big"}, {"bin/big": 2}))
        self.assertTrue(d.hot_blocked({"src/a.py"}, {"src/a.py": 1}))
        self.assertEqual(d.hot_of("Hot-files: a.py b.py"), {"a.py", "b.py"})

    def test_verify_rebase_parsing(self):
        d = self.make()
        body = "重基PR：#77 fleet/M5-issue1-r\n\n验收：`cd openswarm` `python3 -m unittest x`"
        self.assertEqual(d.rebase_of(body), (77, "fleet/M5-issue1-r"))
        self.assertEqual(d.verify_of(body), "cd openswarm && python3 -m unittest x")
        self.assertEqual(d.verify_of("Verify: `pytest -q` `ruff check`"), "pytest -q && ruff check")


class ExternalAndChecks(Base):
    def test_external_backend_and_claimed_cards_are_skipped(self):
        pol = os.path.join(self.tmp, "policy.local.json")
        with open(pol, "w") as f:
            json.dump({"external_backends": {"chatgpt": {"enabled": True}, "off": {"enabled": False}, "_doc": {}}}, f)
        self.cfg["skip_labels"] = ["lane-claimed"]
        self.cfg["external_backends_policy"] = pol
        issues = [issue(1, ["lane-ready", "backend:chatgpt"]), issue(2, ["lane-ready", "lane-claimed"]),
                  issue(3, ["lane-ready", "backend:off"]), issue(4, ["lane-ready"])]
        d = self.make([("issue list", (0, json.dumps(issues), ""))])
        self.assertEqual([i["n"] for i in d.queue()], [3, 4])

    def test_ignored_check_red_only_with_marker_on_matching_title(self):
        d = self.make()
        p = pr(30, 300)
        p["title"] = "upstream(openswarm): x"
        self.assertFalse(d.ci_red(p))
        open(d.path("MACOS-UPSTREAM-RED"), "w").close()
        self.assertTrue(d.ci_red(p))
        p["title"] = "openswarm(pi): y"
        self.assertFalse(d.ci_red(p))


class Pauses(Base):
    def test_scopes(self):
        d = self.make()
        lc = {"host": "h1", "backend": "codex"}
        self.assertFalse(d.paused_lane("CX1", lc))
        for name in ("PAUSE-CX1", "PAUSE-HOST-h1", "PAUSE-BACKEND-codex"):
            open(d.path(name), "w").close()
            self.assertTrue(d.paused_lane("CX1", lc), name)
            os.unlink(d.path(name))

    def test_inode_hysteresis(self):
        d = self.make()
        self.assertFalse(d.inode_decide(94.0))
        self.assertTrue(d.inode_decide(95.0))
        self.assertTrue(d.inode_decide(93.0))   # stays paused until below resume
        self.assertFalse(d.inode_decide(91.9))


class ReviewGate(Base):
    def rules(self, prs, issues_by_n=None, reviews=None):
        os.makedirs(self.cfg["state_dir"], exist_ok=True)
        if reviews is not None:
            with open(os.path.join(self.cfg["state_dir"], "reviews.json"), "w") as f:
                json.dump(reviews, f)
        issues_by_n = issues_by_n or {}

        def view(text):
            n = int(text.split("issue view ")[1].split()[0])
            return 0, json.dumps(issues_by_n.get(n, {"state": "OPEN", "body": "orig"})), ""
        return [("pr list", (0, json.dumps(prs), "")), ("issue view", view)]

    def test_conflict_checked_before_reviewed_skip(self):
        # PR 10 was APPROVED at this head but went CONFLICTING after a sibling merge: it must get a rebase task.
        d = self.make(self.rules([pr(10, 100, mergeable="CONFLICTING")], reviews={"10": {"sha": SHA, "verdict": "APPROVE"}}))
        self.assertEqual(d.review_candidates({}, {}, []), [])
        edits = self.runner.called("issue edit 100")
        self.assertEqual(len(edits), 1)
        self.assertIn("Rebase-PR: #10 fleet/M5-issue100-r1", edits[0])
        self.assertIn("--remove-label in-progress", edits[0])
        self.assertEqual(d.load_reviews()["10"]["rebase_sha"], SHA)
        # once per head sha
        self.runner.calls.clear()
        d.review_candidates({}, {}, [])
        self.assertEqual(self.runner.called("issue edit 100"), [])

    def test_quota_failure_is_not_counted_and_next_review_uses_other_backend(self):
        for error_class in ("rate_limit", "quota", "vm_paused"):
            with self.subTest(error_class=error_class):
                d = self.make()
                st = {"review_pr": 31, "review_sha": SHA}
                self.assertTrue(d.finish_review("M5", {"backend": "pi"}, st,
                                                {"status": "failed", "error_class": error_class, "backend": "pi"}, {}))
                self.assertEqual(d.load_reviews()["31"].get("fails", 0), 0)
                self.assertEqual(d.load_reviews()["31"]["quota_backend"], "pi")
        lanes = {"M1": {"backend": "pi"}, "M2": {"backend": "codex"}}
        self.assertEqual(d.pick_reviewer(["M2"], lanes, None), "M2")

    def test_exec_error_increments_review_failure_count(self):
        d = self.make()
        d.finish_review("M5", {"backend": "pi"}, {"review_pr": 32, "review_sha": SHA},
                        {"status": "failed", "error_class": "exec_error"}, {})
        self.assertEqual(d.load_reviews()["32"]["fails"], 1)

    def test_review_failure_limit_still_skips_dispatch(self):
        p = pr(33, 333)
        d = self.make(self.rules([p], reviews={"33": {"fails": 4}}))
        with mock.patch.object(d, "main_sha", side_effect=AssertionError("must not dispatch")):
            d.dispatch_reviews({"M1": {"host": "box", "backend": "pi"}}, {"M1": {"lane": "idle"}}, {}, [])
        self.assertFalse(self.runner.called("dispatch"))

    def test_green_unreviewed_is_candidate_macos_ignored(self):
        prs = [pr(11, 101), pr(12, 102, head=SHA2), pr(13, 103, checks=("PENDING",)), pr(14, 104, branch="feature/x")]
        d = self.make(self.rules(prs, reviews={"12": {"sha": SHA2, "verdict": "APPROVE"}}))
        self.assertEqual([p["number"] for p in d.review_candidates({}, {}, [])], [11])

    def test_ci_red_fix_only_in_burn_mode(self):
        prs = [pr(15, 105, checks=("FAILURE",))]
        d = self.make(self.rules(prs))
        d.review_candidates({}, {}, [])
        self.assertEqual(self.runner.called("issue edit 105"), [])
        open(d.path("BURN-X"), "w").close()
        d.review_candidates({}, {}, [])
        self.assertEqual(len(self.runner.called("issue edit 105")), 1)

    def test_put_review_parallel_writers_do_not_lose_updates(self):
        d = self.make()
        ts = [threading.Thread(target=d.put_review, args=(i, {"sha": str(i), "verdict": "APPROVE"})) for i in range(30)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(sorted(int(k) for k in d.load_reviews()), list(range(30)))

    def test_approve_posts_lane_review_status_on_reviewed_sha(self):
        verdict = "VERDICT: APPROVE\nSUMMARY: ok\n"
        d = self.make([("git -C", (0, verdict, "")),
                       ("pr view 20", (0, json.dumps({"headRefOid": SHA, "state": "OPEN", "headRefName": "fleet/M5-issue200-r",
                                                      "body": "Closes #200"}), ""))])
        st = {"issue": "PR20", "run": "r1", "review_pr": 20, "review_sha": SHA}
        info = {"branch": "lane/x", "commit": "c", "backend": "pi", "model": "m"}
        self.assertTrue(d.finish_review("M5", {"host": "box", "backend": "pi"}, st, info, {}))
        status = self.runner.called(f"statuses/{SHA}")
        self.assertEqual(len(status), 1)
        self.assertIn("state=success", status[0])
        self.assertIn("context=lane-review", status[0])
        self.assertIn("description=APPROVE by M5 (pi/m) run r1", status[0])
        self.assertIn("M5", status[0])
        self.assertIn("pi", status[0])
        self.assertIn("r1", status[0])
        self.assertEqual(d.load_reviews()["20"]["verdict"], "APPROVE")

    def test_changes_posts_failure_status(self):
        verdict = "VERDICT: CHANGES\nSUMMARY: please fix\n"
        d = self.make([("git -C", (0, verdict, "")),
                       ("pr view 21", (0, json.dumps({"headRefOid": SHA, "state": "OPEN", "headRefName": "fleet/CX1-issue201-r",
                                                      "body": "Closes #201"}), "")),
                       ("issue view 201", (0, json.dumps({"body": "do x"}), ""))])
        st = {"issue": "PR21", "run": "r2", "review_pr": 21, "review_sha": SHA}
        info = {"branch": "lane/x", "commit": "c", "backend": "codex", "model": "m2"}
        self.assertTrue(d.finish_review("CX1", {"host": "box", "backend": "codex"}, st, info, {}))
        status = self.runner.called(f"statuses/{SHA}")
        self.assertEqual(len(status), 1)
        self.assertIn("state=failure", status[0])
        self.assertIn("context=lane-review", status[0])
        self.assertIn("description=CHANGES by CX1 (codex/m2) run r2", status[0])
        self.assertEqual(d.load_reviews()["21"]["verdict"], "CHANGES")

    def test_changes_requeues_fix_task_then_needs_human(self):
        verdict = "VERDICT: CHANGES\nSUMMARY: bug\n"
        d = self.make([("git -C", (0, verdict, "")),
                       ("pr view 21", (0, json.dumps({"headRefOid": SHA, "state": "OPEN", "headRefName": "fleet/M5-issue201-r",
                                                      "body": "Closes #201"}), "")),
                       ("issue view 201", (0, json.dumps({"body": "do x"}), ""))])
        st = {"issue": "PR21", "run": "r1", "review_pr": 21, "review_sha": SHA}
        info = {"branch": "lane/x", "commit": "c"}
        d.finish_review("M5", {"host": "box", "backend": "pi"}, st, info, {})
        self.assertIn("state=failure", self.runner.called(f"statuses/{SHA}")[0])
        self.assertTrue(any("--add-label lane-ready" in c for c in self.runner.called("issue edit 201")))
        d.put_review(21, {"rounds": 3})
        self.runner.calls.clear()
        d.finish_review("M5", {"host": "box", "backend": "pi"}, st, info, {})
        self.assertTrue(self.runner.called("--add-label needs-human"))

    def test_moved_head_discards_verdict(self):
        d = self.make([("git -C", (0, "VERDICT: APPROVE\n", "")),
                       ("pr view 22", (0, json.dumps({"headRefOid": SHA2, "state": "OPEN", "headRefName": "b", "body": ""}), ""))])
        st = {"issue": "PR22", "run": "r1", "review_pr": 22, "review_sha": SHA}
        d.finish_review("M5", {"host": "box", "backend": "pi"}, st, {"branch": "x", "commit": "c"}, {})
        self.assertEqual(self.runner.called("statuses/"), [])

    def test_moved_head_does_not_post_status_and_requeues(self):
        p = pr(22, 220, head=SHA2)
        d = self.make([("git -C", (0, "VERDICT: APPROVE\n", "")),
                       ("pr view 22", (0, json.dumps({"headRefOid": SHA2, "state": "OPEN", "headRefName": "fleet/M5-issue220-r", "body": ""}), "")),
                       ("pr list", (0, json.dumps([p]), ""))])
        st = {"issue": "PR22", "run": "r1", "review_pr": 22, "review_sha": SHA}
        done = d.finish_review("M5", {"host": "box", "backend": "pi"}, st, {"branch": "x", "commit": "c"}, {})
        self.assertTrue(done)
        self.assertEqual(self.runner.called("statuses/"), [])
        self.assertNotIn("22", d.load_reviews())
        # PR with SHA2 is unreviewed and requeued as a candidate
        candidates = d.review_candidates({}, {}, [])
        self.assertEqual([c["number"] for c in candidates], [22])

    def test_post_status_failure_does_not_record_approve_in_reviews(self):
        verdict = "VERDICT: APPROVE\nSUMMARY: looks good\n"
        d = self.make([("git -C", (0, verdict, "")),
                       ("pr view 23", (0, json.dumps({"headRefOid": SHA, "state": "OPEN", "headRefName": "fleet/M5-issue230-r",
                                                      "body": "Closes #230"}), "")),
                       (f"statuses/{SHA}", (1, "", "gh: API rate limit exceeded"))])
        st = {"issue": "PR23", "run": "r1", "review_pr": 23, "review_sha": SHA}
        info = {"branch": "lane/x", "commit": "c", "backend": "pi", "model": "m"}
        ok = d.finish_review("M5", {"host": "box", "backend": "pi"}, st, info, {})
        self.assertFalse(ok)
        reviews = d.load_reviews()
        self.assertNotIn("23", reviews)

    def test_dry_run_does_not_post_status(self):
        verdict = "VERDICT: APPROVE\nSUMMARY: ok\n"
        d = self.make([("git -C", (0, verdict, "")),
                       ("pr view 24", (0, json.dumps({"headRefOid": SHA, "state": "OPEN", "headRefName": "fleet/M5-issue240-r",
                                                      "body": "Closes #240"}), ""))], dry_run=True)
        st = {"issue": "PR24", "run": "r1", "review_pr": 24, "review_sha": SHA}
        info = {"branch": "lane/x", "commit": "c", "backend": "pi", "model": "m"}
        d.finish_review("M5", {"host": "box", "backend": "pi"}, st, info, {})
        self.assertEqual(self.runner.called(f"statuses/{SHA}"), [])
        self.assertNotIn("24", d.load_reviews())

    def test_no_token_skips_status_without_failing_review(self):
        verdict = "VERDICT: APPROVE\nSUMMARY: ok\n"
        d = self.make([("git -C", (0, verdict, "")),
                       ("pr view 25", (0, json.dumps({"headRefOid": SHA, "state": "OPEN", "headRefName": "fleet/M5-issue250-r",
                                                      "body": "Closes #250"}), "")),
                       ("auth status", (1, "", "not logged in"))])
        st = {"issue": "PR25", "run": "r1", "review_pr": 25, "review_sha": SHA}
        info = {"branch": "lane/x", "commit": "c", "backend": "pi", "model": "m"}
        with mock.patch.dict(os.environ, {"GH_TOKEN": "", "GITHUB_TOKEN": ""}):
            ok = d.finish_review("M5", {"host": "box", "backend": "pi"}, st, info, {})
        self.assertTrue(ok)
        self.assertEqual(self.runner.called("statuses/"), [])
        self.assertEqual(d.load_reviews()["25"]["verdict"], "APPROVE")


class RebaseHarvest(Base):
    ST = {"issue": 7, "run": "r1", "base": SHA, "rebase_pr": 70, "rebase_branch": "fleet/M5-issue7-r0"}
    INFO = {"branch": "lane/x", "commit": "c", "status": "done", "repo": "/w"}

    def harvest(self, rules, st=None):
        d = self.make(rules)
        st = dict(st or self.ST)
        state = {"M5": st}
        return d, d.finish("M5", {"host": "box", "backend": "pi"}, st, dict(self.INFO), state), state

    def test_nothing_to_push_is_lane_failed_not_lane_pr(self):
        d, done, state = self.harvest([("merge-base", (1, "", "")), ("diff --stat", (0, " a | 1\n", ""))])
        self.assertTrue(done)
        self.assertEqual(self.runner.called(" push -q"), [])
        edits = " ".join(self.runner.called("issue edit 7"))
        self.assertIn("--add-label lane-failed", edits)
        self.assertNotIn("--add-label lane-pr", edits)
        self.assertIn(7, state["_done"])

    def test_push_always_uses_lease(self):
        ok = [("merge-base", (0, "", "")), ("diff --stat", (0, " a | 1\n", ""))]
        _, done, _ = self.harvest(ok, dict(self.ST, rebase_head=SHA2))
        push = self.runner.called(" push -q")
        self.assertTrue(done)
        self.assertEqual(len(push), 1)
        self.assertIn(f"--force-with-lease=refs/heads/fleet/M5-issue7-r0:{SHA2}", push[0])
        # no recorded head: read it now and lease against it, never a bare --force
        _, done, _ = self.harvest(ok + [("ls-remote", (0, SHA + "\trefs/heads/x\n", ""))])
        push = self.runner.called(" push -q")
        self.assertIn(f"--force-with-lease=refs/heads/fleet/M5-issue7-r0:{SHA}", push[0])
        self.assertFalse(any(c.split().count("--force") for c in push))
        # head unreadable: do not push at all, retry next tick
        _, done, _ = self.harvest(ok + [("ls-remote", (2, "", "boom"))])
        self.assertFalse(done)
        self.assertEqual(self.runner.called(" push -q"), [])

    def test_moved_head_keeps_newer_head(self):
        rules = [("merge-base", (0, "", "")), ("diff --stat", (0, " a | 1\n", "")),
                 (" push -q", (1, "", "! [rejected] (stale info)"))]
        _, done, _ = self.harvest(rules, dict(self.ST, rebase_head=SHA2))
        self.assertTrue(done)
        self.assertIn("--add-label lane-pr", " ".join(self.runner.called("issue edit 7")))


class FreshReads(Base):
    def test_read_failure_is_not_stale_and_budget_caps_reads(self):
        self.cfg["fresh_read_budget"] = 2
        d = self.make([("issue view 1 ", (1, "", "HTTP 502")),
                       ("issue view", (0, json.dumps({"state": "OPEN", "labels": [{"name": "lane-pr"}]}), ""))])
        self.assertEqual(d.label_state(1), "unknown")
        self.assertEqual(d.label_state(2), "stale")
        self.assertEqual(d.label_state(3), "unknown")       # budget spent
        self.assertEqual(d.label_state(2), "stale")         # cached, no extra read
        self.assertEqual(len(self.runner.called("issue view")), 2)


class Round2(Base):
    def test_pr_without_closes_is_still_reviewed_with_stale_skips_in_state(self):
        prs = [pr(40, 400)]
        prs[0]["body"] = "no issue reference"
        d = self.make([("pr list", (0, json.dumps(prs), ""))])
        state = {"_stale_skips": {"9": 1}, "M5": {"issue": 3}}
        self.assertEqual([p["number"] for p in d.review_candidates({}, state, [])], [40])

    def test_moved_head_removes_only_present_labels(self):
        p = pr(41, 401, head=SHA2, labels=("lane-reviewed",))
        d = self.make([("pr list", (0, json.dumps([p]), "")), ("issue view", (0, json.dumps({"state": "OPEN", "body": ""}), ""))])
        d.review_candidates({}, {}, [])
        edit = self.runner.called("pr edit 41")[0]
        self.assertIn("--remove-label lane-reviewed", edit)
        self.assertNotIn("needs-changes", edit)

    def test_failed_write_is_logged(self):
        d = self.make([("issue edit", (1, "", "HTTP 502"))])
        logged = []
        d.log = lambda msg, brain=True: logged.append(msg)
        d.relabel(5, "lane-pr", ["in-progress"])
        self.assertTrue(any("WRITE FAILED" in m and "HTTP 502" in m for m in logged))

    def test_stale_strip_skips_in_progress(self):
        self.cfg["stale_strip_after"] = 1
        d = self.make([("issue view", (0, json.dumps({"state": "OPEN", "labels": [{"name": "lane-ready"}, {"name": "in-progress"}]}), ""))])
        d.note_stale_skip(6, {})
        self.assertEqual(self.runner.called("issue edit"), [])
        d = self.make([("issue view", (0, json.dumps({"state": "OPEN", "labels": [{"name": "lane-ready"}, {"name": "lane-pr"}]}), ""))])
        d.note_stale_skip(6, {})
        self.assertEqual(len(self.runner.called("issue edit 6 --remove-label lane-ready")), 1)


class Assign(Base):
    def test_idempotency_key_reused_on_transport_retry(self):
        calls = {"n": 0}

        def ssh(text):
            calls["n"] += 1
            return (255, "", "Connection reset") if calls["n"] == 1 else (0, '{"id": "run-1"}', "")
        d = self.make([("cat > /tmp/task", (0, "", "")), ("lane assign", ssh)])
        item = {"n": 5, "title": "t", "body": "", "backend": None}
        run, _ = d.dispatch("M8", {"host": "far", "backend": "pi"}, item, SHA)
        self.assertEqual(run, "run-1")
        keys = [c.split("--idempotency-key ")[1].split()[0] for c in self.runner.called("lane assign")]
        self.assertEqual(len(keys), 2)
        self.assertEqual(keys[0], keys[1])

    def test_key_can_be_disabled_for_old_fleet_releases(self):
        self.cfg["assign_idempotency_key"] = False
        d = self.make([("cat > /tmp/task", (0, "", "")), ("lane assign", (0, '{"id": "run-2"}', ""))])
        d.dispatch("M8", {"host": "far", "backend": "pi"}, {"n": 5, "title": "t", "body": "", "backend": None}, SHA)
        self.assertNotIn("--idempotency-key", self.runner.called("lane assign")[0])


class LanePickingAndBurn(Base):
    def lanes(self):
        return {"P1": {"host": "far", "backend": "pi"},
                "C1": {"host": "far", "backend": "codex"},
                "A1": {"host": "far", "backend": "agy"}}

    def history(self, d, value):
        os.makedirs(d.D, exist_ok=True)
        with open(d.path("lane-lru.json"), "w") as stream:
            json.dump(value, stream)

    def order(self, d):
        return [name for name, _ in d.lane_order(self.lanes())]

    def timed_burn(self):
        self.cfg["burn"] = {"backend": "pi", "window": {"resets_at": 10000},
                            "burn_before_reset_h": 1, "factor": 2}

    def test_legacy_lru_orders_oldest_lane_and_breaks_ties_by_name(self):
        d = self.make()
        self.history(d, {"P1": "2026-11-01 12:03:00", "C1": "2026-11-01 12:01:00",
                         "A1": "2026-11-01 12:02:00"})
        self.assertEqual(self.order(d), ["C1", "A1", "P1"])
        self.history(d, {"P1": "2026-11-01 12:03:00", "C1": "2026-11-01 12:01:00",
                         "A1": "2026-11-01 12:01:00"})
        self.assertEqual(self.order(d), ["A1", "C1", "P1"])

    def test_native_picker_state_preserves_dispatch_counter_order(self):
        d = self.make()
        self.history(d, {"version": 1, "counter": 10, "lanes": {"P1": 10, "C1": 1, "A1": 2}})
        self.assertEqual(self.order(d), ["C1", "A1", "P1"])

    def test_corrupt_lru_falls_back_as_a_whole_to_name_order(self):
        d = self.make()
        bad_states = ('{"P1":', '["P1"]',
                      '{"version":1,"counter":2,"lanes":{"C1":1,"P1":"bad"}}',
                      '{"P1":"2026-11-01 12:00:00","C1":null}')
        for text in bad_states:
            with self.subTest(text=text):
                with open(d.path("lane-lru.json"), "w") as stream:
                    stream.write(text)
                self.assertEqual(self.order(d), ["A1", "C1", "P1"])

    def test_dry_run_reads_legacy_history_without_migrating_or_touching_it(self):
        d = self.make(dry_run=True)
        self.history(d, {"P1": "2026-11-01 12:00:00", "C1": "2026-11-01 13:00:00"})
        with open(d.path("lane-lru.json"), "rb") as stream:
            before = stream.read()
        self.assertEqual(self.order(d), ["A1", "P1", "C1"])
        with open(d.path("lane-lru.json"), "rb") as stream:
            self.assertEqual(stream.read(), before)

    def test_burn_window_prioritizes_configured_backend_over_lru(self):
        self.timed_burn()
        d = self.make()
        d.clock = lambda: 9000
        self.history(d, {"version": 1, "counter": 10, "lanes": {"C1": 1, "A1": 2, "P1": 10}})
        self.assertEqual(self.order(d), ["P1", "C1", "A1"])
        self.assertEqual(d.review_max(), 12)  # factor 2 is capped by review.burn_max

    def test_outside_burn_window_preserves_lru_and_review_limit(self):
        self.timed_burn()
        d = self.make()
        self.history(d, {"version": 1, "counter": 10, "lanes": {"C1": 1, "A1": 2, "P1": 10}})
        for now in (6399, 10001):
            with self.subTest(now=now):
                d.clock = lambda now=now: now
                self.assertEqual(self.order(d), ["C1", "A1", "P1"])
                self.assertEqual(d.review_max(), 8)

    def test_burn_scaling_is_not_always_the_legacy_fixed_burn_max(self):
        self.timed_burn()
        self.cfg["burn"]["factor"] = 1.25
        d = self.make()
        d.clock = lambda: 9000
        self.assertEqual(d.review_max(), 10)

    def test_missing_burn_config_keeps_normal_order_and_limit(self):
        self.cfg["burn"] = {}
        d = self.make()
        self.history(d, {"version": 1, "counter": 10, "lanes": {"C1": 1, "A1": 2, "P1": 10}})
        self.assertEqual(self.order(d), ["C1", "A1", "P1"])
        self.assertEqual(d.review_max(), 8)

    def test_legacy_marker_can_enable_review_capacity_from_zero(self):
        self.cfg["review"]["max"] = 0
        d = self.make()
        self.assertEqual(d.review_max(), 0)
        with open(d.path("BURN-X"), "w"):
            pass
        self.assertEqual(d.review_max(), 12)

    def task_dispatch(self, d, fail=False):
        lanes = {"P1": self.lanes()["P1"]}
        d.main_sha = lambda: SHA
        d.label_state = lambda n: "fresh"
        calls = []

        def dispatch(*args):
            calls.append(args[0])
            if fail:
                raise RuntimeError("fixture assignment failed")
            return "run-1", "verify"

        d.dispatch = dispatch
        state = {}
        queue = [{"n": 1, "title": "task", "body": "", "backend": None, "strict": False, "prio": 2}]
        d.assign_issues(lanes, {"P1": {"lane": "idle"}}, state, queue)
        return calls, state

    def test_successful_task_assignment_migrates_legacy_and_touches_picker(self):
        d = self.make()
        self.history(d, {"P1": "2026-11-01 12:00:00", "C1": "2026-11-01 13:00:00"})
        calls, state = self.task_dispatch(d)
        self.assertEqual(calls, ["P1"])
        self.assertEqual(state["P1"]["run"], "run-1")
        with open(d.path("lane-lru.json")) as stream:
            history = json.load(stream)
        self.assertEqual(history["version"], 1)
        self.assertGreater(history["lanes"]["P1"], history["lanes"]["C1"])
        self.assertEqual(history["counter"], history["lanes"]["P1"])

    def test_failed_or_dry_run_assignment_does_not_touch_lru(self):
        for dry_run, fail in ((False, True), (True, False)):
            with self.subTest(dry_run=dry_run, fail=fail):
                self.cfg["state_dir"] = os.path.join(self.tmp, f"state-{dry_run}-{fail}")
                d = self.make(dry_run=dry_run)
                self.task_dispatch(d, fail=fail)
                self.assertFalse(os.path.exists(d.path("lane-lru.json")))

    def test_successful_review_assignment_touches_same_picker_history(self):
        d = self.make()
        lanes = {"P1": self.lanes()["P1"]}
        d.main_sha = lambda: SHA
        d.review_candidates = lambda *args: [pr(11, 101)]
        d.review_item = lambda item: {"n": "PR11", "title": "review", "body": ""}
        d.dispatch = lambda *args: ("review-run", "verify")
        state = {}
        d.dispatch_reviews(lanes, {"P1": {"lane": "idle"}}, state, [])
        self.assertEqual(state["P1"]["review_pr"], 11)
        with open(d.path("lane-lru.json")) as stream:
            history = json.load(stream)
        self.assertEqual(history["version"], 1)
        self.assertEqual(history["lanes"]["P1"], history["counter"])


class Defaults(unittest.TestCase):
    def test_remote_home_matches_fleet_wrapper(self):
        with open(os.path.join(os.path.dirname(HERE), "bin", "openswarm-fleet")) as f:
            m = __import__("re").search(r'OPENSWARM_HOME="\$HOME/([^"]+)"', f.read())
        self.assertEqual(E.DEFAULTS["remote_home"], "~/" + m.group(1))


class DryRunTick(Base):
    def test_dry_run_decides_without_writes(self):
        lanes = {"M5": {"host": "box", "backend": "pi"}, "CX1": {"host": "box", "backend": "codex"}}
        os.makedirs(self.cfg["state_dir"], exist_ok=True)
        with open(self.cfg["lanes_file"], "w") as f:
            json.dump(lanes, f)
        idle = json.dumps({"lane": "idle", "run": None})
        issues = [issue(1, ["lane-ready", "p1", "backend:pi"]), issue(2, ["lane-ready", "p1"])]
        d = self.make([("python3 -", (0, idle, "")),
                       ("issue list --state open --label lane-ready", (0, json.dumps(issues), "")),
                       ("issue list", (0, "[]", "")),
                       ("pr list", (0, "[]", "")),
                       ("issue view", (0, json.dumps({"state": "OPEN", "labels": [{"name": "lane-ready"}]}), "")),
                       ("ls-remote", (0, SHA + "\trefs/heads/main\n", ""))], dry_run=True)
        d.tick(lanes, {})
        assigns = sorted((p["lane"], p["issue"]) for p in d.plan if p.get("would") == "assign")
        self.assertEqual(assigns, [("CX1", 2), ("M5", 1)])
        self.assertEqual(self.runner.called("issue edit"), [])
        self.assertEqual(self.runner.called("lane assign"), [])
        for name in ("state.json", "lanes-status.json", "lane-lru.json", "heartbeat"):
            self.assertFalse(os.path.exists(d.path(name)), name)

    def test_dry_run_creates_no_state_dir(self):
        self.cfg["state_dir"] = os.path.join(self.tmp, "fresh-state")
        d = self.make([("python3 -", (0, json.dumps({"lane": "idle", "run": None}), "")), ("issue list", (0, "[]", "")),
                       ("pr list", (0, "[]", ""))], dry_run=True)
        d.tick({"M5": {"host": "box", "backend": "pi"}}, {})
        self.assertFalse(os.path.exists(self.cfg["state_dir"]))


class Cli(Base):
    def test_check_config_merges_defaults(self):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = E.main(["dispatch", "check-config", "--config", os.path.join(self.tmp, "cfg.json")])
        self.assertEqual(rc, 0)
        merged = json.loads(buf.getvalue())
        self.assertEqual(merged["repo"], "acme/project")
        self.assertEqual(merged["review"]["status_context"], "lane-review")
        self.assertEqual(merged["hot_files"]["limits"], {"bin/big": 2})


class ScriptRun(unittest.TestCase):
    def test_sibling_modules_do_not_shadow_stdlib_when_run_as_script(self):
        # lib/dispatch/queue.py once shadowed the stdlib queue that concurrent.futures needs
        import subprocess
        import sys
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        for mod in ("queue", "json"):
            with open(os.path.join(tmp, f"{mod}.py"), "w") as f:
                f.write(f"raise ImportError('sibling {mod}.py shadowed the stdlib')\n")
        shutil.copy(os.path.join(os.path.dirname(HERE), "lib", "dispatch", "engine.py"), os.path.join(tmp, "engine.py"))
        cfg = os.path.join(tmp, "c.txt")
        with open(cfg, "w") as f:
            f.write('{"repo": "a/b", "state_dir": "%s"}' % tmp)
        r = subprocess.run([sys.executable, os.path.join(tmp, "engine.py"), "dispatch", "check-config", "--config", cfg],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr[-500:])
        self.assertIn('"repo": "a/b"', r.stdout)


class ThinDiff(unittest.TestCase):
    def test_classify(self):
        f = lambda s: E.classify_numstat(s, 30, (".md",), (".py",), ("tests/", "bin/"))
        self.assertEqual(f(""), "empty-diff")
        self.assertTrue(f("10\t2\ta.py\n").startswith("eff<30"))
        self.assertEqual(f("40\t0\topenswarm/bin/openswarm\n"), "")
        self.assertTrue(f("35\t0\ta.py\n90\t0\tdocs/x.md\n").startswith("mostly-docs"))


if __name__ == "__main__":
    unittest.main()
