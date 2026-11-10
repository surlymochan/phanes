import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "ci" / "public_verify.py"


def run(*args, data=None, cwd=None, env=None):
    clean_env = os.environ.copy()
    clean_env.pop("PUBLIC_VERIFY_DENYLIST", None)
    if env:
        clean_env.update(env)
    return subprocess.run([sys.executable, str(SCRIPT), *args], input=data,
                          text=True, capture_output=True, cwd=cwd, env=clean_env)


def text(value, *options):
    return run("text", "--label", "test", "--generic-only", *options, data=value)


@pytest.mark.parametrize("value", [
    "10." + "3.4.5", "172." + "16.3.4", "192." + "168.1.2",
    "100." + "64.0.1", "198." + "18.1.1", "fc" + "00::1234",
    "/Us" + "ers/alice/file", "/ho" + "me/alice/file", "/ro" + "ot/file",
    "/private/" + "tmp/file", "/var/" + "folders/abc", "C:\\Us" + "ers\\alice\\file",
    "alice" + "@mail.com", "gh" + "p_" + "a" * 36,
    "service." + "internal", "service." + "ts.net", "secret-" + "hold" + "out/data",
])
def test_generic_findings_are_redacted(value):
    result = text(value, "--format", "json")
    assert result.returncode == 1, result.stderr
    assert value not in result.stdout + result.stderr
    finding = json.loads(result.stdout)[0]
    assert set(finding) == {"category", "location", "hash"}
    assert len(finding["hash"]) == 8


@pytest.mark.parametrize("value", [
    "127." + "0.0.1", "192." + "0.2.1", "203." + "0.113.1",
    "version 10.2.3", "/Us" + "ers/<name>/file", "/ho" + "me/runner/work",
    "user" + "@example.invalid", "someone" + "@users.noreply.github.com",
    "service." + "international", "householdoutcomes",
])
def test_generic_clean(value):
    assert text(value).returncode == 0


def test_denylist_required_and_boundary(tmp_path):
    assert run("text", "--label", "test", data="clean").returncode == 2
    deny = tmp_path / "rules"
    deny.write_text("host\t" + "owl" + "tower\n")
    result = run("text", "--label", "test", "--denylist", str(deny), data="an Owltower here")
    assert result.returncode == 1
    assert "Owltower" not in result.stdout + result.stderr
    assert run("text", "--label", "test", "--denylist", str(deny), data="owltowers").returncode == 0
    deny.write_text("domain\tre:" + "foo[0-9]+\n")
    assert run("text", "--label", "test", "--denylist", str(deny), data="foo42").returncode == 1
    deny.write_text("")
    assert run("text", "--label", "test", "--denylist", str(deny), data="x").returncode == 2


def git(repo, *args):
    return subprocess.check_output(["git", *args], cwd=repo, text=True).strip()


def test_diff_tree_history_and_commits(tmp_path):
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
    assert run("diff", "--generic-only", "--base", base, "--head", "HEAD", cwd=tmp_path).returncode == 0
    assert run("tree", "--generic-only", "--head", "HEAD", cwd=tmp_path).returncode == 1
    assert run("history", "--generic-only", cwd=tmp_path).returncode == 1
    (tmp_path / ("node." + "internal")).write_text("ok\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "contact " + secret)
    result = run("diff", "--generic-only", "--base", base, "--head", "HEAD", cwd=tmp_path)
    assert result.returncode == 1
    assert "node." + "internal" not in result.stdout
    assert run("commits", "--generic-only", "--base", base, "--head", "HEAD", cwd=tmp_path).returncode == 1


def test_environment_denylist_and_invalid_config():
    key = "PUBLIC_VERIFY_DENYLIST"
    secret = "cedar" + "signal"
    result = run("text", "--label", "private", data=secret,
                 env={key: "bus\t" + secret})
    assert result.returncode == 1
    assert secret not in result.stdout + result.stderr
    assert run("text", "--label", "x", data="x", env={key: "unknown\tvalue"}).returncode == 2
    assert run("text", "--label", "x", data="x", env={key: "host\tre:["}).returncode == 2


def test_rename_does_not_rescan_unchanged_lines(tmp_path):
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "test@example.invalid")
    git(tmp_path, "config", "user.name", "Tester")
    (tmp_path / "old").write_text(("10." + "2.3.4\n") * 10)
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "base")
    base = git(tmp_path, "rev-parse", "HEAD")
    git(tmp_path, "mv", "old", "new")
    git(tmp_path, "commit", "-qm", "rename")
    assert run("diff", "--generic-only", "--base", base, "--head", "HEAD", cwd=tmp_path).returncode == 0
