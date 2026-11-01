"""In-memory GitHub double shared by the SDK and dispatcher tests."""
import copy
import itertools


import os as _os
import tempfile as _tempfile

# keep the wake-gate state of tests out of ~/.cache
_os.environ["EXTERNAL_LANE_STATE_DIR"] = _tempfile.mkdtemp(prefix="external-lane-test-")


def issue(n, labels, body="", title=None, updated_at="2026-11-01T00:00:00Z", state="open"):
    return {"number": n, "title": title or f"issue {n}", "body": body, "state": state, "updated_at": updated_at,
            "labels": [{"name": l} for l in labels]}


class FakeGitHub:
    def __init__(self, issues=(), prs=()):
        self.issues = {i["number"]: copy.deepcopy(i) for i in issues}
        self.prs = list(prs)
        self.comments_by = {}
        self.created_prs = []
        self.calls = []
        self._pr_no = itertools.count(9000)

    def list_issues(self, labels, state="open", limit=100):
        want = set(labels)
        return [copy.deepcopy(i) for i in self.issues.values()
                if i["state"] == state and want <= {l["name"] for l in i["labels"]}]

    def get_issue(self, n):
        return copy.deepcopy(self.issues[n])

    def set_labels(self, n, labels):
        self.calls.append(("labels", n, sorted(labels)))
        self.issues[n]["labels"] = [{"name": l} for l in sorted(labels)]

    def comment(self, n, body):
        self.calls.append(("comment", n, body))
        self.comments_by.setdefault(n, []).append(body)

    def create_pr(self, head, title, body, base="main"):
        n = next(self._pr_no)
        self.created_prs.append({"number": n, "head": head, "title": title, "body": body, "base": base})
        return {"number": n, "html_url": f"https://github.com/o/r/pull/{n}"}

    def labels_of(self, n):
        return {l["name"] for l in self.issues[n]["labels"]}
