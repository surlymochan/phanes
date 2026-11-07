#!/usr/bin/env python3
"""Claim metadata of a dispatched issue (Refs #1761).

A lane that takes a card leaves two traces on the issue itself:

* an **assignee** — the claimer.  Today that is the repo owner, later the OpenBot bot
  name; the login comes from the configuration, never from the code.
* the **lane id in the project's ``Lane`` field** (a TEXT field), so the board shows
  which agent is actually working on the card.

Both writes are best effort: a rate limit, a missing ``project`` scope or any other
``gh`` error is logged and retried once on the next dispatcher tick, and it never blocks
the dispatch itself.  The field is resolved **by name** (``Lane``) — its id is looked up
at runtime and never hardcoded.

The class is deliberately free of GitHub specifics: the caller injects the command
runner, so the whole module is testable offline.
"""
import json
import threading
import time


class IssueMeta:
    """Write the assignee and the project ``Lane`` field of a claimed issue."""

    # gh reports the GraphQL type of a field: "ProjectV2Field" is a text field,
    # "ProjectV2SingleSelectField" a single select one.  The short names are accepted too.
    FIELD_KINDS = {
        "text": "TEXT", "projectv2field": "TEXT",
        "single_select": "SINGLE_SELECT", "projectv2singleselectfield": "SINGLE_SELECT",
    }

    def __init__(self, repo, cfg=None, runner=None, log=None, plan=None, dry_run=False, clock=None):
        cfg = dict(cfg or {})
        self.repo = repo or ""
        self.cfg = cfg
        self.runner = runner or (lambda cmd, timeout=60: (0, "", ""))
        self.log = log or (lambda msg, brain=True: None)
        self.plan = plan or (lambda entry: None)
        self.dry_run = bool(dry_run)
        self.clock = clock or time.time
        self.enabled = bool(cfg.get("enabled", True))
        self.assignee = str(cfg.get("assignee") or "").strip() or self.repo_owner()
        self.project_number = int(cfg.get("project_number") or 0)
        self.project_owner = str(cfg.get("project_owner") or "").strip() or self.repo_owner()
        self.lane_field = str(cfg.get("lane_field") or "Lane").strip() or "Lane"
        self.item_limit = int(cfg.get("item_limit") or 200)
        self.cache_ttl_s = float(cfg.get("cache_ttl_s") or 300)
        self.fail_ttl_s = float(cfg.get("fail_ttl_s") or 60)
        self._guard = threading.Lock()
        self._locks = {}          # issue number -> lock: one writer per issue
        self._project = None      # {"id": project node id, "fields": [...]}
        self._project_at = 0.0
        self._project_fail_at = 0.0
        self._items = {}          # issue number -> project item id

    # ------------------------------------------------------------ helpers
    def repo_owner(self):
        return (self.repo or "").split("/")[0].strip()

    def issue_url(self, n):
        return f"https://github.com/{self.repo}/issues/{n}"

    @staticmethod
    def _wrapped(data, key):
        """The array of a gh json answer.

        gh wraps the arrays: ``field-list`` answers ``{"fields": [...]}`` and
        ``item-list`` answers ``{"items": [...]}``.  A bare array is accepted as well.
        """
        if isinstance(data, dict):
            value = data.get(key)
            return value if isinstance(value, list) else []
        return data if isinstance(data, list) else []

    def _lock(self, n):
        """One writer per issue: two lanes must not race on the same field."""
        with self._guard:
            lock = self._locks.get(n)
            if lock is None:
                lock = self._locks[n] = threading.Lock()
            return lock

    def _gh(self, args, timeout=60):
        """Run a gh command.  A broken gh never raises here."""
        cmd = ["gh"] + [str(a) for a in args]
        try:
            rc, out, err = self.runner(cmd, timeout=timeout)
        except Exception as e:
            return 1, "", f"{e!r}"
        try:
            rc = int(rc)
        except (TypeError, ValueError):
            rc = 1
        return rc, out or "", err or ""

    # ------------------------------------------------------------ project lookups
    def _project_meta(self):
        """The project node id and its fields (cached for ``cache_ttl_s``).

        ``gh project view --format json`` returns the project itself — its ``fields`` is
        a count, not the field list — so the field definitions come from
        ``gh project field-list --format json``, which answers ``{"fields": [...]}``.
        """
        now = self.clock()
        if self._project is not None and now - self._project_at < self.cache_ttl_s:
            return self._project
        if self._project is None and now - self._project_fail_at < self.fail_ttl_s:
            return None                     # a recent failure: do not hammer gh
        pid = ""
        rc, out, err = self._gh(["project", "view", str(self.project_number),
                                 "--owner", self.project_owner, "--format", "json"])
        if rc:
            self.log(f"project view {self.project_number} of {self.project_owner} failed: "
                     f"{(err or out or '').strip()[-160:]}", brain=False)
        else:
            try:
                pid = str((json.loads(out) or {}).get("id") or "")
            except ValueError:
                pid = ""
        fields = []
        rc, out, err = self._gh(["project", "field-list", str(self.project_number),
                                 "--owner", self.project_owner, "--format", "json"])
        if rc:
            self.log(f"project field-list {self.project_number} failed: "
                     f"{(err or out or '').strip()[-160:]}", brain=False)
        else:
            try:
                fields = self._wrapped(json.loads(out), "fields")
            except ValueError:
                fields = []
        if not pid or not fields:
            self._project_fail_at = self.clock()
            return None                     # not cached: the next dispatch tries again
        self._project = {"id": pid, "fields": fields}
        self._project_at = self.clock()
        return self._project

    def _field(self, meta):
        """The project field that carries the lane id, resolved by name."""
        name = self.lane_field.casefold()
        for field in (meta or {}).get("fields") or []:
            if not isinstance(field, dict):
                continue
            if str(field.get("name") or "").strip().casefold() == name:
                return field
        return None

    def _field_kind(self, field):
        """``TEXT`` or ``SINGLE_SELECT`` — gh names the GraphQL type of the field."""
        raw = str((field or {}).get("type") or "").strip()
        return self.FIELD_KINDS.get(raw.casefold(), raw.upper())

    def _content_repo(self, content):
        """``owner/name`` of the repository the item belongs to (empty when unknown).

        gh answers ``"repository": "owner/name"`` for an issue item.
        """
        repo = content.get("repository")
        if isinstance(repo, dict):
            owner = repo.get("owner") or {}
            name = str(repo.get("name") or "").strip()
            login = str(owner.get("login") or owner.get("name") or "").strip()
            return f"{login}/{name}" if name and login else ""
        return str(repo or "").strip()

    def _same_issue(self, content, n, url):
        """Is this item *our* issue?

        The number alone is not enough: a project holds the issues of many repositories
        and #7 of another repo is not our #7.  The full url decides; the number counts
        only together with the repository of the item.
        """
        if str(content.get("url") or "").rstrip("/").casefold() == url.rstrip("/").casefold():
            return True
        if str(content.get("number")) != str(n):
            return False
        repo = self._content_repo(content)
        return bool(repo) and repo.casefold() == (self.repo or "").casefold()

    def _find_item(self, n, url):
        """The project item id of the issue, or None when the issue is not on the board."""
        rc, out, err = self._gh(["project", "item-list", str(self.project_number),
                                 "--owner", self.project_owner, "--format", "json",
                                 "--limit", str(self.item_limit)])
        if rc:
            self.log(f"project item-list {self.project_number} failed: "
                     f"{(err or out or '').strip()[-160:]}", brain=False)
            return None
        try:
            items = self._wrapped(json.loads(out), "items")
        except ValueError:
            return None
        for item in items:
            if not isinstance(item, dict):
                continue
            content = item.get("content") or {}
            if isinstance(content, dict) and self._same_issue(content, n, url) and item.get("id"):
                return item["id"]
        return None

    def _item_id(self, n):
        """The project item id of the issue; the issue joins the board when it is missing."""
        if n in self._items:
            return self._items[n]
        url = self.issue_url(n)
        item = self._find_item(n, url)
        if not item:
            rc, out, err = self._gh(["project", "item-add", str(self.project_number),
                                     "--owner", self.project_owner, "--url", url, "--format", "json"])
            if rc:
                self.log(f"project item-add #{n} failed: {(err or out or '').strip()[-160:]}", brain=False)
                item = self._find_item(n, url)   # it may have been on the board all along
            else:
                try:
                    data = json.loads(out) or {}
                except ValueError:
                    data = {}
                item = data.get("id") or self._find_item(n, url)
        if item:
            self._items[n] = item
        return item

    # ------------------------------------------------------------ writes
    def _assign(self, n, login):
        """Add the assignee; the assignees already on the issue are kept."""
        rc, out, err = self._gh(["issue", "edit", str(n), "--add-assignee", login, "-R", self.repo])
        if rc:
            return False, (err or out or "").strip()[-160:] or f"rc={rc}"
        return True, ""

    def _set_lane(self, n, lane):
        """Set the project's ``Lane`` field of the issue to the lane id."""
        meta = self._project_meta()
        if not meta:
            return False, f"project {self.project_number} of {self.project_owner} not readable"
        field = self._field(meta)
        if not field:
            return False, f"project field {self.lane_field!r} not found"
        item = self._item_id(n)
        if not item:
            return False, "no project item for the issue"
        args = ["project", "item-edit", "--id", item, "--field-id", str(field.get("id")),
                "--project-id", str(meta["id"])]
        ftype = self._field_kind(field)
        if ftype == "TEXT":
            args += ["--text", lane]
        elif ftype == "SINGLE_SELECT":
            option = next((o for o in field.get("options") or []
                           if str(o.get("name") or "").strip().casefold() == lane.strip().casefold()), None)
            if not option or not option.get("id"):
                return False, f"project field {self.lane_field!r} has no option {lane!r}"
            args += ["--single-select-option-id", str(option["id"])]
        else:
            return False, f"project field {self.lane_field!r} has unsupported type {ftype or 'unknown'}"
        rc, out, err = self._gh(args)
        if rc:
            return False, (err or out or "").strip()[-160:] or f"rc={rc}"
        return True, ""

    # ------------------------------------------------------------ public
    def claim(self, n, lane, assignee=True):
        """Stamp the claim on the issue.  Returns ``{"ok": bool, "errors": [...]}``.

        Never raises: a failed write is reported so the caller can retry it later.
        """
        if not self.enabled or not n:
            return {"ok": True, "errors": []}
        lane = str(lane or "").strip()
        if self.dry_run:
            self.plan({"would": "issue-meta",
                       "cmd": ["gh", "issue", "edit", str(n), "--add-assignee", self.assignee, "-R", self.repo]})
            self.plan({"would": "issue-meta",
                       "cmd": ["gh", "project", "item-edit", str(self.project_number), "--owner", self.project_owner,
                               "--url", self.issue_url(n), "--field", self.lane_field, "--text", lane]})
            return {"ok": True, "errors": []}
        errors = []
        with self._lock(n):                  # one writer per issue: no lost updates
            if assignee and self.assignee:
                ok, err = self._assign(n, self.assignee)
                if not ok:
                    errors.append(f"assignee {self.assignee}: {err}")
            if lane and self.project_number:
                ok, err = self._set_lane(n, lane)
                if not ok:
                    errors.append(f"lane {lane}: {err}")
        return {"ok": not errors, "errors": errors}
