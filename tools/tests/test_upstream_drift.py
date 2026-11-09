import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tools import upstream_drift
from tools.upstream_drift import (blob_id, classify, local_files, snapshot,
                                 unmarked_paths)


SCRIPT = Path(__file__).resolve().parents[1] / "upstream_drift.py"


def commit(repo: Path, message: str) -> str:
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test",
                    "-c", "user.email=test@example.com", "commit", "-m", message],
                   check=True, capture_output=True)
    return subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"],
                                   text=True).strip()


class UpstreamDriftTests(unittest.TestCase):
    def test_classifies_generic_phanes_only_and_missing(self):
        # A shared file changed on both sides is generic; the scenario glob is local-only.
        result = classify(
            {"shared.txt": "old", "deleted.txt": "upstream"},
            {"shared.txt": "new", "examples/org-health-local/run.py": "new"},
            ["examples/org-health-local/**"],
        )
        self.assertEqual(result, {
            "phanes_only": ["examples/org-health-local/run.py"],
            "generic": ["shared.txt"],
            "missing": ["deleted.txt"],
        })

    def test_ignored_paths_are_not_missing(self):
        # Ignored upstream artefacts are absent on purpose, not deleted by phanes.
        result = classify(
            {"ignored/skipped.txt": "upstream", "kept.txt": "same"},
            {"kept.txt": "same"},
            [],
            ignored=frozenset({"ignored/skipped.txt"}),
        )
        self.assertEqual(result["missing"], [])
        self.assertEqual(result["generic"], [])

    def test_title_check_only_flags_changed_generic_paths(self):
        report = {"openbot": {"generic": ["src/app.ts"], "missing": [], "phanes_only": []}}
        changed = {"openbot/src/app.ts"}
        self.assertEqual(unmarked_paths(report, changed, "fix: change"), ["openbot/src/app.ts"])
        self.assertEqual(unmarked_paths(report, changed, "upstream: sync"), [])
        self.assertEqual(unmarked_paths(report, changed, "fix: upstream: sync"), [])
        self.assertEqual(
            unmarked_paths(report, changed, "swrm(codex): #1372 upstream(openbot): sync"), [])
        self.assertEqual(unmarked_paths(report, set(), "fix: unrelated"), [])

    def test_snapshot_rejects_missing_base_sha(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as scratch:
            remote = Path(scratch) / "remote.git"
            subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
            with self.assertRaises(RuntimeError):
                snapshot({"upstream_repo": str(remote)}, "deadbeef", Path(scratch))

    def test_snapshot_uses_requested_work_dir_for_scratch_and_cleans_it(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as scratch:
            root = Path(scratch)
            remote = root / "upstream"
            work_dir = root / "requested-work"
            remote.mkdir()
            work_dir.mkdir()
            subprocess.run(["git", "init", str(remote)], check=True, capture_output=True)
            (remote / "tracked.txt").write_text("snapshot\n")
            base_sha = commit(remote, "base")

            init_locations = []
            original_run = upstream_drift.run

            def recording_run(args, *, cwd=None):
                if args[:3] == ["git", "init", "--bare"]:
                    init_locations.append(Path(args[3]))
                return original_run(args, cwd=cwd)

            with patch.object(upstream_drift, "run", side_effect=recording_run):
                result = snapshot({"upstream_repo": str(remote)}, base_sha, work_dir)

            self.assertEqual(result, {"tracked.txt": blob_id(b"snapshot\n")})
            self.assertEqual(len(init_locations), 1)
            self.assertEqual(init_locations[0].parent, work_dir)
            self.assertFalse(init_locations[0].exists())

    def test_snapshot_rejects_missing_base_sha_with_localized_git_error(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as scratch:
            remote = Path(scratch) / "remote.git"
            subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
            real_run = subprocess.run
            fetch_results = []

            def run_with_localized_fetch_error(args, **kwargs):
                if args[:2] == ["git", "--git-dir"] and "fetch" in args:
                    result = subprocess.CompletedProcess(args, 128, "", "致命错误：找不到远程版本")
                    fetch_results.append(result)
                    return result
                return real_run(args, **kwargs)

            with patch("tools.upstream_drift.subprocess.run", side_effect=run_with_localized_fetch_error):
                with self.assertRaises(RuntimeError):
                    snapshot({"upstream_repo": str(remote)}, "deadbeef", Path(scratch))
            self.assertEqual(len(fetch_results), 1)
            self.assertNotEqual(fetch_results[0].returncode, 0)
            self.assertEqual(fetch_results[0].args[-2:], [str(remote), "deadbeef"])

    def test_local_files_hash_symlink_targets(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as scratch:
            directory = Path(scratch) / "demo"
            (directory / "shared").mkdir(parents=True)
            (directory / "skills").symlink_to("shared")
            (directory / "file.txt").write_text("data\n")
            files = local_files(directory)
            self.assertEqual(files["skills"], blob_id(b"shared"))
            self.assertEqual(files["file.txt"], blob_id(b"data\n"))

    def test_cli_reports_drift_from_local_upstream(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as scratch:
            root = Path(scratch) / "checkout"
            upstream = Path(scratch) / "upstream"
            (root / "demo").mkdir(parents=True)
            upstream.mkdir()
            subprocess.run(["git", "init", str(upstream)], check=True, capture_output=True)
            (upstream / "common.txt").write_text("baseline\n")
            (upstream / "removed.txt").write_text("will be removed locally\n")
            subprocess.run(["git", "-C", str(upstream), "add", "."], check=True)
            subprocess.run(["git", "-C", str(upstream), "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "base"], check=True, capture_output=True)
            sha = subprocess.check_output(["git", "-C", str(upstream), "rev-parse", "HEAD"], text=True).strip()
            (root / "demo" / "common.txt").write_text("changed\n")
            (root / "demo" / "added.txt").write_text("local\n")
            config = Path(scratch) / "config.json"
            config.write_text(json.dumps({"components": {"demo": {
                "upstream_repo": str(upstream), "base_sha": sha,
                "phanes_only": ["added.txt"],
            }}}))
            output = subprocess.check_output([
                "python3", str(SCRIPT), "--json", "--config", str(config), "--root", str(root),
            ], text=True)
            report = json.loads(output)["demo"]
            self.assertEqual(report["generic"], ["common.txt"])
            self.assertEqual(report["phanes_only"], ["added.txt"])
            self.assertEqual(report["missing"], ["removed.txt"])

    def test_cli_keeps_symlinks_and_ignores_gitignored_files(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as scratch:
            root = Path(scratch) / "checkout"
            upstream = Path(scratch) / "upstream"
            (root / "demo" / "shared").mkdir(parents=True)
            upstream.mkdir()
            subprocess.run(["git", "init", str(upstream)], check=True, capture_output=True)
            (upstream / "common.txt").write_text("baseline\n")
            (upstream / "kept.txt").write_text("kept\n")
            (upstream / "artifacts").mkdir()
            (upstream / "artifacts" / "raw.txt").write_text("upstream artefact\n")
            (upstream / "skills").symlink_to("shared")
            sha = commit(upstream, "base")

            # phanes side: same symlink, artefact ignored, one file changed.
            subprocess.run(["git", "init", str(root)], check=True, capture_output=True)
            (root / ".gitignore").write_text("demo/artifacts/**\n")
            (root / "demo" / "common.txt").write_text("changed\n")
            (root / "demo" / "kept.txt").write_text("kept\n")
            (root / "demo" / "artifacts").mkdir()
            (root / "demo" / "artifacts" / "raw.txt").write_text("upstream artefact\n")
            (root / "demo" / "skills").symlink_to("shared")
            commit(root, "phanes")

            config = Path(scratch) / "config.json"
            config.write_text(json.dumps({"components": {"demo": {
                "upstream_repo": str(upstream), "base_sha": sha,
                "phanes_only": [],
            }}}))
            output = subprocess.check_output([
                "python3", str(SCRIPT), "--json", "--config", str(config), "--root", str(root),
            ], text=True)
            report = json.loads(output)["demo"]
            self.assertEqual(report["generic"], ["common.txt"])
            self.assertEqual(report["missing"], [])


if __name__ == "__main__":
    unittest.main()
