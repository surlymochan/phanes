"""Load, validate, and evaluate resource threshold policy."""
import argparse
import copy
import json
import math
import sys


DEFAULTS = {
    "inode": {"pause_pct": 95, "resume_pct": 92, "cleanup_pct": 90},
    "disk": {"pause_pct": 95, "resume_pct": 92, "cleanup_pct": 90},
    "cgroup": {"high_ratio": 0.95, "high_events_delta": 1000, "shmem_ratio": 0.5},
}


def load_resources_policy(obj):
    """Return a copied full policy with resources defaults merged in and warnings."""
    if not isinstance(obj, dict):
        raise TypeError("policy must be an object")
    result = copy.deepcopy(obj)
    supplied = obj.get("resources", {})
    if not isinstance(supplied, dict):
        supplied = {}
        warnings = ["resources: expected object; using defaults"]
    else:
        warnings = []
    resources = copy.deepcopy(supplied)
    for group, defaults in DEFAULTS.items():
        values = resources.get(group, {})
        if not isinstance(values, dict):
            warnings.append("resources.%s: expected object; using defaults" % group)
            values = {}
        resources[group] = {**defaults, **copy.deepcopy(values)}
        for key in values:
            if key not in defaults:
                warnings.append("resources.%s.%s: unknown key" % (group, key))
    for group, values in resources.items():
        if group not in DEFAULTS:
            warnings.append("resources.%s: unknown key" % group)
    result["resources"] = resources
    return result, warnings


def validate(policy):
    """Return validation errors for a full policy or its resources section."""
    resources = policy.get("resources", policy) if isinstance(policy, dict) else None
    errors = []
    if not isinstance(resources, dict):
        return ["resources: must be an object"]
    for group in ("inode", "disk"):
        values = resources.get(group)
        if not isinstance(values, dict):
            errors.append("resources.%s: must be an object" % group)
            continue
        nums = {}
        for key in ("pause_pct", "resume_pct", "cleanup_pct"):
            value = values.get(key)
            path = "resources.%s.%s" % (group, key)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                errors.append("%s: must be a number from 0 to 100" % path)
            else:
                nums[key] = value
                if not 0 <= value <= 100:
                    errors.append("%s: must be a number from 0 to 100" % path)
        if len(nums) == 3:
            if not nums["resume_pct"] < nums["pause_pct"]:
                errors.append("resources.%s.resume_pct: must be < pause_pct (%s)" % (group, nums["pause_pct"]))
            if not nums["cleanup_pct"] <= nums["resume_pct"]:
                errors.append("resources.%s.cleanup_pct: must be <= resume_pct (%s)" % (group, nums["resume_pct"]))
    values = resources.get("cgroup")
    if not isinstance(values, dict):
        errors.append("resources.cgroup: must be an object")
    else:
        for key in ("high_ratio", "shmem_ratio"):
            value = values.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 < value <= 1:
                errors.append("resources.cgroup.%s: must be a ratio in (0, 1]" % key)
        value = values.get("high_events_delta")
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            errors.append("resources.cgroup.high_events_delta: must be a positive integer")
    return errors


def decide(kind, used_pct, was_paused, policy):
    """Choose a resource action using pause/resume hysteresis thresholds."""
    resources = policy.get("resources", policy)
    thresholds = resources[kind]
    if used_pct >= thresholds["pause_pct"]:
        return "pause"
    if was_paused:
        return "stay_paused" if used_pct >= thresholds["resume_pct"] else "resume"
    if used_pct >= thresholds["cleanup_pct"]:
        return "cleanup"
    return "ok"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("policy", nargs="?", help="policy JSON file (defaults to stdin)")
    parser.add_argument("--json", action="store_true", help="print machine-readable result")
    args = parser.parse_args(argv)
    try:
        with open(args.policy, encoding="utf-8") if args.policy else sys.stdin as stream:
            source = json.load(stream)
        policy, warnings = load_resources_policy(source)
        errors = validate(policy)
    except (OSError, ValueError, TypeError) as exc:
        print(json.dumps({"errors": [str(exc)], "warnings": []}, indent=2))
        return 2
    result = {"policy": policy, "warnings": warnings, "errors": errors}
    print(json.dumps(result if args.json else result, indent=2, sort_keys=True))
    return 2 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
