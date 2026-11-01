"""Tiny GitHub REST client (stdlib only) with rate-limit handling.

Auth: GITHUB_TOKEN or GH_TOKEN from the environment; if neither is set and the `gh` CLI is installed and logged in,
requests go through `gh api`. Tokens never appear in argv or logs.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

API = os.environ.get("GITHUB_API_URL", "https://api.github.com")


class GitHubError(RuntimeError):
    def __init__(self, status, message):
        super().__init__(f"GitHub {status}: {message}")
        self.status = status


def token_from_env(env=None):
    env = os.environ if env is None else env
    return (env.get("GITHUB_TOKEN") or env.get("GH_TOKEN") or "").strip() or None


def rate_limit_wait(status, headers, now=None, attempt=0):
    """Seconds to wait before retrying, or None when the response is not a rate limit.
    Honors Retry-After (secondary limits), X-RateLimit-Reset when Remaining is 0, else exponential backoff."""
    now = time.time() if now is None else now
    headers = {str(k).lower(): v for k, v in (headers or {}).items()}
    if status not in (403, 429):
        return None
    if headers.get("retry-after"):
        try:
            return max(1.0, float(headers["retry-after"]))
        except ValueError:
            pass
    if str(headers.get("x-ratelimit-remaining")) == "0" and headers.get("x-ratelimit-reset"):
        try:
            return max(1.0, float(headers["x-ratelimit-reset"]) - now + 1)
        except ValueError:
            pass
    if status == 429:
        return min(300.0, 30.0 * (2 ** attempt))
    return None  # plain 403 = permission problem, not a rate limit


class GitHub:
    """Minimal REST client. `transport(method, path, payload) -> (status, headers, body)` is injectable for tests."""

    def __init__(self, repo, token=None, transport=None, sleep=time.sleep, min_write_interval=1.0, max_retries=4):
        if repo.count("/") != 1:
            raise ValueError("repo must be owner/name")
        self.repo = repo
        self.token = token if token is not None else token_from_env()
        self.sleep = sleep
        self.min_write_interval = min_write_interval
        self.max_retries = max_retries
        self._last_write = 0.0
        if transport is not None:
            self.transport = transport
        elif self.token:
            self.transport = self._urllib
        elif shutil.which("gh"):
            self.transport = self._gh
        else:
            raise GitHubError(0, "set GITHUB_TOKEN (or GH_TOKEN), or log in to the gh CLI")

    # -- transports -------------------------------------------------------------------------------
    def _urllib(self, method, path, payload=None):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(API + path, data=data, method=method)
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        req.add_header("User-Agent", "openswarm-external-lane")
        req.add_header("Authorization", f"Bearer {self.token}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read().decode() or "null"
                return resp.status, dict(resp.headers), json.loads(raw)
        except urllib.error.HTTPError as error:
            raw = error.read().decode(errors="replace")
            try:
                body = json.loads(raw)
            except ValueError:
                body = {"message": raw[:300]}
            return error.code, dict(error.headers or {}), body

    def _gh(self, method, path, payload=None):
        cmd = ["gh", "api", "-i", "--method", method, path.lstrip("/")]
        if payload is not None:
            cmd += ["--input", "-"]
        proc = subprocess.run(cmd, input=json.dumps(payload) if payload is not None else None,
                              capture_output=True, text=True, timeout=90)
        head, _, body = proc.stdout.replace("\r\n", "\n").partition("\n\n")
        lines = head.splitlines()
        try:
            status = int(lines[0].split()[1])
        except (IndexError, ValueError):
            status = 0 if proc.returncode else 200
        headers = dict(line.split(": ", 1) for line in lines[1:] if ": " in line)
        try:
            data = json.loads(body) if body.strip() else None
        except ValueError:
            data = {"message": (body or proc.stderr)[:300]}
        return status, headers, data

    # -- core -------------------------------------------------------------------------------------
    def request(self, method, path, payload=None):
        if method != "GET":
            gap = self.min_write_interval - (time.time() - self._last_write)
            if gap > 0:
                self.sleep(gap)  # GitHub asks for >=1s between content-creating requests
        for attempt in range(self.max_retries + 1):
            status, headers, body = self.transport(method, path, payload)
            if method != "GET":
                self._last_write = time.time()
            wait = rate_limit_wait(status, headers, attempt=attempt)
            if wait is not None and attempt < self.max_retries:
                self.sleep(min(wait, 900))
                continue
            if 500 <= status < 600 and attempt < self.max_retries:
                self.sleep(2 ** attempt)
                continue
            if status >= 400 or status == 0:
                msg = body.get("message") if isinstance(body, dict) else str(body)[:300]
                raise GitHubError(status, f"{method} {path}: {msg}")
            return body
        raise GitHubError(status, f"{method} {path}: retries exhausted")

    def r(self, suffix):
        return f"/repos/{self.repo}{suffix}"

    # -- helpers used by the agent ----------------------------------------------------------------
    def list_issues(self, labels, state="open", limit=100):
        q = urllib.parse.urlencode({"state": state, "labels": ",".join(labels), "per_page": min(limit, 100),
                                    "sort": "created", "direction": "asc"})
        rows = self.request("GET", self.r(f"/issues?{q}")) or []
        return [i for i in rows if "pull_request" not in i]

    def get_issue(self, n):
        return self.request("GET", self.r(f"/issues/{int(n)}"))

    def set_labels(self, n, labels):
        return self.request("PUT", self.r(f"/issues/{int(n)}/labels"), {"labels": sorted(labels)})

    def comment(self, n, body):
        return self.request("POST", self.r(f"/issues/{int(n)}/comments"), {"body": body})

    def comments(self, n):
        return self.request("GET", self.r(f"/issues/{int(n)}/comments?per_page=100")) or []

    def open_prs(self):
        return self.request("GET", self.r("/pulls?state=open&per_page=100")) or []

    def create_pr(self, head, title, body, base="main"):
        return self.request("POST", self.r("/pulls"), {"head": head, "base": base, "title": title, "body": body})

    def pr_files(self, n):
        return self.request("GET", self.r(f"/pulls/{int(n)}/files?per_page=100")) or []

    def default_branch(self):
        return (self.request("GET", self.r("")) or {}).get("default_branch") or "main"

    def ensure_label(self, name, color="ededed", description=""):
        if self.request_ok("GET", self.r(f"/labels/{urllib.parse.quote(name, safe='')}")):
            return False
        self.request("POST", self.r("/labels"), {"name": name, "color": color, "description": description})
        return True

    def request_ok(self, method, path):
        try:
            self.request(method, path)
            return True
        except GitHubError as error:
            if error.status == 404:
                return False
            raise
