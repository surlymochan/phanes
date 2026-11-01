"""Agent-side library: claim -> branch -> PR, or fail with a reason. See README.md for the contract."""
from __future__ import annotations

import base64
import os
import re
import subprocess
import tempfile

from . import contract as C
from .github import GitHub, token_from_env

PRIO = {"p0": 0, "p1": 1, "p2": 2}


def _prio(issue):
    return min([PRIO[l] for l in C.label_names(issue) if l in PRIO] or [3]), int(issue["number"])


def git_auth_env(token, base=None):
    """Pass the token to git via GIT_CONFIG_* env (git >= 2.31): never in argv, never written to .git/config."""
    env = dict(os.environ if base is None else base)
    if token:
        cred = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        n = int(env.get("GIT_CONFIG_COUNT") or 0)
        env.update({"GIT_CONFIG_COUNT": str(n + 1), f"GIT_CONFIG_KEY_{n}": "http.https://github.com/.extraheader",
                    f"GIT_CONFIG_VALUE_{n}": f"AUTHORIZATION: basic {cred}"})
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    return env


class LaneError(RuntimeError):
    pass


class ExternalLane:
    def __init__(self, repo, agent, max_inflight=1, gh=None, token=None, runner=subprocess.run, log=print):
        self.repo = repo
        self.agent = C.check_agent(agent)
        self.label = C.backend_label(self.agent)
        self.max_inflight = max(1, int(max_inflight))
        self.token = token if token is not None else token_from_env()
        self.gh = gh or GitHub(repo, token=self.token)
        self.run = runner
        self.log = log
        self.remote_url = f"https://github.com/{repo}.git"

    # ---- queue -----------------------------------------------------------------------------------
    def inflight(self):
        """Issues this agent holds (claimed, no PR yet)."""
        return sorted(self.gh.list_issues([self.label, C.CLAIMED]), key=_prio)

    def poll(self):
        return sorted((i for i in self.gh.list_issues([self.label, C.READY]) if C.claimable(i, self.agent)), key=_prio)

    def claim(self, n):
        issue = self.gh.get_issue(n)  # labels from the issue itself: list endpoints can lag label edits
        if not C.claimable(issue, self.agent):
            return None
        self.gh.set_labels(n, C.claim_labels(C.label_names(issue)))
        self.gh.comment(n, C.claim_comment(self.agent))
        self.log(f"claimed #{n}")
        return self.gh.get_issue(n)

    def claim_next(self):
        if len(self.inflight()) >= self.max_inflight:
            return None
        for issue in self.poll():
            got = self.claim(issue["number"])
            if got:
                return got
        return None

    # ---- outcome ---------------------------------------------------------------------------------
    def fail(self, n, reason):
        issue = self.gh.get_issue(n)
        self.gh.set_labels(n, C.failed_labels(C.label_names(issue)))
        self.gh.comment(n, f"external lane **{self.agent}** gave up: {str(reason).strip()[:3000]}")
        self.log(f"failed #{n}: {reason}")

    def branch_for(self, issue):
        """(branch, rework_pr). Rework issues (`重基PR：#P branch` / `Rework PR: #P branch`) reuse the PR branch."""
        rw = C.rework_target(issue.get("body") or "")
        if rw:
            return rw[1], rw[0]
        return C.branch_name(self.agent, issue["number"]), None

    def open_pr(self, n, branch, title, summary="", files=None, rework_pr=None):
        """After the branch is pushed. `files` ([{filename, additions, deletions}]) enables the thin-diff gate."""
        if files is not None:
            why = C.thin_reason(files)
            if why:
                self.fail(n, f"thin diff ({why}); the contract needs >= {C.MIN_EFFECTIVE_LINES} effective code/test lines")
                return None
        issue = self.gh.get_issue(n)
        if rework_pr:
            self.gh.comment(rework_pr, f"external lane **{self.agent}** pushed a rework to `{branch}` for #{n}. {summary}".strip())
            url = f"https://github.com/{self.repo}/pull/{rework_pr}"
        else:
            pr = self.gh.create_pr(branch, (title or f"#{n}")[:200], C.pr_body(n, self.agent, summary))
            url = pr.get("html_url") if isinstance(pr, dict) else str(pr)
        self.gh.set_labels(n, C.pr_opened_labels(C.label_names(issue)))
        self.log(f"#{n} -> {url}")
        return url

    # ---- git helpers -----------------------------------------------------------------------------
    def git(self, *args, cwd=None, check=True):
        proc = self.run(["git", *args], cwd=cwd, env=git_auth_env(self.token), capture_output=True, text=True)
        if check and proc.returncode:
            raise LaneError(f"git {args[0]} failed: {(proc.stderr or proc.stdout).strip()[-400:]}")
        return proc.stdout

    def checkout(self, workdir, branch, rework=False, base="main"):
        if not os.path.isdir(os.path.join(workdir, ".git")):
            self.git("clone", "-q", self.remote_url, workdir)
        self.git("fetch", "-q", "origin", base, cwd=workdir)
        if rework:
            self.git("fetch", "-q", "origin", f"+{branch}:refs/remotes/origin/{branch}", cwd=workdir)
            self.git("checkout", "-q", "-B", branch, f"origin/{branch}", cwd=workdir)
        else:
            self.git("checkout", "-q", "-B", branch, f"origin/{base}", cwd=workdir)

    def numstat(self, workdir, base="main"):
        out = self.git("diff", "--numstat", f"origin/{base}...HEAD", cwd=workdir)
        rows = []
        for line in out.splitlines():
            a, d, path = (line.split("\t") + ["", "", ""])[:3]
            rows.append({"filename": path, "additions": 0 if a == "-" else int(a or 0),
                         "deletions": 0 if d == "-" else int(d or 0)})
        return rows

    def push(self, workdir, branch, force=False):
        self.git("push", "-q", *(["--force-with-lease"] if force else []), "origin", f"HEAD:refs/heads/{branch}", cwd=workdir)

    # ---- one full turn ---------------------------------------------------------------------------
    def run_once(self, workdir, work_cmd, run_verify=True):
        """Claim one issue, run `work_cmd` in a fresh branch, verify, push, open the PR. Returns the PR url or None."""
        issue = self.claim_next()
        if not issue:
            return None
        n = issue["number"]
        branch, rework_pr = self.branch_for(issue)
        try:
            self.checkout(workdir, branch, rework=bool(rework_pr))
            head0 = self.git("rev-parse", "HEAD", cwd=workdir).strip()
            with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as fh:
                fh.write(f"# #{n} {issue.get('title')}\n\n{issue.get('body') or ''}\n")
                body_file = fh.name
            env = dict(os.environ, ISSUE_NUMBER=str(n), ISSUE_TITLE=str(issue.get("title") or ""),
                       ISSUE_BODY_FILE=body_file, LANE_BRANCH=branch, LANE_REPO=self.repo, LANE_AGENT=self.agent)
            proc = self.run(work_cmd, shell=True, cwd=workdir, env=env, capture_output=True, text=True)
            os.unlink(body_file)
            if proc.returncode:
                return self.fail(n, f"work command exited {proc.returncode}: {(proc.stderr or proc.stdout).strip()[-800:]}")
            if self.git("rev-parse", "HEAD", cwd=workdir).strip() == head0:
                return self.fail(n, "work command made no commit")
            verify = C.verify_command(issue.get("body") or "")
            if run_verify and verify:
                v = self.run(verify, shell=True, cwd=workdir, capture_output=True, text=True)
                if v.returncode:
                    return self.fail(n, f"acceptance `{verify}` failed: {(v.stderr or v.stdout).strip()[-800:]}")
            files = self.numstat(workdir)
            why = C.thin_reason(files)
            if why:
                return self.fail(n, f"thin diff ({why})")
            self.push(workdir, branch, force=bool(rework_pr))
            title = self.git("log", "-1", "--format=%s", cwd=workdir).strip()
            return self.open_pr(n, branch, title, rework_pr=rework_pr)
        except Exception as error:  # never leave a claim dangling silently
            self.fail(n, f"{type(error).__name__}: {re.sub(r'(?i)(token|authorization)[^ ]*', '[redacted]', str(error))[:800]}")
            return None
