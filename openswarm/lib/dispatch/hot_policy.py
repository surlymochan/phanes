"""The hot-file policy: one source for the caps of the mint side and of the AD (Refs #1721).

The dispatch policy files carry a ``dispatch`` section::

    "dispatch": {
      "hot_shared": {"openswarm/bin/openswarm": 1},
      "hot_union": ["docs/notes.md"],
      "hot_default": 1
    }

``hot_shared`` is the per-file cap (the HOT_SHARED list), ``hot_union`` the files that
never block a dispatch and ``hot_default`` the cap of every file without an entry.  The
planner (which mints a card before it enters the queue) and the AD (which hands the
cards to the lanes) read this one policy, so the two sides cannot disagree about a cap:
the list is written once, in the policy file, and never in both places.

The cap maps are returned per source file, lowest precedence first, so the caller can
merge them with the path normalization of ``hotfiles.py`` (one file spelled in two ways
must follow the source with the higher precedence, not the spelling that comes last).
"""
import json
import os

POLICY_SECTION = "dispatch"


def policy_paths(cfg=None):
    """Return the policy files of the dispatch, lowest precedence first.

    The repository policy (``openswarm/policy.json``) ships the defaults, the local
    policy file refines them and the dispatch config may point at its own policy file.
    """
    cfg = cfg or {}
    shipped = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), "policy.json")
    local = cfg.get("policy_file") or os.path.expanduser("~/.claude/openswarm/policy.local.json")
    return [shipped, local]


def _section(path):
    """Return the ``dispatch`` section of one policy file (an empty dict when absent)."""
    try:
        with open(path, encoding="utf-8") as stream:
            data = json.load(stream)
    except (OSError, ValueError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    section = data.get(POLICY_SECTION)
    return section if isinstance(section, dict) else {}


def load_hot_policy(cfg=None, paths=None):
    """Return the hot-file policy of the dispatch.

    The result is ``{"caps": [<cap map per policy file>], "union": [...],
    "default_limit": <int or None>}``.  Later policy files win for the caps; the exempt
    files are additive, so a file that one policy exempts stays exempt.  A missing or
    broken policy file is skipped: the dispatch keeps working with the caps it has.
    """
    caps, union, default = [], [], None
    for path in (paths if paths is not None else policy_paths(cfg)):
        section = _section(path)
        source = section.get("hot_shared")
        if isinstance(source, dict):
            caps.append(source)
        union.extend(section.get("hot_union") or [])
        if section.get("hot_default") is not None:
            default = section["hot_default"]
    return {"caps": caps, "union": union, "default_limit": default}
