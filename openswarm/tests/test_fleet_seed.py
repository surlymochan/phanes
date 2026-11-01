import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _load  # noqa: E402

xs = _load.load()


class FleetSeedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="openswarm-seed-")
        self.root = pathlib.Path(self.tmp.name)
        self.patch = mock.patch.object(xs, "DATA", str(self.root / "data"))
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.addCleanup(self.tmp.cleanup)
        self.env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
                    "GIT_TERMINAL_PROMPT": "0"}

    def git(self, *args, cwd=None):
        return subprocess.check_output(["git", *args], cwd=cwd, env=self.env, stderr=subprocess.PIPE).decode().strip()

    def repo(self, path, bare=False):
        path.mkdir()
        self.git("init", "--bare" if bare else "--quiet", str(path))

    def test_push_bundle_can_checkout_requested_sha(self):
        source, remote = self.root / "source", self.root / "remote.git"
        self.repo(source)
        self.git("-C", str(source), "config", "user.name", "test")
        self.git("-C", str(source), "config", "user.email", "test@example.invalid")
        (source / "base.txt").write_text("requested base\n")
        self.git("-C", str(source), "add", "base.txt")
        self.git("-C", str(source), "commit", "-m", "base")
        sha = self.git("-C", str(source), "rev-parse", "HEAD")
        self.repo(remote, bare=True)
        hosts = {"local": {"name": "local", "kind": "local", "repos": {"project": str(source)}},
                 "remote": {"name": "remote", "kind": "local", "seed_strategy": "push",
                            "repos": {"project": str(remote)}}}
        self.assertEqual(xs.fleet_seed(hosts["remote"], sha, hosts), sha)
        self.assertEqual(self.git("--git-dir", str(remote), "rev-parse", f"refs/openswarm/base/{sha}"), sha)
        checkout = self.root / "checkout"
        self.git("--git-dir", str(remote), "worktree", "add", "--detach", str(checkout), sha)
        self.assertEqual((checkout / "base.txt").read_text(), "requested base\n")

    def test_fetch_retries_with_exponential_backoff(self):
        host = {"name": "remote", "kind": "local", "seed_strategy": "fetch", "fetch_retries": 2,
                "repos": {"project": str(self.root)}}
        outcomes = [(1, b"", b"offline"), (1, b"", b"offline"), (0, b"", b""),
                    (0, b"", b""), (0, b"a" * 40 + b"\n", b"")]
        with mock.patch.object(xs, "host_git", side_effect=outcomes) as git, \
                mock.patch.object(xs.time, "sleep") as sleep:
            self.assertEqual(xs.fleet_seed(host, "HEAD", {"local": {"name": "local", "kind": "local"}}), "a" * 40)
        self.assertEqual(git.call_count, 5)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 2])

    def test_unknown_strategy_is_rejected(self):
        host = {"name": "remote", "kind": "local", "seed_strategy": "pull"}
        with self.assertRaisesRegex(ValueError, "invalid seed_strategy"):
            xs.fleet_seed(host, "HEAD", {"local": {"name": "local", "kind": "local"}})

    def test_default_local_host_without_repo_mapping_uses_current_checkout(self):
        host = {"name": "local", "kind": "local"}
        sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=os.getcwd()).decode().strip()
        expected = (0, b"", b"")
        with mock.patch.object(xs, "host_git", side_effect=[expected, (0, b"", b""), (0, sha.encode(), b"")]):
            self.assertEqual(xs.fleet_seed(host, sha, {"local": host}), sha)


if __name__ == "__main__":
    unittest.main()
