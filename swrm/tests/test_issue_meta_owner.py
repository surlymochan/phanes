import importlib.util
from pathlib import Path


spec = importlib.util.spec_from_file_location(
    "issue_meta_owner", Path(__file__).resolve().parents[1] / "lib/dispatch/issue_meta.py"
)
issue_meta = importlib.util.module_from_spec(spec)
spec.loader.exec_module(issue_meta)
IssueMeta = issue_meta.IssueMeta


def test_repo_owner_from_repository():
    assert IssueMeta("example-owner/example-repo").repo_owner() == "example-owner"


def test_repo_owner_from_empty_repository():
    assert IssueMeta("").repo_owner() == ""
