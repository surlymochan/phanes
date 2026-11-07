"""Tests for the lane restart recovery: a restarted lane resumes the run it had in flight (Refs #1720)."""
import importlib.util
import json
import os
import pathlib
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
CLI = ROOT / "bin" / "openswarm"
FIXTURES = ROOT / "tests" / "fixtures"

sys.path.insert(0, str(HERE))
import _load  # noqa: E402

xs = _load.load()
xs.__path__ = [str(ROOT)]
# Other suites patch the CLI through ``sys.modules["openswarm"]``; a second
# registration here would point those patches at this module instead.
if not hasattr(sys.modules.get("openswarm"), "lane_config"):
    sys.modules["openswarm"] = xs

RESUME_PATH = ROOT / "lib" / "lane" / "resume.py"
_resume_spec = importlib.util.spec_from_file_location("lane_resume", RESUME_PATH)
resume_mod = importlib.util.module_from_spec(_resume_spec)
_resume_spec.loader.exec_module(resume_mod)

CLI_BOOTSTRAP = (
    "import importlib.machinery, importlib.util;"
    "loader = importlib.machinery.SourceFileLoader('openswarm_cli', {path!r});"
    "spec = importlib.util.spec_from_loader('openswarm_cli', loader);"
    "module = importlib.util.module_from_spec(spec);"
    "loader.exec_module(module);"
    "module.main()"
)


def process_alive(pid):
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except (OSError, TypeError, ValueError):
        return False
    return True


class ResumeDecisionTests(unittest.TestCase):
    """The pure decision logic of lib/lane/resume.py."""

    def record(self, **fields):
        record = {"run_id": "run-1", "lane": "L1", "branch": "openswarm/run-1", "workspace": "/tmp/wt"}
        record.update(fields)
        return record

    def ad(self, status="in_progress", run="run-1", issue=1720, reason=""):
        return {"status": status, "run": run, "issue": issue, "reason": reason}

    def test_a_run_with_a_pushed_commit_resumes_from_the_branch_tip(self):
        workspace = {"exists": True, "dirty": False, "tip": "b" * 40, "base": "a" * 40,
                     "ahead": True, "pushed_point": True, "head_branch": "openswarm/run-1"}
        decision = resume_mod.plan_recovery(self.record(), self.ad(), {}, workspace)
        self.assertEqual(decision["action"], resume_mod.ACTION_RESUME)
        self.assertEqual(decision["tier"], resume_mod.TIER_COMMIT)
        self.assertEqual(decision["point"], "b" * 40)

    def test_a_run_with_only_workspace_changes_resumes_from_the_uncommitted_state(self):
        workspace = {"exists": True, "dirty": True, "tip": "a" * 40, "base": "a" * 40,
                     "ahead": False, "pushed_point": False, "head_branch": "openswarm/run-1"}
        decision = resume_mod.plan_recovery(self.record(), self.ad(), {}, workspace)
        self.assertEqual(decision["action"], resume_mod.ACTION_RESUME)
        self.assertEqual(decision["tier"], resume_mod.TIER_WORKSPACE)
        self.assertEqual(decision["point"], "workspace")
        self.assertIn("uncommitted", decision["reason"])

    def test_a_tip_that_equals_the_base_is_not_a_commit_grade(self):
        """The base is not a new commit: only the workspace state may be recovered."""
        grade = resume_mod.grade_workspace({"tip": "a" * 40, "base": "a" * 40, "ahead": True,
                                             "pushed_point": True, "dirty": False})
        self.assertEqual(grade["action"], resume_mod.ACTION_ABANDON)
        grade = resume_mod.grade_workspace({"tip": "a" * 40, "base": "a" * 40, "ahead": True,
                                             "pushed_point": True, "dirty": True})
        self.assertEqual(grade["action"], resume_mod.ACTION_RESUME)
        self.assertEqual(grade["tier"], resume_mod.TIER_WORKSPACE)

    def test_a_commit_that_was_never_pushed_is_not_a_commit_grade(self):
        grade = resume_mod.grade_workspace({"tip": "b" * 40, "base": "a" * 40, "ahead": True,
                                             "pushed_point": False, "dirty": False})
        self.assertEqual(grade["action"], resume_mod.ACTION_RESUME)
        self.assertEqual(grade["tier"], resume_mod.TIER_WORKSPACE)
        self.assertIn("not pushed", grade["reason"])

    def test_a_dirty_workspace_is_never_graded_as_a_commit(self):
        grade = resume_mod.grade_workspace({"tip": "b" * 40, "base": "a" * 40, "ahead": True,
                                             "pushed_point": True, "dirty": True})
        self.assertEqual(grade["tier"], resume_mod.TIER_WORKSPACE)

    def test_a_workspace_without_a_commit_or_a_change_is_abandoned(self):
        decision = resume_mod.plan_recovery(
            self.record(), self.ad(), {},
            {"exists": True, "dirty": False, "tip": "a" * 40, "base": "a" * 40, "ahead": False,
             "pushed_point": False, "head_branch": "openswarm/run-1"})
        self.assertEqual(decision["action"], resume_mod.ACTION_ABANDON)
        self.assertEqual(decision["tier"], resume_mod.TIER_NONE)
        self.assertTrue(decision["reason"])

    def test_a_run_the_ad_side_no_longer_tracks_is_abandoned(self):
        view = resume_mod.ad_view_from_state({"L1": {"issue": 1720, "run": "run-2"}}, "L1", "run-1")
        decision = resume_mod.plan_recovery(self.record(), view, {}, {"exists": True, "dirty": True})
        self.assertEqual(decision["action"], resume_mod.ACTION_ABANDON)
        self.assertIn("run-2", decision["reason"])

    def test_an_unreadable_ad_state_leaves_the_run_alone(self):
        decision = resume_mod.plan_recovery(self.record(), self.ad(status="unknown"), {},
                                             {"exists": True, "dirty": True})
        self.assertEqual(decision["action"], resume_mod.ACTION_SKIP)
        self.assertIn("in progress", decision["reason"])

    def test_a_finished_run_is_not_resumed(self):
        decision = resume_mod.plan_recovery(self.record(), self.ad(), {"state": "finished"},
                                             {"exists": True, "dirty": True})
        self.assertEqual(decision["action"], resume_mod.ACTION_ABANDON)

    def test_a_workspace_on_another_branch_is_abandoned(self):
        decision = resume_mod.plan_recovery(
            self.record(), self.ad(), {},
            {"exists": True, "dirty": True, "head_branch": "openswarm/other"})
        self.assertEqual(decision["action"], resume_mod.ACTION_ABANDON)
        self.assertIn("openswarm/other", decision["reason"])

    def test_a_missing_workspace_is_abandoned(self):
        decision = resume_mod.plan_recovery(self.record(), self.ad(), {}, {"exists": False})
        self.assertEqual(decision["action"], resume_mod.ACTION_ABANDON)
        self.assertIn("gone", decision["reason"])

    def test_the_ad_view_follows_the_authoritative_state(self):
        state = {"L1": {"issue": 1720, "run": "run-1", "status": "in_progress"}, "L2": {"issue": 1721, "run": "run-2"}}
        view = resume_mod.ad_view_from_state(state, "L1", "run-1")
        self.assertEqual(view["status"], resume_mod.AD_IN_PROGRESS)
        self.assertEqual(view["issue"], 1720)

    def test_the_ad_view_requires_explicit_in_progress_status(self):
        for status in (None, "pending", "running"):
            state = {"L1": {"issue": 1720, "run": "run-1", "status": status}}
            view = resume_mod.ad_view_from_state(state, "L1", "run-1")
            self.assertEqual(view["status"], resume_mod.AD_UNKNOWN)
            self.assertIn("explicitly confirm", view["reason"])

    def test_the_ad_view_rejects_a_lane_that_got_another_run(self):
        state = {"L1": {"issue": 1720, "run": "run-9"}}
        view = resume_mod.ad_view_from_state(state, "L1", "run-1")
        self.assertEqual(view["status"], resume_mod.AD_REASSIGNED)
        self.assertIn("run-9", view["reason"])

    def test_the_ad_view_rejects_an_issue_that_moved_to_another_lane(self):
        state = {"L1": {"issue": 1720, "run": "run-1"}, "L2": {"issue": 1720, "run": "run-2"}}
        view = resume_mod.ad_view_from_state(state, "L1", "run-1")
        self.assertEqual(view["status"], resume_mod.AD_REASSIGNED)
        self.assertIn("L2", view["reason"])

    def test_the_ad_view_rejects_a_finished_issue(self):
        state = {"L1": {"issue": 1720, "run": "run-1"}, "_done": [1720]}
        view = resume_mod.ad_view_from_state(state, "L1", "run-1")
        self.assertEqual(view["status"], resume_mod.AD_DONE)

    def test_the_ad_view_rejects_an_issue_the_entry_calls_finished(self):
        state = {"L1": {"issue": 1720, "run": "run-1", "status": "done"}}
        view = resume_mod.ad_view_from_state(state, "L1", "run-1")
        self.assertEqual(view["status"], resume_mod.AD_DONE)

    def test_the_ad_view_does_not_invent_a_state_for_an_unknown_lane(self):
        view = resume_mod.ad_view_from_state({"L2": {"issue": 1720, "run": "run-2"}}, "L1", "run-1")
        self.assertEqual(view["status"], resume_mod.AD_UNKNOWN)

    def test_the_resume_prompt_names_the_recovery_point_and_the_session(self):
        decision = {"tier": resume_mod.TIER_COMMIT, "point": "b" * 40,
                    "session": {"kind": "pi_session", "id": "session-7"}}
        prompt = resume_mod.resume_prompt({}, decision)
        self.assertIn("b" * 40, prompt)
        self.assertIn("session-7", prompt)


class FakeChild:
    """A lane child that only records what the executor sends it."""

    def __init__(self, reply=None, error=None):
        self.sent = []
        self.reply = reply if reply is not None else {}
        self.error = error

    def send(self, message):
        self.sent.append(message)

    def wait_response(self, req_id, timeout=30, event_file=None):
        if self.error:
            raise RuntimeError(self.error)
        return self.reply


class ResumeSessionTests(unittest.TestCase):
    """The executor restores the session of an interrupted run instead of starting a new one."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="openswarm-lane-session-")
        self.root = pathlib.Path(self.tmp.name)
        self.event_file = self.root / "events.jsonl"
        self.event_file.write_text("")

    def tearDown(self):
        self.tmp.cleanup()

    def events(self):
        return [json.loads(line) for line in self.event_file.read_text().splitlines() if line.strip()]

    def test_pi_switches_back_into_the_interrupted_session(self):
        child = FakeChild(reply={"type": "response", "command": "switch_session", "success": True})
        turn = {}
        restored = xs.lane_restore_session(
            {"name": "L1", "backend": "pi", "model": "m"}, child, "/tmp/wt", str(self.event_file), turn,
            {"kind": "pi_session", "id": "session-7", "path": "/tmp/pi-sessions/s7.jsonl"})
        self.assertTrue(restored)
        self.assertEqual(child.sent[0]["type"], "switch_session")
        self.assertEqual(child.sent[0]["sessionPath"], "/tmp/pi-sessions/s7.jsonl")
        self.assertEqual(turn["session_id"], "session-7")
        self.assertEqual(turn["session_file"], "/tmp/pi-sessions/s7.jsonl")
        self.assertTrue(any(row.get("type") == "session_restored" for row in self.events()))

    def test_pi_falls_back_when_the_session_cannot_be_restored(self):
        child = FakeChild(error="pi RPC switch_session failed: session file not found")
        turn = {}
        restored = xs.lane_restore_session(
            {"name": "L1", "backend": "pi", "model": "m"}, child, "/tmp/wt", str(self.event_file), turn,
            {"kind": "pi_session", "id": "session-7", "path": "/tmp/pi-sessions/gone.jsonl"})
        self.assertFalse(restored)
        self.assertTrue(any(row.get("type") == "session_restore_failed" for row in self.events()))

    def test_codex_resumes_the_interrupted_thread(self):
        child = FakeChild(reply={"thread": {"id": "thread-3"}})
        turn = {}
        restored = xs.lane_restore_session(
            {"name": "L1", "backend": "codex", "model": "m"}, child, "/tmp/wt", str(self.event_file), turn,
            {"kind": "codex_thread", "id": "thread-3"})
        self.assertTrue(restored)
        self.assertEqual(child.sent[0]["method"], "thread/resume")
        self.assertEqual(child.sent[0]["params"]["threadId"], "thread-3")
        self.assertEqual(turn["thread_id"], "thread-3")

    def test_codex_falls_back_when_the_thread_cannot_be_resumed(self):
        child = FakeChild(reply={"error": {"message": "unknown method"}})
        turn = {}
        restored = xs.lane_restore_session(
            {"name": "L1", "backend": "codex", "model": "m"}, child, "/tmp/wt", str(self.event_file), turn,
            {"kind": "codex_thread", "id": "thread-3"})
        self.assertFalse(restored)
        self.assertTrue(any(row.get("type") == "session_restore_failed" for row in self.events()))

    def test_a_run_without_a_session_starts_a_new_one(self):
        child = FakeChild()
        turn = {}
        self.assertFalse(xs.lane_restore_session(
            {"name": "L1", "backend": "pi", "model": "m"}, child, "/tmp/wt", str(self.event_file), turn, {}))
        self.assertEqual(child.sent, [])

    def test_the_turn_session_names_the_session_of_the_backend(self):
        self.assertEqual(xs.lane_turn_session({"backend": "codex"}, {"thread_id": "t1"}),
                         {"kind": "codex_thread", "id": "t1"})
        self.assertEqual(xs.lane_turn_session({"backend": "pi"}, {"session_id": "s1", "session_file": "/f"}),
                         {"kind": "pi_session", "id": "s1", "path": "/f"})


class ResumeWorkspaceTests(unittest.TestCase):
    """The executor side: the workspace probe and the workspace preparation."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="openswarm-lane-resume-")
        self.root = pathlib.Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "Lane Test")
        self.git("config", "user.email", "lane-test@example.invalid")
        (self.repo / "README.md").write_text("base\n")
        self.git("add", "README.md")
        self.git("commit", "-q", "-m", "base")
        self.base = self.git("rev-parse", "HEAD")
        self.constants = {key: getattr(xs, key) for key in ("RUNS", "NOW", "DATA")}
        xs.RUNS = str(self.root / "runs")
        xs.NOW = lambda: 1_800_000_000

    def tearDown(self):
        for key, value in self.constants.items():
            setattr(xs, key, value)
        self.tmp.cleanup()

    def git(self, *args, cwd=None):
        return subprocess.run(["git", *args], cwd=str(cwd or self.repo), check=True,
                              capture_output=True, text=True).stdout.strip()

    def lane(self):
        return {"name": "L1", "backend": "pi", "model": "fake-model", "repo": str(self.repo)}

    def worktree(self):
        wt = self.root / "wt"
        if not wt.exists():
            self.git("worktree", "add", "--detach", "-q", str(wt), self.base)
        return wt

    def test_the_probe_does_not_call_the_base_a_new_commit(self):
        wt = self.worktree()
        self.git("checkout", "-q", "-B", "openswarm/run-1", self.base, cwd=wt)
        probe = xs.lane_resume_probe({"workspace": str(wt), "branch": "openswarm/run-1", "base": self.base})
        self.assertEqual(probe["tip"], self.base)
        self.assertFalse(probe["ahead"])
        self.assertFalse(probe["pushed_point"])

    def test_the_probe_does_not_mark_a_commit_pushed_without_a_remote_ref(self):
        wt = self.worktree()
        self.git("checkout", "-q", "-B", "openswarm/run-1", self.base, cwd=wt)
        (wt / "work.txt").write_text("done\n")
        self.git("add", "work.txt", cwd=wt)
        self.git("commit", "-q", "-m", "work", cwd=wt)
        probe = xs.lane_resume_probe({"workspace": str(wt), "branch": "openswarm/run-1", "base": self.base})
        self.assertNotEqual(probe["tip"], self.base)
        self.assertTrue(probe["ahead"])
        self.assertFalse(probe["pushed_point"])

    def test_the_probe_rejects_a_commit_the_remote_does_not_carry(self):
        wt = self.worktree()
        self.git("checkout", "-q", "-B", "openswarm/run-1", self.base, cwd=wt)
        (wt / "work.txt").write_text("done\n")
        self.git("add", "work.txt", cwd=wt)
        self.git("commit", "-q", "-m", "work", cwd=wt)
        tip = self.git("rev-parse", "HEAD", cwd=wt)
        self.git("update-ref", f"refs/remotes/origin/openswarm/run-1", self.base, cwd=wt)
        probe = xs.lane_resume_probe({"workspace": str(wt), "branch": "openswarm/run-1", "base": self.base})
        self.assertTrue(probe["ahead"])
        self.assertEqual(probe["pushed"], self.base)
        self.assertFalse(probe["pushed_point"])
        self.git("update-ref", f"refs/remotes/origin/openswarm/run-1", tip, cwd=wt)
        probe = xs.lane_resume_probe({"workspace": str(wt), "branch": "openswarm/run-1", "base": self.base})
        self.assertTrue(probe["pushed_point"])

    def test_the_workspace_grade_keeps_the_uncommitted_files(self):
        wt = self.worktree()
        self.git("checkout", "-q", "-B", "openswarm/run-1", self.base, cwd=wt)
        (wt / "partial.txt").write_text("half done\n")
        with mock.patch.object(xs, "lane_root", return_value=str(self.root)):
            workspace = xs.lane_prepare_workspace(self.lane(), "run-1", self.base,
                                                   resume={"tier": "workspace", "point": "workspace"})
        self.assertTrue((wt / "partial.txt").exists())
        self.assertEqual((wt / "partial.txt").read_text(), "half done\n")
        self.assertTrue(workspace["base_dirty"])

    def test_the_commit_grade_resets_the_workspace_to_the_recovery_point(self):
        wt = self.worktree()
        self.git("checkout", "-q", "-B", "openswarm/run-1", self.base, cwd=wt)
        (wt / "work.txt").write_text("done\n")
        self.git("add", "work.txt", cwd=wt)
        self.git("commit", "-q", "-m", "work", cwd=wt)
        tip = self.git("rev-parse", "HEAD", cwd=wt)
        with mock.patch.object(xs, "lane_root", return_value=str(self.root)):
            xs.lane_prepare_workspace(self.lane(), "run-1", self.base,
                                      resume={"tier": "commit", "point": tip})
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=wt), tip)
        self.assertEqual(self.git("rev-parse", "--abbrev-ref", "HEAD", cwd=wt), "openswarm/run-1")

    def test_the_ad_view_reads_the_configured_ad_state(self):
        state = self.root / "ad-state.json"
        state.write_text(json.dumps({"L1": {"issue": 1720, "run": "run-1", "status": "in_progress"}}))
        with mock.patch.dict(os.environ, {"OPENSWARM_AD_STATE": str(state)}):
            view = xs.lane_ad_run_view("L1", "run-1")
        self.assertEqual(view["status"], resume_mod.AD_IN_PROGRESS)
        self.assertEqual(view["issue"], 1720)

    def test_the_ad_view_reports_an_unreadable_ad_state(self):
        with mock.patch.dict(os.environ, {"OPENSWARM_AD_STATE": str(self.root / "missing.json")}):
            view = xs.lane_ad_run_view("L1", "run-1")
        self.assertEqual(view["status"], resume_mod.AD_UNKNOWN)

    def test_the_ad_view_does_not_trust_the_state_the_lane_left_behind(self):
        with mock.patch.dict(os.environ, {"OPENSWARM_AD_STATE": str(self.root / "missing.json")}):
            view = xs.lane_ad_run_view("L1", "run-1", {"current_run": "run-1"})
        self.assertEqual(view["status"], resume_mod.AD_UNKNOWN)
        self.assertIn("no AD state", view["reason"])

    def test_the_ad_view_gives_up_when_the_lane_does_not_hold_the_run(self):
        with mock.patch.dict(os.environ, {"OPENSWARM_AD_STATE": str(self.root / "missing.json")}):
            view = xs.lane_ad_run_view("L1", "run-1", {"current_run": "other-run"})
        self.assertEqual(view["status"], resume_mod.AD_UNKNOWN)


class LaneRestartRecoveryTests(unittest.TestCase):
    """A restarted lane resumes the run it had in flight instead of waiting for a new card."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="lr-", dir=os.environ.get("TMPDIR"))
        self.root = pathlib.Path(self.tmp.name)
        self.data = self.root / "data"
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "Lane Test")
        self.git("config", "user.email", "lane-test@example.invalid")
        (self.repo / "README.md").write_text("base\n")
        self.git("add", "README.md")
        self.git("commit", "-q", "-m", "base")
        for path in FIXTURES.glob("fake_*py"):
            path.chmod(0o755)
        self.env = dict(os.environ, OPENSWARM_HOME=str(self.data), HOME=str(self.root))
        self.env["FAKE_PI_RPC_LOG"] = str(self.root / "fake-pi-rpc.jsonl")
        self.ad_state = self.root / "ad-state.json"
        self.env["OPENSWARM_AD_STATE"] = str(self.ad_state)
        self.lane_name = "L1"
        (self.data / "lanes").mkdir(parents=True, exist_ok=True)
        (self.data / "lanes.json").write_text(json.dumps({"lanes": [{
            "name": self.lane_name, "backend": "pi", "model": "fake-model",
            "provider": "fake-provider", "repo": str(self.repo),
            "binary": str(FIXTURES / "fake_pi_rpc.py"),
        }]}))

    def tearDown(self):
        try:
            self.cli("lane", "down", self.lane_name, check=False, timeout=45)
        except (OSError, subprocess.TimeoutExpired):
            pass
        self.tmp.cleanup()

    def git(self, *args, cwd=None):
        return subprocess.run(["git", *args], cwd=str(cwd or self.repo), check=True,
                              capture_output=True, text=True).stdout.strip()

    def cli(self, *args, check=True, timeout=20):
        policy_file = self.data / "policy.local.json"
        policy = json.loads(policy_file.read_text()) if policy_file.exists() else {}
        policy.setdefault("pi", {})["require_isolation"] = False
        policy_file.write_text(json.dumps(policy))
        result = subprocess.run([sys.executable, "-c", CLI_BOOTSTRAP.format(path=str(CLI)), *map(str, args)],
                                env=self.env, text=True, capture_output=True, timeout=timeout)
        if check and result.returncode:
            self.fail(f"CLI failed ({result.returncode}): {result.stdout}\n{result.stderr}")
        return result

    def wait_for(self, predicate, timeout=20, description="condition"):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            last = predicate()
            if last:
                return last
            time.sleep(0.05)
        self.fail(f"timed out waiting for {description}; last={last!r}")

    def lane_state(self):
        try:
            return json.loads((self.data / "lanes" / self.lane_name / "state.json").read_text())
        except (OSError, ValueError):
            return {}

    def run_state(self, run_id):
        try:
            return json.loads((self.data / "runs" / run_id / "status.json").read_text())
        except (OSError, ValueError):
            return {}

    def run_record(self, run_id):
        try:
            return json.loads((self.data / "runs" / run_id / "resume.json").read_text())
        except (OSError, ValueError):
            return {}

    def pi_messages(self):
        path = pathlib.Path(self.env["FAKE_PI_RPC_LOG"])
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def start_lane(self):
        result = self.cli("lane", "up", self.lane_name)
        self.assertTrue(json.loads(result.stdout)["lanes"][0]["state"] in ("idle", "busy", "starting"))

    def assign(self, task):
        result = self.cli("lane", "assign", self.lane_name, "--prompt", task)
        return json.loads(result.stdout)["id"]

    def worktree(self):
        return self.data / "lanes" / self.lane_name / "wt"

    def start_inflight_run(self, task="LANE_TEST_WAIT keep the lane busy"):
        """Start a lane, assign a long running task and wait until the turn is in flight."""
        self.start_lane()
        run_id = self.assign(task)
        self.wait_for(lambda: (self.run_state(run_id).get("state") == "running"
                                and self.run_record(run_id).get("session")),
                      description="the run to start with a session")
        return run_id

    def kill_lane(self):
        """Kill the supervisor and its executor: the lane goes down with a run in flight."""
        state = self.lane_state()
        for key in ("child_pid", "supervisor_pid"):
            pid = state.get(key)
            if pid:
                try:
                    os.kill(int(pid), signal.SIGKILL)
                except OSError:
                    pass
        self.wait_for(lambda: not self.lane_state().get("supervisor_pid")
                      or not process_alive(self.lane_state().get("supervisor_pid")),
                      description="the lane supervisor to die")

    def write_ad_state(self, run_id, issue=1720):
        self.ad_state.write_text(json.dumps({self.lane_name: {"issue": issue, "run": run_id,
                                                               "host": "local", "at": "now", "status": "in_progress"}}))

    def test_a_restarted_lane_resumes_the_run_and_does_not_dispatch_it_again(self):
        run_id = self.start_inflight_run("LANE_TEST_WAIT_ALWAYS keep the lane busy")
        session = self.run_record(run_id).get("session") or {}
        self.assertTrue(session.get("id"))
        wt = self.worktree()
        (wt / "partial.txt").write_text("half done\n")
        self.write_ad_state(run_id)
        self.kill_lane()
        self.start_lane()
        # The lane takes the recovered run instead of waiting for a new card.
        self.wait_for(lambda: (s if (s := self.lane_state()).get("current_run") == run_id else None),
                      description="the recovered run to be claimed by the lane")
        self.assertEqual(self.lane_state().get("state"), "busy")
        # The card is not dispatched a second time while the recovered run is in flight.
        again = self.cli("lane", "assign", self.lane_name, "--prompt", "must be refused", check=False)
        self.assertNotEqual(again.returncode, 0)
        self.assertIn("busy", again.stdout + again.stderr)
        # The interrupted session is continued instead of a fresh one.
        messages = self.pi_messages()
        self.assertEqual(sum(row.get("type") == "switch_session" for row in messages), 1)
        self.assertEqual(sum(row.get("type") == "new_session" for row in messages), 1)
        switched = next(row for row in messages if row.get("type") == "switch_session")
        self.assertEqual(switched.get("sessionPath"), session.get("path"))
        # The run finishes and the recovery is written down.
        state = self.wait_for(lambda: (s if (s := self.run_state(run_id)).get("state") == "finished" else None),
                               description="the recovered run to finish")
        self.assertEqual(state.get("status"), "done")
        recovery = state.get("recovery") or {}
        self.assertEqual(recovery.get("tier"), resume_mod.TIER_WORKSPACE)
        events = [json.loads(line) for line in
                  (self.data / "runs" / run_id / "events.1.pi.jsonl").read_text().splitlines()]
        self.assertTrue(any(row.get("type") == "session_restored" for row in events))
        # The brain log names the recovered run, its grade and its recovery point.
        brain = (self.data / "brain" / "log.md").read_text()
        self.assertIn("lane-resume", brain)
        self.assertIn(run_id, brain)
        self.assertIn("tier=workspace", brain)

    def test_a_restarted_lane_resumes_a_run_that_left_a_commit(self):
        run_id = self.start_inflight_run()
        wt = self.worktree()
        self.git("checkout", "-q", "-B", f"openswarm/{run_id}", "HEAD", cwd=wt)
        (wt / "committed.txt").write_text("work in progress\n")
        self.git("add", "committed.txt", cwd=wt)
        self.git("-c", "user.name=Lane Test", "-c", "user.email=lane-test@example.invalid",
                "commit", "-q", "-m", "work in progress", cwd=wt)
        self.git("update-ref", f"refs/remotes/origin/openswarm/{run_id}", "HEAD", cwd=wt)
        self.write_ad_state(run_id)
        self.kill_lane()
        self.start_lane()
        self.wait_for(lambda: (s if (s := self.lane_state()).get("current_run") == run_id else None),
                      description="the recovered run to be claimed by the lane")
        state = self.wait_for(lambda: (s if (s := self.run_state(run_id)).get("state") == "finished" else None),
                               description="the recovered run to finish")
        recovery = state.get("recovery") or {}
        self.assertEqual(recovery.get("tier"), resume_mod.TIER_COMMIT)
        self.assertTrue(recovery.get("point"))

    def test_a_restarted_lane_does_not_resume_without_ad_confirmation(self):
        run_id = self.start_inflight_run()
        (self.worktree() / "partial.txt").write_text("half done\n")
        self.kill_lane()
        self.start_lane()
        self.wait_for(lambda: self.lane_state().get("state") == "idle", description="idle lane")
        self.assertNotEqual(self.lane_state().get("current_run"), run_id)
        brain = (self.data / "brain" / "log.md").read_text()
        self.assertIn("no AD state tracks", brain)
        self.assertEqual(sum(row.get("type") == "new_session" for row in self.pi_messages()), 1)

    def test_a_restarted_lane_abandons_a_run_the_ad_side_re_dispatched(self):
        run_id = self.start_inflight_run()
        # The AD side gave the lane another run in the meantime: the old run is not resumed.
        self.ad_state.write_text(json.dumps({self.lane_name: {"issue": 1720, "run": "another-run"}}))
        self.kill_lane()
        self.start_lane()
        state = self.wait_for(lambda: (s if (s := self.run_state(run_id)).get("state") == "finished" else None),
                               description="the abandoned run to be closed")
        self.assertEqual(state.get("status"), "failed")
        self.assertEqual(state.get("error_class"), "resume_abandoned")
        self.assertIn("another-run", state.get("error") or "")
        self.assertEqual(self.run_record(run_id).get("state"), "abandoned")
        # The lane is idle again, so the card can be dispatched by the AD side.
        self.wait_for(lambda: (s if (s := self.lane_state()).get("state") == "idle" else None),
                      description="the lane to become idle again")

    def test_a_restarted_lane_keeps_the_uncommitted_workspace_of_the_run(self):
        run_id = self.start_inflight_run()
        wt = self.worktree()
        (wt / "partial.txt").write_text("half done\n")
        self.write_ad_state(run_id)
        self.kill_lane()
        self.start_lane()
        self.wait_for(lambda: (s if (s := self.lane_state()).get("current_run") == run_id else None),
                      description="the recovered run to be claimed by the lane")
        # The uncommitted work of the interrupted run is still there.
        self.assertTrue((wt / "partial.txt").exists())
        state = self.wait_for(lambda: (s if (s := self.run_state(run_id)).get("state") == "finished" else None),
                               description="the recovered run to finish")
        recovery = state.get("recovery") or {}
        self.assertEqual(recovery.get("tier"), resume_mod.TIER_WORKSPACE)
        self.assertIn("uncommitted", recovery.get("note") or "")


CLI_BOOTSTRAP = (
    "import importlib.machinery, importlib.util;"
    "loader = importlib.machinery.SourceFileLoader('openswarm_cli', {path!r});"
    "spec = importlib.util.spec_from_loader('openswarm_cli', loader);"
    "module = importlib.util.module_from_spec(spec);"
    "loader.exec_module(module);"
    "module.main()"
)


def process_alive(pid):
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except (OSError, TypeError, ValueError):
        return False
    return True
