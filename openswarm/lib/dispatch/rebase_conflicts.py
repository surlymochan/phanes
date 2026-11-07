"""Classify git rebase conflicts and rebase a PR head with a policy.

A conflicting PR head is not always a dead end.  A documentation conflict, a file
that both sides added and a whitespace-only difference can be settled
mechanically, while a conflict in the same region of a code file needs a human.
The dispatcher asks this module *before* it spends a lane run on the rebase:

* :func:`rebase_with_policy` rebases a head onto a base in a disposable
  worktree, classifies the conflicts that come up, merges the automatic ones with
  a real union merge (both sides are kept) and continues the rebase;
* a manual conflict ends the run immediately: the report names every conflicted
  file, counts the conflicting lines on *both* sides and says which direction the
  author has to take;
* the conflict scene is never thrown away: the worktree of a manual conflict
  keeps the rebase state and the index, so ``git rebase --continue`` still works
  there.

The module is also a CLI::

    python3 openswarm/lib/dispatch/rebase_conflicts.py . --rebase main --head rebase-pr-1708
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

# Files whose conflicts are merged mechanically: a union merge of prose keeps
# both texts, so a human only has to read the result.
DOC_EXTENSIONS = {".md", ".txt", ".rst", ".adoc", ".html", ".css"}

# Conflict kinds that the policy settles on its own.
AUTOMATIC_KINDS = ("documentation", "addition", "whitespace-only")
# Conflict kinds that need a human.
MANUAL_KINDS = ("same-region-code", "binary", "delete-modify")

_MARKERS = ("<<<<<<<", "|||||||", "=======", ">>>>>>>")


def git(repo, *args, check=True):
    """Run a git command in ``repo``; the editor never blocks a rebase."""
    env = dict(os.environ)
    env.update({"GIT_EDITOR": "true", "GIT_SEQUENCE_EDITOR": "true", "GIT_MERGE_AUTOEDIT": "no"})
    result = subprocess.run(["git", "-C", repo, *args], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, check=False, env=env)
    if check and result.returncode:
        raise RuntimeError(result.stderr.decode("utf-8", "replace").strip())
    return result


def _out(result):
    return result.stdout.decode("utf-8", "replace").strip()


def _stage_blobs(repo, path):
    """Return ``{stage: blob}`` of the unmerged stages of one path."""
    stages = {}
    raw = git(repo, "ls-files", "-u", "-z", "--", path).stdout
    for entry in raw.split(b"\0"):
        if not entry:
            continue
        meta, _ = entry.split(b"\t", 1)
        _mode, oid, stage = meta.decode("ascii").split()
        blob = git(repo, "cat-file", "blob", oid, check=False)
        stages[int(stage)] = blob.stdout if not blob.returncode else b""
    return stages


def _conflict_line_counts(text):
    """Count the conflicting content lines of both sides of a conflicted file.

    The markers themselves are not counted, and the base section of the diff3
    style belongs to neither side.
    """
    ours = theirs = 0
    state = None
    for line in text.splitlines():
        if line.startswith("<<<<<<<"):
            state = "ours"
        elif line.startswith("|||||||"):
            if state == "ours":
                state = "base"
        elif line.startswith("======="):
            if state in ("ours", "base"):
                state = "theirs"
        elif line.startswith(">>>>>>>"):
            state = None
        elif state == "ours":
            ours += 1
        elif state == "theirs":
            theirs += 1
    return ours, theirs


def _changed_lines(repo, first, second):
    """The number of lines that differ between two blobs."""
    if first is None or second is None:
        return 0
    result = git(repo, "diff", "--numstat", first, second, check=False)
    if result.returncode:
        return 0
    for line in _out(result).splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            return int(parts[0]) + int(parts[1])
    return 0


def _kind_of(stages, blobs, binary, ext):
    """The conflict kind of one path, from its index stages."""
    if binary:
        return "binary"
    have = set(stages)
    if 1 not in have:
        # Both sides added the file (add/add) is mechanical; a single stage means
        # that one side added a file the other side deleted: that needs a human.
        return "addition" if have == {2, 3} else "delete-modify"
    if 2 not in have or 3 not in have:
        # Deleted on one side, changed on the other.
        return "delete-modify"
    if _same_without_whitespace(blobs.get(2, b""), blobs.get(3, b"")):
        return "whitespace-only"
    if ext in DOC_EXTENSIONS:
        return "documentation"
    return "same-region-code"


def _same_without_whitespace(first, second):
    return b"".join(first.split()) == b"".join(second.split())


def conflict_entries(repo):
    """Return the conflicted paths with their kind and their conflicting lines."""
    raw = git(repo, "ls-files", "-u", "-z").stdout
    stages = {}
    for entry in raw.split(b"\0"):
        if not entry:
            continue
        meta, path = entry.split(b"\t", 1)
        mode, oid, stage = meta.decode("ascii").split()
        stages.setdefault(os.fsdecode(path), {})[int(stage)] = (mode, oid)
    conflicts = []
    for path, entries in sorted(stages.items()):
        blobs = {}
        for stage, (_mode, oid) in entries.items():
            blob = git(repo, "cat-file", "blob", oid, check=False)
            blobs[stage] = blob.stdout if not blob.returncode else b""
        binary = any(b"\0" in data for data in blobs.values())
        working = os.path.join(repo, path)
        text = ""
        if os.path.isfile(working):
            with open(working, "rb") as stream:
                raw_work = stream.read()
            if b"\0" in raw_work:
                binary = True
            else:
                text = raw_work.decode("utf-8", "replace")
        ours, theirs = _conflict_line_counts(text) if text else (0, 0)
        if not ours and not theirs:
            # A delete against a modify has no markers: the changed lines of the
            # side that still exists are the conflicting lines.
            ours = _changed_lines(repo, entries.get(1, (None, None))[1], entries.get(2, (None, None))[1]) \
                if 2 in entries else 0
            theirs = _changed_lines(repo, entries.get(1, (None, None))[1], entries.get(3, (None, None))[1]) \
                if 3 in entries else 0
        ext = os.path.splitext(path)[1].lower()
        conflicts.append({"path": path, "kind": _kind_of(entries, blobs, binary, ext),
                          "stages": sorted(entries), "conflict_lines": ours + theirs,
                          "upstream_lines": ours, "pr_lines": theirs})
    return conflicts


def suggest_direction(conflicts):
    """The direction a human has to take, decided from the conflict facts.

    A file that main deleted cannot be patched by the author alone, and a
    conflict whose bulk sits on the main side means the baseline is still
    moving: in both cases main has to land first.  Everything else is the
    author's own region and is redone on the new baseline.
    """
    manual = [c for c in conflicts if c["kind"] in MANUAL_KINDS]
    if not manual:
        return ""
    upstream = sum(c["upstream_lines"] for c in manual)
    pr = sum(c["pr_lines"] for c in manual)
    deleted = [c["path"] for c in manual if c["kind"] == "delete-modify"]
    if deleted:
        return ("等 main 侧先合：main 已删除 {}，本 PR 的改动要按新基线重做或放弃".format(
            ", ".join(deleted[:3])))
    if upstream >= 2 * pr and upstream >= 20:
        return ("等 main 侧先合：冲突主要来自 main 侧的大范围改动（upstream {} 行 vs PR {} 行），"
                "基线稳定后再重放本 PR".format(upstream, pr))
    return ("作者侧重做：冲突集中在本 PR 改动的区域（upstream {} 行 vs PR {} 行），"
            "按最新 main 基线重新整理本 PR 的改动".format(upstream, pr))


def classify_entries(conflicts):
    """Classify conflicts that were already read from the index."""
    kinds = {c["kind"] for c in conflicts}
    automatic = bool(conflicts) and kinds <= set(AUTOMATIC_KINDS)
    return {"classification": "automatic" if automatic else "manual", "conflicts": conflicts,
            "suggested_direction": suggest_direction(conflicts)}


def classify(repo):
    """Classify the conflicts of a repository that is in the middle of a rebase."""
    return classify_entries(conflict_entries(repo))


def union_content(repo, path, stages):
    """Merge both sides of a conflict with a real union merge.

    ``git merge-file --union`` keeps the content of *both* sides of every
    conflicting hunk, so no line of either side is dropped -- not even a line
    that appears twice in the same file.
    """
    ours = stages.get(2, b"")
    base = stages.get(1, b"")
    theirs = stages.get(3, b"")
    scratch = tempfile.mkdtemp(prefix="openswarm-union-")
    try:
        names = []
        for index, data in enumerate((ours, base, theirs)):
            name = os.path.join(scratch, str(index))
            with open(name, "wb") as stream:
                stream.write(data)
            names.append(name)
        merged = subprocess.run(["git", "merge-file", "--union", "-p", names[0], names[1], names[2]],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        if merged.returncode not in (0, 1):
            return None
        return merged.stdout
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _has_markers(data):
    for line in data.decode("utf-8", "replace").splitlines():
        if line.startswith("<<<<<<<"):
            return True
    return False


def resolve_conflicts(repo, conflicts):
    """Resolve the automatic conflicts and stage them.

    Returns ``(resolved, unresolved)``: a path that cannot be merged is left
    untouched, so the caller can report it as a manual conflict.
    """
    resolved, unresolved = [], []
    for item in conflicts:
        if item["kind"] not in AUTOMATIC_KINDS:
            unresolved.append(item["path"])
            continue
        stages = _stage_blobs(repo, item["path"])
        if item["kind"] == "whitespace-only":
            # Both sides are the same text modulo whitespace: main wins.
            data = stages.get(2) or stages.get(3) or b""
        else:
            data = union_content(repo, item["path"], stages)
        if not data or _has_markers(data):
            unresolved.append(item["path"])
            continue
        with open(os.path.join(repo, item["path"]), "wb") as stream:
            stream.write(data)
        git(repo, "add", "--", item["path"])
        resolved.append(item["path"])
    return resolved, unresolved


def resolve_document_union(repo, conflicts):
    """Resolve eligible document and addition conflicts, returning staged paths."""
    eligible = [item for item in conflicts if item["kind"] in {
        "documentation", "documentation-addition", "addition"
    }]
    resolved, _ = resolve_conflicts(repo, eligible)
    return resolved


def preserve_scene(repo, dest=None):
    """Copy the conflicted worktree into a scene that can still be worked on.

    The copy holds the whole working tree (not only the conflicted files), the
    conflicted index and the rebase state, and it shares the objects with the
    original repository, so ``git -C <scene> status`` shows the conflict and
    ``git -C <scene> rebase --continue`` can carry it on.
    """
    repo = os.path.abspath(repo)
    dest = os.path.abspath(dest or os.path.join(
        repo, ".openswarm-rebase-scene-" + time.strftime("%Y%m%d-%H%M%S")))
    if os.path.exists(dest):
        raise RuntimeError("conflict scene exists: " + dest)
    os.makedirs(dest)
    for name in sorted(os.listdir(repo)):
        if name == ".git" or name.startswith(".openswarm-rebase-"):
            continue
        src, dst = os.path.join(repo, name), os.path.join(dest, name)
        if os.path.isdir(src) and not os.path.islink(src):
            shutil.copytree(src, dst, symlinks=True)
        else:
            shutil.copy2(src, dst, follow_symlinks=False)
    gitdir = os.path.join(dest, ".git")
    os.makedirs(os.path.join(gitdir, "objects", "info"), exist_ok=True)
    os.makedirs(os.path.join(gitdir, "refs", "heads"), exist_ok=True)
    common = _out(git(repo, "rev-parse", "--git-common-dir"))
    if not os.path.isabs(common):
        common = os.path.join(repo, common)
    with open(os.path.join(gitdir, "objects", "info", "alternates"), "w") as stream:
        stream.write(os.path.join(common, "objects") + "\n")
    with open(os.path.join(gitdir, "HEAD"), "w") as stream:
        stream.write(_out(git(repo, "rev-parse", "HEAD")) + "\n")
    with open(os.path.join(gitdir, "config"), "w") as stream:
        stream.write("[core]\n\trepositoryformatversion = 0\n\tbare = false\n")
    source = _out(git(repo, "rev-parse", "--absolute-git-dir"))
    index = os.path.join(source, "index")
    if os.path.exists(index):
        shutil.copy2(index, os.path.join(gitdir, "index"))
    for state in ("rebase-merge", "rebase-apply"):
        src = os.path.join(source, state)
        if os.path.isdir(src):
            shutil.copytree(src, os.path.join(gitdir, state))
    return dest


def _rebase(worktree, base, extra=()):
    return git(worktree, "rebase", *(list(extra) + [base]), check=False)


def _settle(worktree, base, conflicts, note, depth=0):
    """Classify the conflicts of a stopped rebase and settle the automatic ones."""
    result = classify_entries(conflicts)
    note("rebase conflict [{}]: {}".format(
        result["classification"], ", ".join("{}:{}".format(c["path"], c["kind"]) for c in conflicts)))
    if result["classification"] == "automatic" and depth < 20:
        if all(c["kind"] == "whitespace-only" for c in conflicts):
            # Whitespace only: the rebase is retried with the whitespace ignored.
            git(worktree, "rebase", "--abort", check=False)
            retry = _rebase(worktree, base, ("-Xignore-space-change",))
            if retry.returncode == 0:
                head = _out(git(worktree, "rev-parse", "HEAD"))
                note("whitespace-only conflict: rebase retried with -Xignore-space-change")
                return {"status": "rebased", "head": head, "conflicts": [], "classification": "automatic",
                        "suggested_direction": "", "message": "rebase finished (whitespace ignored)"}
            conflicts = conflict_entries(worktree)
            result = classify_entries(conflicts)
        if result["classification"] == "automatic":
            resolved, unresolved = resolve_conflicts(worktree, conflicts)
            note("union merge resolved {} conflict(s): {}".format(len(resolved), ", ".join(resolved) or "-"))
            if not unresolved:
                return _continue(worktree, base, note, depth + 1)
            conflicts = conflict_entries(worktree)
            result = classify_entries(conflicts)
    return {"status": "manual", "conflicts": conflicts, "classification": "manual",
            "suggested_direction": result["suggested_direction"],
            "message": "the conflicts need a human"}


def _continue(worktree, base, note, depth):
    """Continue a rebase whose conflicts were resolved; stop at the next one."""
    while depth < 40:
        run = git(worktree, "rebase", "--continue", check=False)
        if run.returncode == 0:
            head = _out(git(worktree, "rev-parse", "HEAD"))
            note("rebase continued: " + head[:10])
            return {"status": "rebased", "head": head, "conflicts": [], "classification": "automatic",
                    "suggested_direction": "", "message": "rebase finished"}
        conflicts = conflict_entries(worktree)
        if not conflicts:
            text = (run.stdout or b"").decode("utf-8", "replace") + (run.stderr or b"").decode("utf-8", "replace")
            if "nothing to commit" in text or "no changes" in text:
                git(worktree, "rebase", "--skip", check=False)
                depth += 1
                continue
            note("rebase stopped: " + text.strip()[:160])
            return {"status": "failed", "error": text.strip()[:400], "conflicts": [],
                    "classification": "manual", "suggested_direction": "",
                    "message": text.strip()[:400]}
        return _settle(worktree, base, conflicts, note, depth + 1)
    return {"status": "failed", "error": "the rebase did not settle", "conflicts": [],
            "classification": "manual", "suggested_direction": "", "message": "the rebase did not settle"}


def rebase_with_policy(repo, base, head=None, log=None, scene_root=None, in_place=False):
    """Rebase ``head`` onto ``base`` and settle the conflicts with the policy.

    The report that comes back carries a ``status``:

    * ``rebased``: the rebase finished, ``head`` is the new commit;
    * ``manual``: a conflict needs a human -- ``conflicts`` names every file,
      ``suggested_direction`` says what to do and ``worktree`` is the conflict
      scene, where the rebase can be continued;
    * ``failed``: the rebase broke for another reason.

    With ``in_place`` the rebase runs in ``repo`` itself (the lane rebases in its
    own worktree); otherwise a disposable worktree is created for it and the
    conflict scene of a manual conflict is that worktree.
    """
    repo = os.path.abspath(repo)
    events = []

    def note(message):
        events.append(message)
        if log:
            log(message)

    created = False
    if in_place:
        worktree = repo
    else:
        root = os.path.abspath(scene_root or tempfile.gettempdir())
        try:
            os.makedirs(root, exist_ok=True)
        except OSError:
            root = tempfile.gettempdir()
        worktree = os.path.join(root, "rebase-{}-{}".format(time.strftime("%Y%m%d-%H%M%S"), os.getpid()))
        add = git(repo, "worktree", "add", "--detach", worktree, head, check=False)
        if add.returncode:
            message = (add.stderr or add.stdout).decode("utf-8", "replace").strip()[:400]
            note("the rebase worktree could not be created: " + message[:160])
            return {"status": "failed", "error": message, "conflicts": [], "classification": "manual",
                    "suggested_direction": "", "events": events, "worktree": ""}
        created = True
    run = _rebase(worktree, base)
    if run.returncode == 0:
        head_sha = _out(git(worktree, "rev-parse", "HEAD"))
        note("rebase onto {} finished: {}".format(base[:10], head_sha[:10]))
        report = {"status": "rebased", "head": head_sha, "conflicts": [], "classification": "automatic",
                  "suggested_direction": "", "message": "rebase onto {} finished".format(base[:10]),
                  "events": events}
    else:
        conflicts = conflict_entries(worktree)
        if not conflicts:
            message = (run.stderr or run.stdout).decode("utf-8", "replace").strip()[:400]
            note("the rebase failed without a conflict: " + message[:160])
            report = {"status": "failed", "error": message, "conflicts": [], "classification": "manual",
                      "suggested_direction": "", "message": message, "events": events}
        else:
            report = _settle(worktree, base, conflicts, note)
            report["events"] = events
    if report["status"] == "rebased" and created:
        git(repo, "worktree", "remove", "--force", worktree, check=False)
        report["worktree"] = ""
    else:
        # The conflict scene stays where it is: the rebase can be continued there.
        report["worktree"] = worktree
        note("the conflict scene is kept at " + worktree)
    return report


def format_report(report):
    """The human readable report of a rebase policy run."""
    lines = []
    conflicts = report.get("conflicts") or []
    for item in conflicts:
        lines.append("- `{}` [{}] 冲突行数：upstream {} / PR {}".format(
            item["path"], item["kind"], item.get("upstream_lines", 0), item.get("pr_lines", 0)))
    if not lines:
        lines.append("- {}".format(report.get("message") or report.get("error") or "no conflict"))
    out = ["rebase 冲突（{}）:".format(report.get("classification") or report.get("status"))]
    out.extend(lines)
    if report.get("suggested_direction"):
        out.append("建议方向：" + report["suggested_direction"])
    if report.get("worktree"):
        out.append("冲突现场（可继续 `git rebase --continue`）：" + report["worktree"])
    return "\n".join(out)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo", nargs="?", default=".", help="the repository (default: .)")
    parser.add_argument("--rebase", metavar="BASE", help="rebase onto BASE with the conflict policy")
    parser.add_argument("--head", default="HEAD", help="the head to rebase (default: HEAD)")
    parser.add_argument("--in-place", action="store_true",
                        help="rebase in the given repository instead of a disposable worktree")
    parser.add_argument("--scene", action="store_true", help="keep a copy of the conflict scene")
    args = parser.parse_args(argv)
    repo = os.path.abspath(args.repo)
    if args.rebase:
        report = rebase_with_policy(repo, args.rebase, None if args.in_place else args.head,
                                     in_place=args.in_place)
    else:
        report = classify(repo)
        if args.scene:
            report["scene"] = preserve_scene(repo)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    status = report.get("status") or report.get("classification")
    return 0 if status in ("rebased", "automatic") else 1


if __name__ == "__main__":
    sys.exit(main())
