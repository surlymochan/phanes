"""收割塌缩护栏（Refs #1833）：fix 后 head 必须包含 fix 前 head，且文件数/改动行数不得塌缩。

fix/rebase 轮若不在 PR 当前 head 上续改、只产出增量补丁，AD 把该提交推回 PR 分支就会把整个 PR
替换成一个补丁（2026-11-03 实测 6 个 PR 被 builder 强推还原）。收割推送前的两道检查：

①新 head 必须以 fix 前 PR head 为祖先（git merge-base --is-ancestor），否则视作整体替换；
②新 head 相对 base 的变更文件数**或**总改动行数，任一项相对 fix 前 head 下降超过阈值即拒绝。

本文件测三件事：判定规则的每一条、2026-11-03 六个塌缩 PR 的回放、以及收割路径上被拒绝时
PR 分支确实没有被改动（不推送、不建 PR、不消耗 review 轮次）。
"""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[2] / "tools" / "lane-autodispatch" / "autodispatch.py"
SPEC = importlib.util.spec_from_file_location("autodispatch_collapse_guard", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

OLD = "a" * 40
NEW = "b" * 40
BASE = "c" * 40

# 2026-11-03 被 builder 强推还原的 6 个 PR：fix 前体量 → fix 后体量（文件数/改动行数）。
# 前后 sha 见盒子上的 gocc:~/phanes-ctl/autodispatch/bak/restore-collapsed-heads-20261103.json。
COLLAPSED_PRS = [
    ("#1807", 8, 1780, 1, 11),
    ("#1804", 6, 1401, 3, 39),
    ("#1715", 8, 868, 2, 23),
    ("#1755", 8, 746, 1, 19),
    ("#1653", 6, 200, 2, 19),
    ("#1824", 9, 835, 1, 17),
]


class FakeGit:
    """Answers the git commands the guard runs; records every call."""

    def __init__(self, ancestor_rc=0, sizes=None, diff_rc=0, remote_head=OLD, local_head=NEW):
        self.ancestor_rc = ancestor_rc          # 0 = 祖先成立, 1 = 不是祖先, 其它 = git 失败
        self.sizes = sizes or {}                 # (base, rev) -> (files, lines)
        self.diff_rc = diff_rc
        self.remote_head = remote_head
        self.local_head = local_head
        self.calls = []

    def __call__(self, cmd, timeout=120, inp=None):
        argv = cmd if isinstance(cmd, list) else [cmd]
        joined = " ".join(argv)
        self.calls.append(joined)
        if "merge-base" in joined: return (self.ancestor_rc, "", "")
        if "--numstat" in joined:
            if self.diff_rc: return (self.diff_rc, "", "boom")
            for (base, rev), size in self.sizes.items():
                if base in joined and rev in joined:
                    return (0, numstat(*size), "")
            return (0, "", "")
        if "ls-remote" in joined: return (0, f"{self.remote_head}\trefs/heads/x\n", "")
        if "rev-parse" in joined: return (0, f"{self.local_head}\n", "")
        if "cat-file" in joined: return (0, "", "")
        return (0, "", "")

    def sizes_for(self, before, after):
        return {("base", "old"): before, ("base", "new"): after}


def numstat(files, lines):
    """A `git diff --numstat` output with exactly `files` changed files and `lines` changed lines."""
    rows = [f"0\t0\tfile{i}.py\n" for i in range(files)]
    if rows: rows[0] = f"0\t{lines}\tfile0.py\n"
    return "".join(rows)


class CollapseGuardDecisionTests(unittest.TestCase):
    """判定规则：祖先关系 + 文件数/改动行数，任一项塌缩即拒绝。"""

    def guard(self, git, body=None, ratio=None):
        self.git = git
        original = MODULE.sh
        MODULE.sh = git
        try:
            return MODULE.harvest_guard("/repo", "base", "old", "new", body=body, ratio=ratio)
        finally:
            MODULE.sh = original

    def test_file_count_collapse_only_is_rejected(self):
        # 文件数 8 → 1（降 87.5%），行数不变：只降文件数也必须拒绝
        git = FakeGit(sizes=FakeGit().sizes_for((8, 32), (1, 32)))
        ok, reason, detail = self.guard(git)
        self.assertFalse(ok, "只有文件数下降超过阈值时也必须拒绝")
        self.assertIn("文件数", reason)
        self.assertEqual(detail["files_before"], 8)
        self.assertEqual(detail["files_after"], 1)
        self.assertEqual(detail["lines_before"], 32)
        self.assertEqual(detail["lines_after"], 32)

    def test_line_count_collapse_only_is_rejected(self):
        # 文件数不变（4 → 4），行数 400 → 20（降 95%）：只降行数也必须拒绝
        git = FakeGit(sizes=FakeGit().sizes_for((4, 400), (4, 20)))
        ok, reason, detail = self.guard(git)
        self.assertFalse(ok, "只有改动行数下降超过阈值时也必须拒绝")
        self.assertIn("行数", reason)
        self.assertEqual(detail["files_before"], 4)
        self.assertEqual(detail["files_after"], 4)
        self.assertEqual(detail["lines_before"], 400)
        self.assertEqual(detail["lines_after"], 20)

    def test_ancestor_and_healthy_size_passes(self):
        git = FakeGit(sizes=FakeGit().sizes_for((2, 40), (3, 60)))
        ok, reason, _ = self.guard(git)
        self.assertTrue(ok, "祖先成立且体量没有塌缩时必须放行")

    def test_non_ancestor_is_rejected(self):
        git = FakeGit(ancestor_rc=1, sizes=FakeGit().sizes_for((2, 40), (3, 60)))
        ok, reason, detail = self.guard(git)
        self.assertFalse(ok, "新 head 不是 fix 前 head 的祖先时必须拒绝")
        self.assertIn("祖先", reason)
        self.assertIs(detail["ancestor"], False)

    def test_ancestor_ok_but_lines_drop_more_than_half_is_rejected(self):
        git = FakeGit(sizes=FakeGit().sizes_for((3, 400), (3, 20)))
        ok, reason, _ = self.guard(git)
        self.assertFalse(ok, "祖先成立但行数下降超过 50% 时必须拒绝")
        self.assertIn("行数", reason)

    def test_rewrite_marker_exempts_the_guard(self):
        git = FakeGit(ancestor_rc=1, sizes=FakeGit().sizes_for((8, 1780), (1, 11)))
        ok, reason, _ = self.guard(git, body="本卡显式声明：整体重写（不保留旧 head）")
        self.assertTrue(ok, "声明整体重写的卡豁免塌缩护栏")
        self.assertIn("豁免", reason)

    def test_ancestor_check_failure_fails_closed(self):
        git = FakeGit(ancestor_rc=128)
        ok, reason, detail = self.guard(git)
        self.assertFalse(ok, "git 命令失败时不得放行（fail closed）")
        self.assertIn("fail closed", reason)
        self.assertIsNone(detail["ancestor"])

    def test_size_check_failure_fails_closed(self):
        git = FakeGit(diff_rc=1)
        ok, reason, _ = self.guard(git)
        self.assertFalse(ok, "体量统计失败时不得放行（fail closed）")
        self.assertIn("fail closed", reason)

    def test_ratio_is_configurable_by_env(self):
        git = FakeGit(sizes=FakeGit().sizes_for((10, 100), (7, 70)))
        os.environ[MODULE.HARVEST_COLLAPSE_ENV] = "0.2"
        self.addCleanup(os.environ.pop, MODULE.HARVEST_COLLAPSE_ENV, None)
        ok, reason, _ = self.guard(git)
        self.assertFalse(ok, "阈值 20% 时下降 30% 必须拒绝")
        self.assertIn("阈值 20%", reason)

    def test_ratio_defaults_to_half(self):
        os.environ.pop(MODULE.HARVEST_COLLAPSE_ENV, None)
        self.assertEqual(MODULE.harvest_collapse_ratio(), 0.5)
        os.environ[MODULE.HARVEST_COLLAPSE_ENV] = "not-a-number"
        self.addCleanup(os.environ.pop, MODULE.HARVEST_COLLAPSE_ENV, None)
        self.assertEqual(MODULE.harvest_collapse_ratio(), 0.5)


def git_run(repo, *args):
    proc = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def make_repo(path):
    os.makedirs(path, exist_ok=True)
    git_run(path, "init", "-q")
    git_run(path, "config", "user.email", "lane@example.com")
    git_run(path, "config", "user.name", "lane")
    Path(path, "base.txt").write_text("base\n", encoding="utf-8")
    git_run(path, "add", "-A")
    git_run(path, "commit", "-qm", "base")
    _, base, _ = git_run(path, "rev-parse", "HEAD")
    return base


def commit_files(repo, files, message):
    for name, lines in files.items():
        Path(repo, name).parent.mkdir(parents=True, exist_ok=True)
        Path(repo, name).write_text("".join(f"line {i}\n" for i in range(lines)), encoding="utf-8")
    git_run(repo, "add", "-A")
    git_run(repo, "commit", "-qm", message)
    _, sha, _ = git_run(repo, "rev-parse", "HEAD")
    return sha


def spread(count, lines):
    """`count` 个文件、共 `lines` 行（每行都算作一次改动）。"""
    files = {}
    for index in range(count):
        share = lines // count + (1 if index < lines % count else 0)
        files[f"src/file{index}.py"] = share
    return files


class CollapsedPrReplayTests(unittest.TestCase):
    """回放 2026-11-03 被还原的 6 个 PR：前后 head 都必须命中拒绝。

    盒子上的清单（gocc:~/phanes-ctl/autodispatch/bak/restore-collapsed-heads-20261103.json）带真实
    sha，本仓无网络取不到，所以按清单记录的前后体量在本地重建等价的两条历史：
      A. fix 后 head 是 base 上的另一个提交（不在 fix 前 head 上续改）→ 祖先判定拒绝；
      B. fix 后 head 是 fix 前 head 的子提交但把大部分改动删掉 → 体量塌缩拒绝。
    """

    def replay(self, files_before, lines_before, files_after, lines_after, variant):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        repo = os.path.join(tmp.name, "repo")
        base = make_repo(repo)
        git_run(repo, "checkout", "-q", "-b", "before")
        before = commit_files(repo, spread(files_before, lines_before), "fix 前 head")
        if variant == "A":
            git_run(repo, "checkout", "-q", "-b", "after", base)
            after = commit_files(repo, spread(files_after, lines_after), "fix 后 head（整体替换）")
        else:
            git_run(repo, "checkout", "-q", "-b", "after", before)
            # 续改了，但把大部分改动删掉：文件数塌缩、行数一起塌缩
            keep = spread(files_after, lines_after)
            for name in spread(files_before, lines_before):
                if name not in keep:
                    Path(repo, name).unlink()
            after = commit_files(repo, keep, "fix 后 head（续改但塌缩）")
        original = MODULE.sh
        MODULE.sh = lambda cmd, timeout=120, inp=None: git_run(repo, *cmd[3:])
        try:
            return MODULE.harvest_guard(repo, base, before, after)
        finally:
            MODULE.sh = original

    def test_every_collapsed_pr_is_rejected(self):
        rows = []
        for pr, files_before, lines_before, files_after, lines_after in COLLAPSED_PRS:
            for variant in ("A", "B"):
                ok, reason, detail = self.replay(files_before, lines_before, files_after, lines_after, variant)
                self.assertFalse(ok, f"{pr} 变体 {variant} 必须命中拒绝")
                self.assertTrue(reason)
                if variant == "A":
                    self.assertIn("祖先", reason)
                else:
                    self.assertTrue("文件数" in reason or "行数" in reason, reason)
                rows.append((pr, variant, detail["files_before"], detail["files_after"],
                             detail["lines_before"], detail["lines_after"], reason))
        print("\n塌缩护栏回放（2026-11-03 被还原的 6 个 PR）")
        print(f"{'PR':<8}{'变体':<6}{'前 文件/行':<14}{'后 文件/行':<14}{'判定'}")
        for pr, variant, fb, fa, lb, la, reason in rows:
            print(f"{pr:<8}{variant:<6}{f'{fb}/{lb}':<14}{f'{fa}/{la}':<14}{reason}")


class FakeHost:
    """Records what the harvest path really does to GitHub and to the branch."""

    def __init__(self, ancestor_rc=1):
        self.ancestor_rc = ancestor_rc
        self.pushes = []
        self.prs = []
        self.label_edits = []
        self.comments = []
        self.logs = []
        self.bodies = []
        self.remote_head = OLD

    def sh(self, cmd, timeout=120, inp=None):
        argv = cmd if isinstance(cmd, list) else [cmd]
        joined = " ".join(argv)
        if "push" in joined:
            self.pushes.append(joined)
            return (0, "", "")
        if "fetch" in joined: return (0, "", "")
        if "merge-base" in joined: return (self.ancestor_rc, "", "")
        if "--numstat" in joined:
            return (0, numstat(8, 1780) if OLD in joined else numstat(1, 11), "")
        if "ls-remote" in joined:
            if self.remote_head is None: return (128, "", "boom")
            return (0, f"{self.remote_head}\trefs/heads/x\n" if self.remote_head else "", "")
        if "rev-parse" in joined: return (0, f"{NEW}\n", "")
        if "cat-file" in joined: return (0, "", "")
        return (0, "", "")

    def gh(self, args, timeout=60):
        if args[:2] == ["pr", "create"]:
            self.prs.append(list(args))
            return (0, "https://github.com/surlymochan/phanes/pull/1", "")
        if args[:2] == ["issue", "view"]:
            return (0, json.dumps({"body": "\n".join(self.bodies)}), "")
        if args[:2] == ["issue", "edit"]:
            self.label_edits.append(list(args))
            return (0, "", "")
        if args[:2] == ["issue", "comment"]:
            self.comments.append(list(args))
            return (0, "", "")
        if args[:2] == ["pr", "view"]:
            return (0, json.dumps({"headRefOid": OLD}), "")
        if args[:2] == ["pr", "list"]:
            return (0, json.dumps([{"number": 1807}]), "")
        return (0, "", "")

    def log(self, msg, brain=True):
        self.logs.append((msg, brain))


class HarvestPushGuardTests(unittest.TestCase):
    """收割路径：护栏拒绝时不推送、不建 PR、标 lane-failed、留失败台账与 brain 日志。"""

    def setUp(self):
        self.state = {}
        self.host = FakeHost()
        for name, value in (("sh", MODULE.sh), ("gh", MODULE.gh), ("log", MODULE.log)):
            self.addCleanup(setattr, MODULE, name, getattr(MODULE, name))
            setattr(MODULE, name, value)
        MODULE.sh, MODULE.gh, MODULE.log = self.host.sh, self.host.gh, self.host.log

    def finish(self):
        cfg = {"host": "host1", "backend": "codex", "model": "gpt", "repo": "~/work/phanes-fleet"}
        st = {"issue": 1807, "run": "20261103-162000-aaaaaa", "base": BASE, "host": cfg["host"]}
        info = {"status": "done", "branch": "work", "commit": NEW, "repo": cfg["repo"],
                "backend": "codex", "model": "gpt", "summary": "", "tests": "", "verify_rc": None}
        return MODULE.finish("codex", cfg, st, info, self.state)

    def test_rejected_harvest_does_not_push_and_marks_lane_failed(self):
        result = self.finish()
        self.assertEqual(result["status"], "harvest-guard-blocked")
        self.assertEqual(self.host.pushes, [], "护栏拒绝后不得推送（PR 分支保持 fix 前 head）")
        self.assertEqual(self.host.prs, [], "护栏拒绝后不得建 PR")
        edits = " ".join(" ".join(args) for args in self.host.label_edits)
        self.assertIn("--add-label lane-failed", edits)
        self.assertNotIn("--add-label lane-pr", edits)

    def test_rejection_is_logged_to_brain_with_the_facts(self):
        self.finish()
        brain = [msg for msg, is_brain in self.host.logs if is_brain]
        self.assertTrue(brain, "拒绝必须写一条 brain 日志")
        line = brain[-1]
        self.assertIn("1807", line)      # PR 号
        self.assertIn(OLD[:8], line)     # fix 前 sha
        self.assertIn(NEW[:8], line)     # fix 后 sha
        self.assertIn("8/1780", line)    # fix 前体量（文件数/改动行数）
        self.assertIn("1/11", line)      # fix 后体量
        self.assertIn("祖先", line)       # 判定原因

    def test_rejection_appends_a_failure_ledger_record(self):
        self.finish()
        ledger = self.state["_harvest_guard"]
        self.assertEqual(len(ledger), 1)
        record = ledger[-1]
        self.assertEqual(record["class"], MODULE.HARVEST_GUARD_CLASS)
        self.assertEqual(record["pr"], "1807")
        self.assertEqual(record["old_head"], OLD)
        self.assertEqual(record["new_head"], NEW)
        self.assertEqual(record["files_before"], 8)
        self.assertEqual(record["lines_before"], 1780)
        self.assertEqual(record["files_after"], 1)
        self.assertEqual(record["lines_after"], 11)
        self.assertTrue(record["reason"])

    def test_rejection_leaves_a_clue_on_the_issue(self):
        self.finish()
        comments = " ".join(" ".join(args) for args in self.host.comments)
        self.assertIn("塌缩护栏", comments)
        self.assertIn("未消耗 review 轮次", comments)

    def test_rewrite_marker_lets_the_harvest_through(self):
        self.host.bodies = ["本卡显式声明：整体重写"]
        self.host.ancestor_rc = 0        # 祖先成立
        result = self.finish()
        self.assertNotEqual(result["status"], "harvest-guard-blocked")
        self.assertTrue(self.host.pushes, "豁免的卡照常推送")

    def test_card_target_pr_head_is_the_reference(self):
        # fix/rebase 轮：卡里声明了目标 PR，护栏必须拿该 PR 的 head 当“fix 前 head”
        self.host.bodies = ["Rebase-PR: #1807 fleet/codex-issue1807-run"]
        result = self.finish()
        self.assertEqual(result["status"], "harvest-guard-blocked")
        self.assertEqual(self.state["_harvest_guard"][-1]["pr"], "1807")
        self.assertEqual(self.state["_harvest_guard"][-1]["old_head"], OLD)
        self.assertEqual(self.host.pushes, [])

    def test_a_fresh_branch_without_a_target_pr_is_not_blocked(self):
        self.host.remote_head = ""      # 远端还没有这个分支：没有可替换的 head
        result = self.finish()
        self.assertNotEqual(result["status"], "harvest-guard-blocked")
        self.assertTrue(self.host.pushes, "新建 PR 的收割照常推送")

    def test_unreadable_card_with_missing_branch_fails_closed(self):
        self.host.remote_head = ""
        self.host.bodies = ["Rebase-PR: #1807 fleet/codex-issue1807-run"]
        original_gh = self.host.gh
        for response in ((1, "", "failed"), (0, "invalid json", ""), (0, "{}", "")):
            with self.subTest(response=response):
                MODULE.gh = lambda args, timeout=60: response if args[:2] == ["issue", "view"] else original_gh(args, timeout)
                self.state.clear()
                result = self.finish()
                self.assertEqual(result["status"], "harvest-guard-blocked")
                self.assertEqual(self.host.pushes, [])
                self.assertIn("卡正文读取或解析失败", self.state["_harvest_guard"][-1]["reason"])
                self.assertTrue(any("卡正文读取或解析失败" in msg and brain for msg, brain in self.host.logs))

    def test_unreadable_fix_head_fails_closed(self):
        self.host.remote_head = None    # ls-remote 失败
        result = self.finish()
        self.assertEqual(result["status"], "harvest-guard-blocked")
        self.assertEqual(self.host.pushes, [], "读不到 fix 前 head 时不得推送")


if __name__ == "__main__":
    unittest.main()
