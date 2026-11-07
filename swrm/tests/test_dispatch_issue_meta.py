"""Claim metadata on dispatch: the assignee and the project's Lane field (Refs #1761)."""
import importlib.util
import io
import json
import os
import threading
import time
import unittest
from unittest import mock

from tests.test_dispatch_engine import Base, E, SHA, pr

_spec = importlib.util.spec_from_file_location("issue_meta_under_test",
                                               os.path.join(os.path.dirname(os.path.dirname(
                                                   os.path.abspath(__file__))), "lib", "dispatch", "issue_meta.py"))
M = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(M)


class FakeProject:
    """A tiny in-memory GitHub project: fields, items and the Lane value of every item.

    The json answers follow the shape of the real gh cli: ``field-list`` wraps the array
    in ``fields``, ``item-list`` in ``items`` and ``project view`` answers the project
    itself (``fields`` is a count there, not the field list).
    """

    def __init__(self, fields=None, items=None):
        self.fields = fields if fields is not None else [{"id": "PVTF_lane", "name": "Lane", "type": "ProjectV2Field"}]
        self.items = list(items or [])          # [{"id": "PVTI_7", "content": {...}}]
        self.values = {}                        # item id -> lane value
        self.calls = []
        self.assignees = {}                     # issue number -> [logins]
        self.fail = []                          # command fragments that must fail
        self.lock = threading.Lock()
        self.active = {}                        # issue number -> writers in flight
        self.races = []                         # overlapping writes of the same issue
        self.delay_s = 0.0

    # ------------------------------------------------------------ helpers
    def _fail(self, text):
        return any(f in text for f in self.fail)

    def _issue_of(self, cmd, text):
        if cmd[:3] == ["gh", "issue", "edit"] and len(cmd) > 3:
            return cmd[3]
        if "--id" in cmd:
            item = cmd[cmd.index("--id") + 1]
            for it in self.items:
                if it["id"] == item:
                    return str(it["content"]["number"])
        return None

    def __call__(self, cmd, timeout=120, inp=None, env=None):
        text = " ".join(str(c) for c in cmd)
        with self.lock:
            self.calls.append(text)
        if self._fail(text):
            return 1, "", f"gh: simulated failure for {text[:60]}"
        issue = self._issue_of(cmd, text)
        if issue is not None:
            with self.lock:
                self.active[issue] = self.active.get(issue, 0) + 1
                if self.active[issue] > 1:
                    self.races.append(issue)
        try:
            if self.delay_s:
                time.sleep(self.delay_s)
            return self._answer(cmd, text)
        finally:
            if issue is not None:
                with self.lock:
                    self.active[issue] -= 1

    def _answer(self, cmd, text):
        if cmd[:3] == ["gh", "issue", "edit"] and "--add-assignee" in cmd:
            n, login = cmd[3], cmd[cmd.index("--add-assignee") + 1]
            self.assignees.setdefault(n, []).append(login)
            return 0, "", ""
        if "project view" in text:
            return 0, json.dumps({"id": "PVT_1", "number": 6, "title": "board",
                                   "fields": {"totalCount": len(self.fields)},
                                   "items": {"totalCount": len(self.items)}}), ""
        if "project field-list" in text:
            return 0, json.dumps({"fields": self.fields, "totalCount": len(self.fields)}), ""
        if "project item-list" in text:
            return 0, json.dumps({"items": self.items, "totalCount": len(self.items)}), ""
        if "project item-add" in text:
            url = cmd[cmd.index("--url") + 1]
            number = url.rstrip("/").rsplit("/", 1)[-1]
            repo = "/".join(url.split("/")[3:5])
            item = {"id": f"PVTI_{number}",
                    "content": {"type": "Issue", "body": "", "title": f"card {number}",
                                "number": int(number), "repository": repo, "url": url}}
            self.items.append(item)
            return 0, json.dumps({"id": item["id"], "title": item["content"]["title"],
                                   "body": "", "type": "Issue", "url": url}), ""
        if "project item-edit" in text:
            item = cmd[cmd.index("--id") + 1]
            if "--text" in cmd:
                self.values[item] = cmd[cmd.index("--text") + 1]
            return 0, "", ""
        return 0, "", ""

    # ------------------------------------------------------------ assertions helpers
    def lane_values(self):
        return [self.values.get(f"PVTI_{n}") for n in sorted(self.assignees, key=lambda x: int(x))]

    def called(self, needle):
        return [c for c in self.calls if needle in c]


class IssueMetaUnit(unittest.TestCase):
    def meta(self, project=None, cfg=None, **kwargs):
        project = project or FakeProject()
        cfg = dict({"enabled": True, "assignee": "surlymochan", "project_number": 6,
                    "project_owner": "surlymochan", "lane_field": "Lane"}, **(cfg or {}))
        return M.IssueMeta("surlymochan/phanes", cfg, runner=project, log=lambda m, brain=True: None, **kwargs), project

    def test_assignee_and_lane_are_written(self):
        meta, project = self.meta()
        res = meta.claim(7, "M21")
        self.assertTrue(res["ok"], res)
        self.assertEqual(project.assignees["7"], ["surlymochan"])
        self.assertEqual(project.values["PVTI_7"], "M21")
        self.assertTrue(any("--add-assignee surlymochan" in c for c in project.calls))

    def test_field_id_is_resolved_by_name(self):
        project = FakeProject(fields=[{"id": "PVTF_other", "name": "Status", "type": "ProjectV2Field"},
                                      {"id": "PVTF_lane", "name": "Lane", "type": "ProjectV2Field"}])
        meta, _ = self.meta(project)
        meta.claim(7, "M21")
        edit = [c for c in project.calls if "item-edit" in c][0]
        self.assertIn("--field-id PVTF_lane", edit)
        self.assertNotIn("PVTF_other", edit)

    def test_the_field_list_of_the_real_gh_is_parsed(self):
        # gh answers {"fields": [...]}; the bare array of an old fake hid the bug
        project = FakeProject(fields=[{"id": "PVTF_lane", "name": "Lane", "type": "ProjectV2Field"}])
        meta, _ = self.meta(project)
        res = meta.claim(7, "M21")
        self.assertTrue(res["ok"], res)
        edit = [c for c in project.calls if "item-edit" in c][0]
        self.assertIn("--field-id PVTF_lane", edit)
        self.assertIn("--text M21", edit)

    def test_the_short_field_type_spelling_is_accepted(self):
        project = FakeProject(fields=[{"id": "PVTF_lane", "name": "Lane", "type": "TEXT"}])
        meta, _ = self.meta(project)
        res = meta.claim(7, "M21")
        self.assertTrue(res["ok"], res)
        self.assertEqual(project.values["PVTI_7"], "M21")

    def test_a_single_select_lane_field_writes_the_option_id(self):
        project = FakeProject(fields=[{"id": "PVTF_lane", "name": "Lane",
                                       "type": "ProjectV2SingleSelectField",
                                       "options": [{"id": "opt-m21", "name": "M21"}]}])
        meta, _ = self.meta(project)
        res = meta.claim(7, "M21")
        self.assertTrue(res["ok"], res)
        edit = [c for c in project.calls if "item-edit" in c][0]
        self.assertIn("--single-select-option-id opt-m21", edit)

    def test_unknown_field_degrades_to_the_assignee_only(self):
        project = FakeProject(fields=[{"id": "PVTF_other", "name": "Status", "type": "TEXT"}])
        meta, _ = self.meta(project)
        res = meta.claim(7, "M21")
        self.assertFalse(res["ok"])
        self.assertIn("Lane", res["errors"][0])
        self.assertEqual(project.assignees["7"], ["surlymochan"])     # the assignee still landed
        self.assertFalse([c for c in project.calls if "item-edit" in c])

    def test_missing_item_is_added_before_the_field_is_set(self):
        project = FakeProject(items=[])
        meta, _ = self.meta(project)
        res = meta.claim(7, "M21")
        self.assertTrue(res["ok"], res)
        self.assertTrue(project.called("project item-add"))
        self.assertTrue(project.called("item-edit"))

    def test_existing_item_is_not_added_again(self):
        project = FakeProject(items=[{"id": "PVTI_7", "content": {"type": "Issue", "body": "", "title": "card 7",
                                                                    "number": 7, "repository": "surlymochan/phanes",
                                                                    "url": "https://github.com/surlymochan/phanes/issues/7"}}])
        meta, _ = self.meta(project)
        res = meta.claim(7, "M21")
        self.assertTrue(res["ok"], res)
        self.assertFalse(project.called("project item-add"))
        self.assertEqual(project.values["PVTI_7"], "M21")

    def test_an_item_of_our_repository_is_matched_by_repository_and_number(self):
        project = FakeProject(items=[{"id": "PVTI_7", "content": {"type": "Issue", "number": 7,
                                                                    "repository": "surlymochan/phanes"}}])
        meta, _ = self.meta(project)
        res = meta.claim(7, "M21")
        self.assertTrue(res["ok"], res)
        self.assertFalse(project.called("project item-add"))
        self.assertEqual(project.values["PVTI_7"], "M21")

    def test_an_item_of_another_repository_is_not_our_issue(self):
        # #7 of another repo is not our #7: the lane of that card must stay untouched
        project = FakeProject(items=[{"id": "PVTI_other7", "content": {"type": "Issue", "body": "", "title": "other",
                                                                         "number": 7, "repository": "other/repo",
                                                                         "url": "https://github.com/other/repo/issues/7"}}])
        meta, _ = self.meta(project)
        res = meta.claim(7, "M21")
        self.assertTrue(res["ok"], res)
        self.assertTrue(project.called("project item-add"))          # our issue joins the board
        self.assertIsNone(project.values.get("PVTI_other7"))         # the other card keeps its lane
        self.assertEqual(project.values["PVTI_7"], "M21")
        edit = [c for c in project.calls if "item-edit" in c][0]
        self.assertIn("--id PVTI_7", edit)

    def test_gh_failure_is_reported_not_raised(self):
        project = FakeProject()
        project.fail = ["item-edit"]
        meta, _ = self.meta(project)
        res = meta.claim(7, "M21")
        self.assertFalse(res["ok"])
        self.assertEqual(project.assignees["7"], ["surlymochan"])     # the assignee still landed

    def test_disabled_config_writes_nothing(self):
        project = FakeProject()
        meta, _ = self.meta(project, cfg={"enabled": False})
        res = meta.claim(7, "M21")
        self.assertTrue(res["ok"])
        self.assertEqual(project.calls, [])

    def test_without_a_project_only_the_assignee_is_written(self):
        project = FakeProject()
        meta, _ = self.meta(project, cfg={"project_number": 0})
        res = meta.claim(7, "M21")
        self.assertTrue(res["ok"], res)
        self.assertEqual(project.assignees["7"], ["surlymochan"])
        self.assertFalse([c for c in project.calls if "project" in c])

    def test_the_lane_field_name_is_configurable(self):
        project = FakeProject(fields=[{"id": "PVTF_owner", "name": "Owner Lane", "type": "TEXT"}])
        meta, _ = self.meta(project, cfg={"lane_field": "Owner Lane"})
        res = meta.claim(7, "M21")
        self.assertTrue(res["ok"], res)
        edit = [c for c in project.calls if "item-edit" in c][0]
        self.assertIn("--field-id PVTF_owner", edit)

    def test_assignee_defaults_to_the_repo_owner(self):
        project = FakeProject()
        meta = M.IssueMeta("surlymochan/phanes", {"project_number": 0}, runner=project)
        meta.claim(7, "")
        self.assertEqual(project.assignees["7"], ["surlymochan"])

    def test_concurrent_claims_of_one_issue_are_serialized(self):
        project = FakeProject()
        project.delay_s = 0.01
        meta, _ = self.meta(project)
        threads = [threading.Thread(target=meta.claim, args=(7, f"M{i}")) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(project.races, [])


class ClaimOnDispatch(Base):
    """The dispatcher stamps the claim when it hands a card to a lane."""

    def dispatcher(self, project=None, dry_run=False):
        project = project or FakeProject()
        out = io.StringIO()
        d = E.Dispatcher(self.cfg, runner=project, dry_run=dry_run, out=out)
        self.addCleanup(d.out.close)
        d.log_lines = out
        d.project = project
        return d

    def dispatch_one(self, d, state, lane, n=7):
        d.main_sha = lambda: SHA
        d.label_state = lambda _n: "fresh"
        d.dispatch = lambda _lane, _lc, item, _base: (f"run-{item['n']}", "verify")
        lanes = {lane: {"host": "box", "backend": "pi"}}
        d.assign_issues(lanes, {lane: {"lane": "idle"}}, state,
                        [{"n": n, "title": f"card {n}", "body": "", "backend": None, "strict": False, "prio": 1}])

    def test_dispatch_assigns_the_issue_and_stamps_the_lane(self):
        d = self.dispatcher()
        state = {}
        self.dispatch_one(d, state, "M21")
        self.assertEqual(d.project.assignees["7"], ["acme"])           # the repo owner of acme/project
        self.assertEqual(d.project.values["PVTI_7"], "M21")
        self.assertEqual(state["M21"]["issue"], 7)

    def test_dispatch_reuses_the_existing_project_item(self):
        # the card is already on the board: the dispatch must reuse that item and only
        # write the Lane field on it (the gh answers come in the fields/items wrapper)
        project = FakeProject(items=[{"id": "PVTI_7", "content": {"type": "Issue", "body": "",
                                                                    "title": "card 7", "number": 7,
                                                                    "repository": "acme/project",
                                                                    "url": "https://github.com/acme/project/issues/7"}}])
        d = self.dispatcher(project)
        state = {}
        self.dispatch_one(d, state, "M21")
        self.assertEqual(d.project.assignees["7"], ["acme"])
        self.assertFalse(project.called("project item-add"))            # the item is reused
        self.assertEqual(project.values["PVTI_7"], "M21")               # the Lane field is written on it
        edit = [c for c in project.calls if "item-edit" in c][0]
        self.assertIn("--id PVTI_7", edit)
        self.assertIn("--text M21", edit)
        self.assertTrue(project.called("project field-list"))           # the field id is resolved by name
        self.assertTrue(project.called("project item-list"))            # the item is looked up in the board
        self.assertEqual(state["M21"]["issue"], 7)

    def test_redispatch_overwrites_the_lane_field(self):
        d = self.dispatcher()
        state = {}
        self.dispatch_one(d, state, "M21")
        state.clear()
        self.dispatch_one(d, state, "M22")
        self.assertEqual(d.project.values["PVTI_7"], "M22")

    def test_metadata_failure_does_not_block_the_dispatch(self):
        project = FakeProject()
        project.fail = ["item-edit"]
        d = self.dispatcher(project)
        state = {}
        self.dispatch_one(d, state, "M21")
        self.assertEqual(state["M21"]["issue"], 7)                      # the lane still got the card
        self.assertIn("claim metadata #7", d.log_lines.getvalue())       # and the failure is visible
        self.assertIn("7", d.load_meta_debt())

    def test_a_broken_debt_file_does_not_block_the_dispatch(self):
        # the debt file cannot be written: the dispatch still goes on and the lane state is saved
        project = FakeProject()
        project.fail = ["item-edit"]
        d = self.dispatcher(project)
        state = {}
        real_write = E.write_json

        def boom(path, data, indent=1):
            if path.endswith("meta-debt.json"):
                raise OSError("no space left on device")
            return real_write(path, data, indent=indent)

        with mock.patch.object(E, "write_json", boom):
            self.dispatch_one(d, state, "M21")
        self.assertEqual(state["M21"]["issue"], 7)                      # the lane still got the card
        with open(d.path("state.json")) as f:
            self.assertEqual(json.load(f)["M21"]["issue"], 7)            # and the state is on disk
        self.assertIn("claim metadata #7", d.log_lines.getvalue())       # the failure is visible
        self.assertIn("debt", d.log_lines.getvalue())

    def test_failed_metadata_is_retried_once_on_the_next_tick(self):
        project = FakeProject()
        project.fail = ["item-edit"]
        d = self.dispatcher(project)
        state = {}
        self.dispatch_one(d, state, "M21")
        self.assertIn("7", d.load_meta_debt())
        project.fail = []                                              # gh recovers
        d.replay_meta_debt()
        self.assertEqual(project.values["PVTI_7"], "M21")
        self.assertEqual(d.load_meta_debt(), {})

    def test_metadata_is_given_up_after_one_retry(self):
        project = FakeProject()
        project.fail = ["item-edit"]
        d = self.dispatcher(project)
        state = {}
        self.dispatch_one(d, state, "M21")
        d.replay_meta_debt()
        self.assertIn("7", d.load_meta_debt())                         # the retry failed: kept once
        d.replay_meta_debt()
        self.assertEqual(d.load_meta_debt(), {})                       # given up after one retry

    def test_a_new_dispatch_wins_over_the_retry_of_an_older_lane(self):
        # the retry of the old lane and a new dispatch of the same card must not fight:
        # the lane that claims the card last owns the Lane field.
        project = FakeProject()
        project.fail = ["item-edit"]
        d = self.dispatcher(project)
        state = {}
        self.dispatch_one(d, state, "M21")                             # the write fails: the debt is recorded
        self.assertIn("7", d.load_meta_debt())
        project.fail = []                                              # gh recovers
        snapshot_taken = threading.Event()
        resume = threading.Event()
        real_load = d.load_meta_debt

        def slow_load():
            # the retry takes its snapshot first: hold it there until the new dispatch
            # has claimed the card, so both run against the same card
            data = real_load()
            if threading.current_thread() is worker and not snapshot_taken.is_set():
                snapshot_taken.set()
                resume.wait(10)
            return data

        worker = threading.Thread(target=d.replay_meta_debt)
        d.load_meta_debt = slow_load
        worker.start()
        self.assertTrue(snapshot_taken.wait(10))                      # the retry waits for its snapshot
        state.clear()
        self.dispatch_one(d, state, "M22")                             # a new lane claims the card meanwhile
        resume.set()
        worker.join(20)
        self.assertEqual(project.values["PVTI_7"], "M22")              # the newest lane owns the field
        self.assertEqual(d.load_meta_debt(), {})

    def test_review_dispatch_stamps_the_lane_of_the_related_issue(self):
        d = self.dispatcher()
        p = pr(11, 101)
        p["issue"] = 101
        d.review_candidates = lambda *args: [p]
        d.main_sha = lambda: SHA
        d.review_item = lambda item: {"n": "PR11", "title": "review", "body": "", "review": True}
        d.dispatch = lambda lane, *args: ("run-1", None)
        lanes = {"M21": {"host": "box", "backend": "pi"}}
        d.dispatch_reviews(lanes, {"M21": {"lane": "idle"}}, {}, [])
        self.assertEqual(d.project.values["PVTI_101"], "M21")
        self.assertNotIn("101", d.project.assignees)                    # a review does not reassign

    def test_dry_run_only_plans_the_writes(self):
        d = self.dispatcher(dry_run=True)
        state = {}
        self.dispatch_one(d, state, "M21")
        self.assertEqual(d.project.calls, [])
        planned = " ".join(str(e) for e in d.plan)
        self.assertIn("--add-assignee", planned)
        self.assertIn("item-edit", planned)


if __name__ == "__main__":
    unittest.main()
