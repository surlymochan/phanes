"""Dispatcher side of GitHub-only external lanes (bin/openswarm_external.py) + the SDK test-suite under CI."""
import argparse
import os
import sys
import tempfile
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SDK = os.path.join(ROOT, "sdk", "external-lane")
sys.path[:0] = [os.path.join(ROOT, "bin"), SDK, os.path.join(SDK, "tests")]
import openswarm_external as X  # noqa: E402
from external_lane import contract as C  # noqa: E402
from fakes import issue  # noqa: E402

NOW = 1_800_000_000
H = 3600


def iso(t):
    import time
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def cfg(**kw):
    raw = {"repo": "o/r", "max_inflight": 2, "auto_assign": True, "branch_prefixes": ["fleet/chatgpt-"],
           "assign_exclude_hot": ["openswarm/bin/openswarm"], "assign_from_backends": ["codex", "pi"]}
    raw.update(kw)
    return X.backend_configs({"external_backends": {"_doc": "x", "chatgpt-agent": raw}})["chatgpt-agent"]


def pr(num, head, body="", sha="s1", updated=NOW):
    return {"number": num, "head": {"ref": head, "sha": sha}, "body": body, "updated_at": iso(updated)}


READY_BODY = "实现 x\n\n验收：`python3 -m pytest -q`"
FAT = [{"filename": "tools/a.py", "additions": 40}]


class ConfigTest(unittest.TestCase):
    def test_defaults_and_validation(self):
        c = cfg()
        self.assertEqual(c["label"], "backend:chatgpt-agent")
        self.assertEqual(c["claim_timeout_s"], 6 * H)
        self.assertEqual(c["branch_prefixes"], ["fleet/chatgpt-", "fleet/chatgpt-agent-"])
        for bad in ({"repo": "nope"}, {"max_inflight": -1}, {"claim_timeout_s": "6h"}):
            with self.assertRaises(X.ExternalError):
                cfg(**bad)
        with self.assertRaises(ValueError):
            X.backend_configs({"external_backends": {"Bad Name": {"repo": "o/r"}}})

    def test_reserved_labels_for_fleet_dispatchers(self):
        cfgs = X.backend_configs({"external_backends": {"dot": {"repo": "o/r"}, "off": {"repo": "o/r", "enabled": False}}})
        self.assertEqual(X.reserved_labels(cfgs), ["backend:dot", "backend:external", "lane-claimed"])


class AssignTest(unittest.TestCase):
    def test_refills_to_max_inflight_with_eligible_cards_only(self):
        ready = [issue(1, ["lane-ready", "p1"], "热文件：openswarm/bin/openswarm\n" + READY_BODY),  # hot
                 issue(2, ["lane-ready", "p2", "backend:agy"], READY_BODY),                       # pinned elsewhere
                 issue(3, ["lane-ready", "p1", "in-progress"], READY_BODY),                       # fleet owns it
                 issue(4, ["lane-ready", "p1"], "重基PR：#9 fleet/M1-issue4-x\n" + READY_BODY),    # rework
                 issue(5, ["lane-ready", "p1"], "no acceptance line"),
                 issue(6, ["lane-ready", "p2", "backend:codex"], READY_BODY),
                 issue(7, ["lane-ready", "p1"], READY_BODY),
                 issue(8, ["lane-ready", "p0"], READY_BODY)]
        own = [issue(20, ["lane-pr", "backend:chatgpt-agent"])]  # PR stage does not count against max_inflight
        acts = X.plan(cfg(), own, ready, [], lambda n: FAT, {}, NOW)
        assigned = [a["n"] for a in acts if a["op"] == "labels"]
        self.assertEqual(assigned, [8, 7])
        self.assertIn("backend:chatgpt-agent", acts[0]["labels"])
        six = X.plan(cfg(max_inflight=3), own, ready, [], lambda n: FAT, {}, NOW)
        self.assertEqual([a["n"] for a in six], [8, 7, 6])
        self.assertNotIn("backend:codex", six[2]["labels"])
        self.assertIn("was backend:codex", six[2]["comment"])

    def test_no_assign_when_full_or_disabled(self):
        own = [issue(1, ["lane-ready", "backend:chatgpt-agent"]), issue(2, ["lane-claimed", "backend:chatgpt-agent"], updated_at=iso(NOW))]
        ready = [issue(7, ["lane-ready"], READY_BODY)]
        self.assertEqual(X.plan(cfg(), own, ready, [], lambda n: FAT, {}, NOW), [])
        self.assertEqual(X.plan(cfg(auto_assign=False), [], ready, [], lambda n: FAT, {}, NOW), [])


class PullRequestTest(unittest.TestCase):
    def test_links_pr_by_closes_and_posts_contract_status_once_per_head(self):
        own = [issue(7, ["lane-claimed", "backend:chatgpt-agent"], updated_at=iso(NOW))]
        prs = [pr(70, "fleet/chatgpt-issue7-20261102-0200", "Closes #7"), pr(71, "fleet/M5-issue7-x", "Closes #7")]
        mem = {}
        acts = X.plan(cfg(auto_assign=False), own, [], prs, lambda n: FAT, mem, NOW)
        self.assertEqual(acts[0], {"op": "status", "sha": "s1", "pr": 70, "state": "success", "desc": "contract ok (issue #7)"})
        self.assertEqual(acts[1]["labels"], ["backend:chatgpt-agent", "lane-pr"])
        self.assertEqual(mem["prs"]["70"]["issue"], 7)  # tracked by PR number
        self.assertNotIn("71", mem["prs"])               # fleet lane PRs are not ours
        again = X.plan(cfg(auto_assign=False), [], [], prs, lambda n: FAT, mem, NOW)
        self.assertEqual(again, [])                       # same head sha -> no duplicate status

    def test_thin_pr_gets_failing_status(self):
        acts = X.plan(cfg(auto_assign=False), [], [], [pr(70, "fleet/chatgpt-agent-issue7-1", "Closes #7")],
                      lambda n: [{"filename": "a.py", "additions": 4}], {}, NOW)
        self.assertEqual(acts[0]["state"], "failure")
        self.assertIn("thin", acts[0]["desc"])

    def test_pr_without_closes_uses_branch_or_fails(self):
        acts = X.plan(cfg(auto_assign=False), [], [], [pr(70, "fleet/chatgpt-agent-issue7-20261102-010203")], lambda n: FAT, {}, NOW)
        self.assertEqual(acts[0]["state"], "success")
        acts = X.plan(cfg(auto_assign=False), [], [], [pr(71, "fleet/chatgpt-fix-things")], lambda n: FAT, {}, NOW)
        self.assertEqual(acts[0]["state"], "failure")
        self.assertIn("Closes", acts[0]["desc"])

    def test_rework_claim_is_not_relabelled_by_its_own_pr(self):
        own = [issue(7, ["lane-claimed", "backend:chatgpt-agent"], "重基PR：#70 fleet/chatgpt-issue7-1", updated_at=iso(NOW))]
        acts = X.plan(cfg(auto_assign=False), own, [], [pr(70, "fleet/chatgpt-issue7-1", "Closes #7")], lambda n: FAT, {}, NOW)
        self.assertEqual([a["op"] for a in acts], ["status"])


class StaleClaimTest(unittest.TestCase):
    def test_requeue_then_give_up_without_flapping(self):
        mem = {}
        stale = [issue(7, ["lane-claimed", "backend:chatgpt-agent", "p1"], updated_at=iso(NOW - 7 * H))]
        c = cfg(auto_assign=False)
        self.assertEqual(X.plan(c, [issue(7, ["lane-claimed", "backend:chatgpt-agent"], updated_at=iso(NOW - 5 * H))],
                                [], [], lambda n: FAT, mem, NOW), [])
        for k in (1, 2):
            a = X.plan(c, stale, [], [], lambda n: FAT, mem, NOW)[0]
            self.assertEqual(a["labels"], ["backend:chatgpt-agent", "lane-ready", "p1"])
            self.assertIn(f"requeue {k}/2", a["comment"])
        a = X.plan(c, stale, [], [], lambda n: FAT, mem, NOW)[0]
        self.assertEqual(a["labels"], ["backend:chatgpt-agent", "lane-failed", "needs-human", "p1"])

    def test_recent_pr_push_keeps_claim_alive(self):
        own = [issue(7, ["lane-claimed", "backend:chatgpt-agent"], "重基PR：#70 fleet/chatgpt-issue7-1", updated_at=iso(NOW - 9 * H))]
        prs = [pr(70, "fleet/chatgpt-issue7-1", "Closes #7", updated=NOW - H)]
        acts = X.plan(cfg(auto_assign=False), own, [], prs, lambda n: FAT, {}, NOW)
        self.assertEqual([a["op"] for a in acts], ["status"])


class TickTest(unittest.TestCase):
    def host(self):
        calls = []
        pages = {"repos/o/r/issues?state=open&labels=backend%3Achatgpt-agent": [],
                 "repos/o/r/issues?state=open&labels=lane-ready": [issue(8, ["lane-ready", "p1"], READY_BODY)],
                 "repos/o/r/pulls?state=open": []}
        return calls, types.SimpleNamespace(DATA=tempfile.mkdtemp(), gh_pages=lambda e: pages[e],
                                            gh_transport=lambda e, m="GET", p=None, allow_not_found=False: calls.append((m, e, p)))

    def test_dry_run_writes_nothing_and_real_run_relabels(self):
        calls, host = self.host()
        out = X.tick(host, cfg(), dry_run=True, now=NOW)
        self.assertEqual([a["n"] for a in out["actions"]], [8])
        self.assertEqual(calls, [])
        X.tick(host, cfg(), now=NOW)
        self.assertEqual([c[:2] for c in calls], [("PUT", "repos/o/r/issues/8/labels"), ("POST", "repos/o/r/issues/8/comments")])
        self.assertTrue(os.path.exists(X.memory_path(host, "chatgpt-agent")))

    def test_cli_reserved(self):
        args = argparse.Namespace(external_cmd="reserved", name=None)
        out = X.cmd_external(args, None, {"external_backends": {"dot": {"repo": "o/r"}}})
        self.assertEqual(out["skip_labels"], ["backend:dot", "backend:external", "lane-claimed"])
        self.assertEqual(C.CLAIMED, "lane-claimed")


class SelfRegistrationTest(unittest.TestCase):
    POLICY = {"external_backends": {"_defaults": {"repo": "o/r", "assign_exclude_hot": ["openswarm/bin/openswarm"]},
                                    "chatgpt-agent": {"max_inflight": 2}}}

    def test_discovery_from_labels_and_claims(self):
        labels = [{"name": n} for n in ("backend:pi", "backend:codex", "backend:external", "backend:newbot", "p1")]
        claimed = [issue(3, ["lane-claimed", "backend:fresh"])]
        names, claim_only = X.discover(labels, claimed, C.INTERNAL_BACKENDS)
        self.assertEqual((names, claim_only), ({"newbot", "fresh"}, {"fresh"}))
        cfgs = X.backend_configs(self.POLICY, names)
        self.assertTrue(cfgs["chatgpt-agent"]["allowlisted"])
        self.assertFalse(cfgs["newbot"]["allowlisted"])
        self.assertEqual(cfgs["newbot"]["max_inflight"], 1)  # dynamic names get the defaults
        self.assertEqual(cfgs["newbot"]["branch_prefixes"], ["fleet/newbot-"])

    def test_unproven_name_gets_no_refill_until_it_claims(self):
        cfg = X.backend_configs(self.POLICY, {"typo-bot"})["typo-bot"]
        ready = [issue(7, ["lane-ready", "p1"], READY_BODY)]
        mem = {}
        self.assertEqual(X.plan(cfg, [], ready, [], lambda n: FAT, mem, NOW), [])
        own = [issue(4, ["lane-pr", "backend:typo-bot"])]  # it claimed a pool card and opened a PR
        acts = X.plan(cfg, own, ready, [], lambda n: FAT, mem, NOW)
        self.assertTrue(mem["claimed_before"])
        self.assertEqual([a["n"] for a in acts], [7])
        self.assertEqual(X.plan(cfg, [], ready, [], lambda n: FAT, mem, NOW)[0]["n"], 7)  # remembered

    def test_pool_keeps_starter_cards_and_dispatcher_needs_repo(self):
        pool = X.pool_config(self.POLICY)
        self.assertEqual((pool["label"], pool["max_inflight"]), ("backend:external", 1))
        ready = [issue(7, ["lane-ready", "p1"], READY_BODY), issue(8, ["lane-ready", "p1"], READY_BODY)]
        acts = X.plan(pool, [], ready, [], lambda n: FAT, {}, NOW)
        self.assertEqual([(a["n"], "backend:external" in a["labels"]) for a in acts], [(7, True)])
        self.assertEqual(X.plan(pool, [issue(7, ["lane-ready", "backend:external"])], ready, [], lambda n: FAT, {}, NOW), [])
        with self.assertRaises(X.ExternalError):
            X.tick_all(None, {"external_backends": {}})

    def test_internal_names_cannot_be_external(self):
        with self.assertRaises(X.ExternalError):
            X.backend_configs({"external_backends": {"_defaults": {"repo": "o/r"}, "pi": {}}})
        for name in ("pi", "codex", "agy", "copilot", "external"):
            with self.assertRaises(ValueError):
                C.check_agent(name)

    def test_fleet_skip_rule_covers_dynamic_names(self):
        self.assertTrue(C.fleet_must_skip({"lane-ready", "backend:anything-new"}))
        self.assertTrue(C.fleet_must_skip({"lane-ready", "backend:external"}))
        self.assertTrue(C.fleet_must_skip({"lane-claimed"}))
        self.assertFalse(C.fleet_must_skip({"lane-ready", "backend:codex", "p1"}))
        self.assertFalse(C.fleet_must_skip({"lane-ready"}))

    def test_tick_all_creates_label_for_claim_only_name_and_shares_ready_set(self):
        calls = []
        ready = [issue(7, ["lane-ready", "p1"], READY_BODY), issue(8, ["lane-ready", "p2"], READY_BODY)]
        pages = {"repos/o/r/labels": [{"name": "backend:chatgpt-agent"}],
                 "repos/o/r/issues?state=open&labels=lane-claimed": [issue(3, ["lane-claimed", "backend:fresh"], updated_at=iso(NOW))],
                 "repos/o/r/issues?state=open&labels=backend%3Achatgpt-agent": [],
                 "repos/o/r/issues?state=open&labels=backend%3Afresh": [issue(3, ["lane-claimed", "backend:fresh"], updated_at=iso(NOW))],
                 "repos/o/r/issues?state=open&labels=backend%3Aexternal": [],
                 "repos/o/r/issues?state=open&labels=lane-ready": ready, "repos/o/r/pulls?state=open": []}
        host = types.SimpleNamespace(DATA=tempfile.mkdtemp(), gh_pages=lambda e: pages[e],
                                     gh_transport=lambda e, m="GET", p=None, allow_not_found=False: calls.append((m, e, p)))
        out = X.tick_all(host, self.POLICY, now=NOW)
        self.assertEqual(calls[0][:2], ("POST", "repos/o/r/labels"))
        self.assertEqual(calls[0][2]["name"], "backend:fresh")
        assigned = {r["backend"]: [a["n"] for a in r.get("actions", [])] for r in out if "actions" in r}
        self.assertEqual(assigned, {"chatgpt-agent": [7, 8], "fresh": [], "external": []})  # each card goes to one place


class InboxTest(unittest.TestCase):
    def host(self, existing_pr=False, branch=False):
        calls = []

        def transport(e, m="GET", p=None, allow_not_found=False):
            calls.append((m, e, p))
            if e.endswith("/git/ref/heads/external-inbox/dot"):
                return {"ref": "x"} if branch else None
            if e.endswith("/labels/external-inbox"):
                return None
            if e.endswith("/commits/main"):
                return {"sha": "m1", "commit": {"tree": {"sha": "t1"}}}
            if e.endswith("/git/commits"):
                return {"sha": "e1"}
            if e.endswith("/pulls") and m == "POST":
                return {"number": 1500}
            return {}
        pages = lambda e: [{"number": 1499}] if existing_pr else []
        return calls, types.SimpleNamespace(gh_pages=pages, gh_transport=transport)

    def test_creates_draft_inbox_with_empty_commit_and_label(self):
        calls, host = self.host()
        self.assertEqual(X.ensure_inbox(host, "o/r", "dot"), (1500, True))
        commit = next(p for m, e, p in calls if e.endswith("/git/commits"))
        self.assertEqual((commit["tree"], commit["parents"]), ("t1", ["m1"]))  # same tree as main = empty commit
        pr = next(p for m, e, p in calls if e.endswith("/pulls") and m == "POST")
        self.assertTrue(pr["draft"])
        self.assertEqual(pr["head"], "external-inbox/dot")
        self.assertIn(("POST", "repos/o/r/issues/1500/labels", {"labels": ["external-inbox"]}), calls)

    def test_idempotent(self):
        calls, host = self.host(existing_pr=True)
        self.assertEqual(X.ensure_inbox(host, "o/r", "dot"), (1499, False))
        self.assertEqual(calls, [])
        calls, host = self.host(branch=True)
        X.ensure_inbox(host, "o/r", "dot")
        self.assertFalse(any(e.endswith("/git/commits") for _, e, _ in calls))  # branch reused

    def test_inbox_prs_are_not_lane_prs(self):
        cfg_ = cfg(auto_assign=False, branch_prefixes=[])
        acts = X.plan(cfg_, [], [], [pr(1500, "external-inbox/chatgpt-agent")], lambda n: [], {}, NOW)
        self.assertEqual(acts, [])  # no contract status, no linking
        self.assertFalse(any(C.inbox_branch("chatgpt-agent").startswith(p) for p in ("fleet/", "brain/")))  # AD/merge skip

    def test_workflow_least_privilege(self):
        wf = open(os.path.join(os.path.dirname(ROOT), ".github", "workflows", "external-lane-notify.yml")).read()
        perms = wf.split("permissions:", 1)[1].split("\n\n", 1)[0]
        self.assertIn("issues: read", perms)
        self.assertIn("pull-requests: write", perms)
        self.assertNotIn("issues: write", perms)
        self.assertNotIn("write-all", wf)
        self.assertIn("types: [opened, reopened, labeled]", wf)
        self.assertIn("python3 -m external_lane notify", wf)


def load_tests(loader, tests, pattern):
    """Also run sdk/external-lane/tests in the OpenSwarm CI job. Modules are loaded by path: a nested
    loader.discover() would clobber the outer discovery's top-level dir on Python < 3.12."""
    import importlib.util
    for fn in sorted(os.listdir(os.path.join(SDK, "tests"))):
        if fn.startswith("test_") and fn.endswith(".py"):
            spec = importlib.util.spec_from_file_location(f"external_lane_sdk_{fn[:-3]}", os.path.join(SDK, "tests", fn))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            tests.addTests(loader.loadTestsFromModule(mod))
    return tests


if __name__ == "__main__":
    unittest.main()
