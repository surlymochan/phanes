"""Run external dispatch task sources (Refs #1449).

A source hook is an external command that prints one JSON task per stdout line::

    {"issue": 1449, "priority": "p1", "backend": null, "reason": "phanes queue"}

The command is an argv list and is run without a shell, so the caller keeps
control over the executable and its arguments.  A line that is not JSON, or that
does not carry the four protocol fields with the right types, is dropped and
counted as ``rejected``.

``run_hooks`` never raises.  A hook that times out, exits non-zero or breaks in
any other way is reported in ``errors`` and the other hooks still run.  When
several hooks report the same issue the highest priority wins (p0 before p1
before p2 before no priority at all) and the reasons are merged.

The hook configuration is a dict per hook::

    {"name": "phanes", "argv": ["phanes", "queue", "--json"], "pause_scope": "mint"}

``pause_scope`` is the scope the hook belongs to (``"mint"`` or ``None``).  When
``is_paused`` reports that scope as paused, the hook is skipped: a paused scope
must not spawn new work.
"""
import json
import subprocess


PRIORITIES = ("p0", "p1", "p2")
_PRIORITY = {"p0": 0, "p1": 1, "p2": 2, None: 3}
_TASK_FIELDS = ("issue", "priority", "backend", "reason")
_STDERR_TAIL = 300


def _valid_task(value):
    """A task is a dict carrying the four protocol fields with the right types.

    All four fields have to be there: a missing field is a broken line, not a
    task with a default.  Checking presence and reading the value later on
    therefore agree with each other and no lookup can fail afterwards.
    """
    if not isinstance(value, dict):
        return False
    if any(field not in value for field in _TASK_FIELDS):
        return False
    issue = value["issue"]
    priority = value["priority"]
    backend = value["backend"]
    reason = value["reason"]
    return (
        isinstance(issue, int) and not isinstance(issue, bool)
        and (priority is None or priority in PRIORITIES)
        and (backend is None or isinstance(backend, str))
        and isinstance(reason, str)
    )


def _normalize_task(task):
    """Return the task with exactly the four protocol fields.

    The stored task is a copy, so a hook line can never change it later on and
    every later lookup finds the key it expects.
    """
    return {
        "issue": task["issue"],
        "priority": task.get("priority"),
        "backend": task.get("backend"),
        "reason": task.get("reason", ""),
    }


def _stderr_tail(stderr):
    """Keep the last characters of the stderr of a hook; the rest is noise."""
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", errors="replace")
    if not isinstance(stderr, str):
        return ""
    return stderr[-_STDERR_TAIL:]


def _merge_reasons(existing, new):
    """Join two reasons without repeating a part that is already there."""
    parts = []
    for chunk in (existing, new):
        for part in (chunk or "").split(";"):
            part = part.strip()
            if part and part not in parts:
                parts.append(part)
    return "; ".join(parts)


def run_hooks(hooks, runner=subprocess.run, timeout_s=60, is_paused=lambda scope: False):
    """Run the configured source hooks and return the merged tasks and diagnostics.

    The result always has the keys ``tasks``, ``errors``, ``rejected`` and
    ``skipped``; ``tasks`` is sorted by priority first and by issue number
    second.  A broken hook is reported in ``errors`` and never stops the other
    hooks.
    """
    tasks_by_issue = {}
    errors, skipped = [], []
    rejected = 0
    configured = hooks if isinstance(hooks, (list, tuple)) else []
    for hook in configured:
        name = None
        try:
            if not isinstance(hook, dict):
                errors.append({"name": None, "error": "invalid hook configuration"})
                continue
            name = hook.get("name") or "<unnamed>"
            argv = hook.get("argv")
            scope = hook.get("pause_scope")
            if scope is not None:
                try:
                    if is_paused(scope):
                        skipped.append(name)
                        continue
                except Exception as exc:
                    errors.append({"name": name, "error": "pause check failed: {}".format(exc)})
                    continue
            if (not isinstance(argv, (list, tuple)) or not argv
                    or not all(isinstance(arg, str) for arg in argv)):
                errors.append({"name": name, "error": "argv must be a non-empty string list"})
                continue
            try:
                result = runner(list(argv), capture_output=True, text=True, timeout=timeout_s, check=False)
            except subprocess.TimeoutExpired as exc:
                errors.append({"name": name, "error": "timeout", "stderr": _stderr_tail(exc.stderr)})
                continue
            except Exception as exc:
                errors.append({"name": name, "error": str(exc), "stderr": ""})
                continue
            returncode = getattr(result, "returncode", 1)
            stderr = _stderr_tail(getattr(result, "stderr", ""))
            if returncode != 0:
                errors.append({"name": name, "error": "exit status {}".format(returncode), "stderr": stderr})
                continue
            stdout = getattr(result, "stdout", "")
            if isinstance(stdout, bytes):
                stdout = stdout.decode("utf-8", errors="replace")
            if stdout is None:
                stdout = ""
            if not isinstance(stdout, str):
                errors.append({"name": name, "error": "invalid stdout", "stderr": stderr})
                continue
            for line in stdout.splitlines():
                try:
                    raw = json.loads(line)
                except (ValueError, TypeError):
                    rejected += 1
                    continue
                if not _valid_task(raw):
                    rejected += 1
                    continue
                task = _normalize_task(raw)
                issue = task["issue"]
                existing = tasks_by_issue.get(issue)
                if existing is None:
                    tasks_by_issue[issue] = task
                    continue
                if _PRIORITY[task["priority"]] < _PRIORITY[existing["priority"]]:
                    existing["priority"] = task["priority"]
                if task["backend"] is not None:
                    existing["backend"] = task["backend"]
                existing["reason"] = _merge_reasons(existing["reason"], task["reason"])
        except Exception as exc:
            # One broken hook must not stop the other hooks.
            errors.append({"name": name, "error": str(exc)})
    tasks = list(tasks_by_issue.values())
    try:
        tasks.sort(key=lambda task: (_PRIORITY.get(task.get("priority"), 3), task.get("issue")))
    except Exception:
        # Sorting must not break the contract either; fall back to a stable order.
        tasks.sort(key=lambda task: str(task.get("issue")))
    return {"tasks": tasks, "errors": errors, "rejected": rejected, "skipped": skipped}
