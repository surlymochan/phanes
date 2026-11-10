"""Reproduction of the public-verify scanner required by issue #2034.

The scanner, the required CI check and the visibility policy do not exist on
the batch baseline, so every test of this file fails there.  The tests encode
the acceptance criteria of #2034 and the supplementary requirement of its
comment: the ``commits`` subcommand has to scan the author and the committer
identity of every commit, not only the commit message.

The test source itself must not carry literals that the scanner reports, so
the sensitive looking values are assembled at runtime.

The original eleven regression tests from commit 3e4e6de6 are retained below.
The file uses ``unittest`` on purpose: the repository runs ``tools/tests`` with
``python -m unittest discover -s tools/tests -p 'test_*.py'``, so a plain
pytest style module would never be collected by the CI gate.
"""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools" / "ci" / "public_verify.py"
WORKFLOW = ROOT / ".github" / "workflows" / "public-verify.yml"
REUSABLE_WORKFLOW = ROOT / ".github" / "workflows" / "public-verify-reusable.yml"
VISIBILITY = ROOT / "handoff" / "VISIBILITY.md"

# Fictional values, assembled at runtime so that the scanner does not report
# this test file itself.
PRIVATE_ADDRESS = "10." + "11.12.13"
LINK_LOCAL_ADDRESS = "100." + "64.0.7"
PRIVATE_PATH = "/ho" + "me/fixture"
PRIVATE_EMAIL = "author" + "@mail.example.org"
COMMITTER_EMAIL = "committer" + "@mail.example.org"
NO_REPLY_EMAIL = "someone" + "@users.noreply.github.com"
INTERNAL_DOMAIN = "node" + ".internal"
HOLDOUT_NAME = "secret-" + "hold" + "out"
TOKEN = "gh" + "p_" + "a" * 36
ACCOUNT_TERM = "acct-fixture-42"
CLEAN_IDENTITY = "test@example.invalid"


def digest(value):
    """The hash prefix of a finding, as the scanner reports it."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]


class PublicVerifyScannerTests(unittest.TestCase):
    """The public-verify scanner of #2034."""

    def setUp(self):
        if not SCRIPT.is_file():
            self.fail("the public-verify scanner is missing: " + str(SCRIPT))
        self.temp = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def scan(self, *args, data=None, cwd=None, env=None):
        """Run the scanner and return the completed process."""
        clean = os.environ.copy()
        clean.pop("PUBLIC_VERIFY_DENYLIST", None)
        if env:
            clean.update(env)
        return subprocess.run([sys.executable, str(SCRIPT), *args], input=data,
                              text=True, capture_output=True, cwd=cwd, env=clean)

    def scan_text(self, value, *options):
        return self.scan("text", "--label", "repro", *options, data=value)

    def git(self, repo, *args, env=None):
        merged = os.environ.copy()
        if env:
            merged.update(env)
        return subprocess.check_output(["git", *args], cwd=repo, text=True,
                                       env=merged).strip()

    def make_repo(self, name="repo"):
        """A repository with one clean commit, returns the repository path."""
        repo = self.root / name
        repo.mkdir()
        self.git(repo, "init", "-q")
        self.git(repo, "config", "user.email", CLEAN_IDENTITY)
        self.git(repo, "config", "user.name", "Tester")
        (repo / "keep.txt").write_text("clean line\n", encoding="utf-8")
        self.git(repo, "add", ".")
        self.git(repo, "commit", "-qm", "base")
        return repo

    def findings(self, result):
        return json.loads(result.stdout)

    def test_generic_rules_report_private_material(self):
        cases = (
            ("ip", PRIVATE_ADDRESS),
            ("ip", LINK_LOCAL_ADDRESS),
            ("path", PRIVATE_PATH),
            ("email", PRIVATE_EMAIL),
            ("domain", INTERNAL_DOMAIN),
            ("hold" + "out", HOLDOUT_NAME),
            ("token", TOKEN),
        )
        for category, value in cases:
            with self.subTest(category=category):
                result = self.scan_text(value, "--generic-only", "--format", "json")
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                found = self.findings(result)
                self.assertTrue(found, result.stdout)
                self.assertIn(category, {item["category"] for item in found})
                self.assertNotIn(value, result.stdout + result.stderr)

    def test_reserved_material_is_not_reported(self):
        cases = (
            "127." + "0.0.1",
            "192." + "0.2.1",
            "203." + "0.113.9",
            "version 10.2.3",
            "/Us" + "ers/<name>/file",
            "/ho" + "me/runner/work",
            NO_REPLY_EMAIL,
            "user" + "@example.invalid",
            "service." + "international",
            "house" + "holdoutcomes",
        )
        for value in cases:
            with self.subTest(value=value):
                result = self.scan_text(value, "--generic-only")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_denylist_is_required_and_fails_closed(self):
        self.assertEqual(self.scan_text("clean").returncode, 2)
        self.assertEqual(self.scan_text("clean", "--generic-only").returncode, 0)
        rules = self.root / "rules.txt"
        rules.write_text("", encoding="utf-8")
        self.assertEqual(self.scan_text("clean", "--denylist", str(rules)).returncode, 2)
        rules.write_text("unknown" + "\tvalue\n", encoding="utf-8")
        self.assertEqual(self.scan_text("clean", "--denylist", str(rules)).returncode, 2)
        rules.write_text("host" + "\tre:[\n", encoding="utf-8")
        self.assertEqual(self.scan_text("clean", "--denylist", str(rules)).returncode, 2)

    def test_denylist_terms_match_on_word_boundaries(self):
        rules = self.root / "rules.txt"
        rules.write_text("account" + "\t" + ACCOUNT_TERM + "\n", encoding="utf-8")
        hit = self.scan_text("an " + ACCOUNT_TERM + " here", "--denylist", str(rules))
        self.assertEqual(hit.returncode, 1, hit.stdout + hit.stderr)
        self.assertNotIn(ACCOUNT_TERM, hit.stdout + hit.stderr)
        clean = self.scan_text(ACCOUNT_TERM + "s", "--denylist", str(rules))
        self.assertEqual(clean.returncode, 0, clean.stdout + clean.stderr)

    def test_findings_never_repeat_the_matched_text(self):
        result = self.scan_text(PRIVATE_EMAIL, "--generic-only", "--format", "json")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        found = self.findings(result)
        self.assertTrue(found, result.stdout)
        for item in found:
            self.assertEqual(set(item), {"category", "location", "hash"})
            self.assertEqual(len(item["hash"]), 8)
        self.assertIn(digest(PRIVATE_EMAIL), {item["hash"] for item in found})
        self.assertNotIn(PRIVATE_EMAIL, result.stdout + result.stderr)

    def test_diff_scans_added_lines_and_new_paths_only(self):
        repo = self.make_repo()
        base = self.git(repo, "rev-parse", "HEAD")
        (repo / "keep.txt").write_text("clean line\n" + PRIVATE_ADDRESS + "\n",
                                       encoding="utf-8")
        self.git(repo, "add", ".")
        self.git(repo, "commit", "-qm", "second")
        result = self.scan("diff", "--generic-only", "--base", base, "--head", "HEAD",
                           "--format", "json", cwd=repo)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertNotIn(PRIVATE_ADDRESS, result.stdout + result.stderr)

        # A line that the change does not touch is not scanned again.
        quiet = self.make_repo("quiet")
        (quiet / "keep.txt").write_text(PRIVATE_ADDRESS + "\n", encoding="utf-8")
        self.git(quiet, "add", ".")
        self.git(quiet, "commit", "-qm", "store the value")
        stored = self.git(quiet, "rev-parse", "HEAD")
        (quiet / "keep.txt").write_text(PRIVATE_ADDRESS + "\nother\n", encoding="utf-8")
        self.git(quiet, "add", ".")
        self.git(quiet, "commit", "-qm", "third")
        result = self.scan("diff", "--generic-only", "--base", stored, "--head", "HEAD",
                           cwd=quiet)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        # A path that the change adds is scanned by name.
        names = self.make_repo("names")
        names_base = self.git(names, "rev-parse", "HEAD")
        (names / INTERNAL_DOMAIN).write_text("clean\n", encoding="utf-8")
        self.git(names, "add", ".")
        self.git(names, "commit", "-qm", "fourth")
        result = self.scan("diff", "--generic-only", "--base", names_base, "--head", "HEAD",
                           cwd=names)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)

    def test_commits_scans_commit_messages(self):
        repo = self.make_repo()
        base = self.git(repo, "rev-parse", "HEAD")
        (repo / "keep.txt").write_text("clean line\nsecond\n", encoding="utf-8")
        self.git(repo, "add", ".")
        self.git(repo, "commit", "-qm", "contact " + PRIVATE_ADDRESS)
        result = self.scan("commits", "--generic-only", "--base", base, "--head", "HEAD",
                           "--format", "json", cwd=repo)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertNotIn(PRIVATE_ADDRESS, result.stdout + result.stderr)

    def test_commits_scans_author_and_committer_identities(self):
        repo = self.make_repo()
        base = self.git(repo, "rev-parse", "HEAD")
        (repo / "keep.txt").write_text("clean line\nsecond\n", encoding="utf-8")
        self.git(repo, "add", ".")
        self.git(repo, "commit", "-qm", "second", env={
            "GIT_AUTHOR_NAME": "Fixture " + ACCOUNT_TERM,
            "GIT_AUTHOR_EMAIL": PRIVATE_EMAIL,
            "GIT_COMMITTER_NAME": "Fixture Committer",
            "GIT_COMMITTER_EMAIL": COMMITTER_EMAIL,
        })
        result = self.scan("commits", "--generic-only", "--base", base, "--head", "HEAD",
                           "--format", "json", cwd=repo)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        hashes = {item["hash"] for item in self.findings(result)}
        self.assertIn(digest(PRIVATE_EMAIL), hashes)
        self.assertIn(digest(COMMITTER_EMAIL), hashes)

        rules = self.root / "rules.txt"
        rules.write_text("account" + "\t" + ACCOUNT_TERM + "\n", encoding="utf-8")
        result = self.scan("commits", "--base", base, "--head", "HEAD",
                           "--denylist", str(rules), "--format", "json", cwd=repo)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        found = self.findings(result)
        self.assertIn("account", {item["category"] for item in found})
        self.assertIn(digest(ACCOUNT_TERM), {item["hash"] for item in found})
        self.assertNotIn(ACCOUNT_TERM, result.stdout + result.stderr)

    def test_tree_and_history_scan_the_repository(self):
        repo = self.make_repo()
        (repo / "keep.txt").write_text(PRIVATE_ADDRESS + "\n", encoding="utf-8")
        self.git(repo, "add", ".")
        self.git(repo, "commit", "-qm", "store the value")
        for command in ("tree", "history"):
            with self.subTest(command=command):
                result = self.scan(command, "--generic-only", cwd=repo)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertNotIn(PRIVATE_ADDRESS, result.stdout + result.stderr)


class PublicVerifyPolicyTests(unittest.TestCase):
    """The required CI check and the visibility policy of #2034."""

    def test_required_check_workflows_are_present_and_parseable(self):
        try:
            import yaml
        except ImportError:  # pragma: no cover - the parser is optional
            self.skipTest("PyYAML is not installed")
        for path in (WORKFLOW, REUSABLE_WORKFLOW):
            with self.subTest(path=path.name):
                self.assertTrue(path.is_file(), "missing workflow: " + str(path))
                document = yaml.safe_load(path.read_text(encoding="utf-8"))
                self.assertIsInstance(document, dict)
                self.assertIn("jobs", document)

    def test_visibility_policy_documents_the_objects_and_the_gates(self):
        self.assertTrue(VISIBILITY.is_file(), "missing policy: " + str(VISIBILITY))
        document = VISIBILITY.read_text(encoding="utf-8")
        for gate in ("推送前", "写入前", "合并前", "事后巡检"):
            with self.subTest(gate=gate):
                self.assertIn(gate, document)
        for category in ("issue", "project", "PR"):
            with self.subTest(category=category):
                self.assertIn(category, document)


def run(*args, data=None, cwd=None, env=None):
    clean_env = os.environ.copy()
    clean_env.pop("PUBLIC_VERIFY_DENYLIST", None)
    if env:
        clean_env.update(env)
    return subprocess.run([sys.executable, str(SCRIPT), *args], input=data,
                          text=True, capture_output=True, cwd=cwd, env=clean_env)


def text(value, *options):
    return run("text", "--label", "test", "--generic-only", *options, data=value)


def git(repo, *args):
    return subprocess.check_output(["git", *args], cwd=repo, text=True).strip()


TEST_GENERIC_FINDINGS_ARE_REDACTED_VALUES = [
    "10." + "3.4.5", "172." + "16.3.4", "192." + "168.1.2",
    "100." + "64.0.1", "198." + "18.1.1", "fc" + "00::1234",
    "/Us" + "ers/alice/file", "/ho" + "me/alice/file", "/ro" + "ot/file",
    "/private/" + "tmp/file", "/var/" + "folders/abc", "C:\\Us" + "ers\\alice\\file",
    "alice" + "@mail.com", "gh" + "p_" + "a" * 36,
    "service." + "internal", "service." + "ts.net", "secret-" + "hold" + "out/data",
]

TEST_GENERIC_CLEAN_VALUES = [
    "127." + "0.0.1", "192." + "0.2.1", "203." + "0.113.1",
    "version 10.2.3", "/Us" + "ers/<name>/file", "/ho" + "me/runner/work",
    "user" + "@example.invalid", "someone" + "@users.noreply.github.com",
    "service." + "international", "householdoutcomes",
]


class PublicVerifyAdditionalTests(unittest.TestCase):
    setUp = PublicVerifyScannerTests.setUp

    def test_denylist_configuration_reporting(self):
        for value in (None, "", " \t\r\n", "# fixture comment\n  # another\n",
                      "account\tunused-fixture", "unknown\tfixture", "host\tre:["):
            with self.subTest(value=value):
                output = self.root / "output"
                summary = self.root / "summary"
                output.write_text("prior-output\n", encoding="utf-8")
                summary.write_text("prior-summary\n", encoding="utf-8")
                env = dict(os.environ, GITHUB_OUTPUT=str(output),
                           GITHUB_STEP_SUMMARY=str(summary))
                env.pop("PUBLIC_VERIFY_DENYLIST", None)
                if value is not None:
                    env["PUBLIC_VERIFY_DENYLIST"] = value
                result = subprocess.run(
                    [sys.executable, str(SCRIPT.with_name("public_verify_config.py"))],
                    env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                configured = value in ("account\tunused-fixture", "unknown\tfixture", "host\tre:[")
                self.assertEqual(output.read_text(encoding="utf-8"),
                                 "prior-output\nconfigured=" + str(configured).lower() + "\n")
                message = "SKIPPED: denylist not configured"
                self.assertEqual(result.stdout, "" if configured else
                                 message + "\n::warning::" + message + "\n")
                self.assertEqual(result.stderr, "")
                self.assertEqual(summary.read_text(encoding="utf-8"),
                                 "prior-summary\n" + ("" if configured else message + "\n"))

    def test_generic_findings_are_redacted(self):
        for value in TEST_GENERIC_FINDINGS_ARE_REDACTED_VALUES:
            with self.subTest(value=value):
                result = text(value, "--format", "json")
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertNotIn(value, result.stdout + result.stderr)
                finding = json.loads(result.stdout)[0]
                self.assertEqual(set(finding), {"category", "location", "hash"})
                self.assertEqual(len(finding["hash"]), 8)

    def test_generic_clean(self):
        for value in TEST_GENERIC_CLEAN_VALUES:
            with self.subTest(value=value):
                self.assertEqual(text(value).returncode, 0)

    def test_denylist_required_and_boundary(self):
        tmp_path = self.root
        self.assertEqual(run("text", "--label", "test", data="clean").returncode, 2)
        deny = tmp_path / "rules"
        deny.write_text("host\t" + "owl" + "tower\n")
        result = run("text", "--label", "test", "--denylist", str(deny), data="an Owltower here")
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("Owltower", result.stdout + result.stderr)
        self.assertEqual(run("text", "--label", "test", "--denylist", str(deny), data="owltowers").returncode, 0)
        deny.write_text("domain\tre:" + "foo[0-9]+\n")
        self.assertEqual(run("text", "--label", "test", "--denylist", str(deny), data="foo42").returncode, 1)
        deny.write_text("")
        self.assertEqual(run("text", "--label", "test", "--denylist", str(deny), data="x").returncode, 2)

    def test_diff_tree_history_and_commits(self):
        tmp_path = self.root
        git(tmp_path, "init", "-q")
        git(tmp_path, "config", "user.email", "test@example.invalid")
        git(tmp_path, "config", "user.name", "Tester")
        secret = "10." + "2.3.4"
        (tmp_path / "a").write_text(secret + "\nold\n")
        git(tmp_path, "add", ".")
        git(tmp_path, "commit", "-qm", "initial")
        base = git(tmp_path, "rev-parse", "HEAD")
        (tmp_path / "a").write_text(secret + "\nnew\n")
        git(tmp_path, "add", ".")
        git(tmp_path, "commit", "-qm", "clean")
        self.assertEqual(run("diff", "--generic-only", "--base", base, "--head", "HEAD", cwd=tmp_path).returncode, 0)
        self.assertEqual(run("tree", "--generic-only", "--head", "HEAD", cwd=tmp_path).returncode, 1)
        self.assertEqual(run("history", "--generic-only", cwd=tmp_path).returncode, 1)
        (tmp_path / ("node." + "internal")).write_text("ok\n")
        git(tmp_path, "add", ".")
        git(tmp_path, "commit", "-qm", "contact " + secret)
        result = run("diff", "--generic-only", "--base", base, "--head", "HEAD", cwd=tmp_path)
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("node." + "internal", result.stdout)
        self.assertEqual(run("commits", "--generic-only", "--base", base, "--head", "HEAD", cwd=tmp_path).returncode, 1)

    def test_environment_denylist_and_invalid_config(self):
        key = "PUBLIC_VERIFY_DENYLIST"
        secret = "cedar" + "signal"
        result = run("text", "--label", "private", data=secret,
                     env={key: "bus\t" + secret})
        self.assertEqual(result.returncode, 1)
        self.assertNotIn(secret, result.stdout + result.stderr)
        self.assertEqual(run("text", "--label", "x", data="x", env={key: "unknown\tvalue"}).returncode, 2)
        self.assertEqual(run("text", "--label", "x", data="x", env={key: "host\tre:["}).returncode, 2)

    def test_rename_does_not_rescan_unchanged_lines(self):
        tmp_path = self.root
        git(tmp_path, "init", "-q")
        git(tmp_path, "config", "user.email", "test@example.invalid")
        git(tmp_path, "config", "user.name", "Tester")
        (tmp_path / "old").write_text(("10." + "2.3.4\n") * 10)
        git(tmp_path, "add", ".")
        git(tmp_path, "commit", "-qm", "base")
        base = git(tmp_path, "rev-parse", "HEAD")
        git(tmp_path, "mv", "old", "new")
        git(tmp_path, "commit", "-qm", "rename")
        self.assertEqual(run("diff", "--generic-only", "--base", base, "--head", "HEAD", cwd=tmp_path).returncode, 0)


    def test_shared_denylist_contract(self):
        cases = (
            ("# comment\nhost\t.fixture\n", "node.fixture"),
            ("account\tre:(?-i:fixture)\n", "FIXTURE"),
            ("host\tfixture\n", "FIXTURE"),
        )
        for rules, value in cases:
            with self.subTest(rules=rules):
                result = run("text", "--label", "contract", data=value,
                             env={"PUBLIC_VERIFY_DENYLIST": rules})
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertNotIn(value, result.stdout + result.stderr)
        result = run("text", "--label", "contract", data="clean",
                     env={"PUBLIC_VERIFY_DENYLIST": "# comment only"})
        self.assertEqual(result.returncode, 2)

    def test_each_identity_field_and_noreply(self):
        repo = self.root
        git(repo, "init", "-q")
        git(repo, "config", "user.email", CLEAN_IDENTITY)
        git(repo, "config", "user.name", "Tester")
        git(repo, "commit", "--allow-empty", "-qm", "base")
        for field in ("AUTHOR_NAME", "AUTHOR_EMAIL", "COMMITTER_NAME", "COMMITTER_EMAIL"):
            with self.subTest(field=field):
                base = git(repo, "rev-parse", "HEAD")
                value = PRIVATE_EMAIL if field.endswith("EMAIL") else ACCOUNT_TERM
                env = dict(os.environ, **{"GIT_" + field: value})
                subprocess.run(["git", "commit", "--allow-empty", "-qm", "clean"],
                               cwd=repo, env=env, check=True, capture_output=True)
                result = run("commits", "--base", base, "--head", "HEAD", "--format", "json",
                             cwd=repo, env={"PUBLIC_VERIFY_DENYLIST": "account\t" + ACCOUNT_TERM})
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn(digest(value), {item["hash"] for item in json.loads(result.stdout)})
                self.assertNotIn(value, result.stdout + result.stderr)
        base = git(repo, "rev-parse", "HEAD")
        env = dict(os.environ, GIT_AUTHOR_EMAIL=NO_REPLY_EMAIL, GIT_COMMITTER_EMAIL=NO_REPLY_EMAIL)
        subprocess.run(["git", "commit", "--allow-empty", "-qm", "clean"],
                       cwd=repo, env=env, check=True, capture_output=True)
        result = run("commits", "--generic-only", "--base", base, "--head", "HEAD", cwd=repo)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_history_deduplicates_blobs_without_losing_paths_or_identities(self):
        repo = self.root
        git(repo, "init", "-q")
        git(repo, "config", "user.email", CLEAN_IDENTITY)
        git(repo, "config", "user.name", "Tester")
        (repo / "clean").write_text(PRIVATE_ADDRESS + "\n", encoding="utf-8")
        git(repo, "add", ".")
        git(repo, "commit", "-qm", "base")
        git(repo, "mv", "clean", INTERNAL_DOMAIN)
        env = dict(os.environ, GIT_AUTHOR_EMAIL=PRIVATE_EMAIL)
        subprocess.run(["git", "commit", "-qm", "rename"], cwd=repo, env=env,
                       check=True, capture_output=True)
        git(repo, "commit", "--allow-empty", "-qm", "unchanged")
        git(repo, "update-ref", "refs/heads/" + INTERNAL_DOMAIN, "HEAD")
        result = run("tree", "--generic-only", "--head", INTERNAL_DOMAIN, cwd=repo)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertNotIn(INTERNAL_DOMAIN, result.stdout + result.stderr)
        before = git(repo, "status", "--porcelain")
        result = run("history", "--generic-only", "--format", "json", cwd=repo)
        self.assertEqual(result.returncode, 1, result.stderr)
        hashes = [item["hash"] for item in json.loads(result.stdout)]
        self.assertEqual(hashes.count(digest(PRIVATE_ADDRESS)), 1)
        self.assertIn(digest(INTERNAL_DOMAIN), hashes)
        self.assertIn(digest(PRIVATE_EMAIL), hashes)
        self.assertEqual(git(repo, "status", "--porcelain"), before)

    def test_workflow_checkout_and_scan_script(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML is not installed")
        import shutil

        workflow = yaml.safe_load(REUSABLE_WORKFLOW.read_text(encoding="utf-8"))
        trigger = workflow.get("on", workflow.get(True))
        self.assertFalse(trigger["workflow_call"]["secrets"]["PUBLIC_VERIFY_DENYLIST"]["required"])
        config = workflow["jobs"]["denylist-config"]
        self.assertEqual(config["outputs"]["configured"], "${{ steps.config.outputs.configured }}")
        config_checkout, config_step = config["steps"]
        self.assertEqual(config_checkout["with"]["ref"], "${{ inputs.scanner_ref || github.sha }}")
        self.assertFalse(config_checkout["with"]["persist-credentials"])
        self.assertEqual(config_step["id"], "config")
        self.assertEqual(config_step["env"]["PUBLIC_VERIFY_DENYLIST"], "${{ secrets.PUBLIC_VERIFY_DENYLIST }}")
        self.assertEqual(config_step["run"], "python3 .public-verify-scanner/tools/ci/public_verify_config.py")
        self.assertEqual(workflow["jobs"]["public-verify"]["needs"], "denylist-config")
        self.assertEqual(workflow["jobs"]["public-verify"]["if"],
                         "needs.denylist-config.outputs.configured == 'true'")
        steps = workflow["jobs"]["public-verify"]["steps"]
        self.assertEqual(steps[0]["with"]["ref"], "${{ github.sha }}")
        self.assertEqual(steps[0]["with"]["fetch-depth"], 0)
        self.assertEqual(steps[1]["with"]["ref"], "${{ inputs.scanner_ref || github.sha }}")
        self.assertFalse(steps[0]["with"]["persist-credentials"])
        self.assertNotIn("git fetch", steps[2]["run"])
        caller = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        self.assertEqual(caller["jobs"]["public-verify"]["with"]["scanner_ref"], "${{ github.sha }}")

        # Model a fetched merge ref whose head is not the upstream branch tip.
        repo = self.root
        git(repo, "init", "-q")
        git(repo, "config", "user.email", CLEAN_IDENTITY)
        git(repo, "config", "user.name", "Tester")
        git(repo, "commit", "--allow-empty", "-qm", "base")
        base = git(repo, "rev-parse", "HEAD")
        git(repo, "commit", "--allow-empty", "-qm", "fork head")
        head = git(repo, "rev-parse", "HEAD")
        tree = git(repo, "rev-parse", "HEAD^{tree}")
        merge = git(repo, "commit-tree", tree, "-p", base, "-p", head, "-m", "merge preview")
        git(repo, "update-ref", "refs/pull/1/merge", merge)
        scanner = repo / ".public-verify-scanner" / "tools" / "ci"
        scanner.mkdir(parents=True)
        for name in ("public_verify.py", "public_verify_denylist.py"):
            shutil.copyfile(SCRIPT.parent / name, scanner / name)
        env = dict(os.environ, PUBLIC_VERIFY_DENYLIST="account\tunused-fixture", EVENT_NAME="pull_request",
                   BASE=base, HEAD=head, PR_TITLE="$(touch injected)", PR_BODY="clean")
        script = steps[2]["run"]
        result = subprocess.run(["bash", "-c", script], cwd=repo, env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((repo / "injected").exists())
        env["PUBLIC_VERIFY_DENYLIST"] = ""
        result = subprocess.run(["bash", "-c", script], cwd=repo, env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()
