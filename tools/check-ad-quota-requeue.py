#!/usr/bin/env python3
"""Offline contract for returning AD quota failures to lane-ready."""
import importlib.util
import os
from pathlib import Path


QUOTA_REQUEUE_CLASSES = {"rate_limit", "quota", "quota_exhausted", "usage_limit"}


def finish_failed(info, gh):
    """Model the AD finish_failed behavior without calling GitHub or a backend."""
    error_class = info.get("error_class")
    if error_class in QUOTA_REQUEUE_CLASSES:
        gh("issue", "edit", info["issue"], "--add-label", "lane-ready")
        gh("issue", "edit", info["issue"], "--remove-label", "in-progress")
        gh("issue", "edit", info["issue"], "--remove-label", "lane-failed")
        gh("issue", "comment", info["issue"], "Quota/rate limit; this is not a card failure.")
        note_quota(info)
        return
    gh("issue", "edit", info["issue"], "--add-label", "lane-failed")


def note_quota(info):
    """Reference hook: quota failures still trigger the backend pause path."""
    info["quota_noted"] = True


class FakeGh:
    def __init__(self):
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)


def check(name, condition):
    if not condition:
        raise AssertionError(name)


def run_reference_cases():
    for error_class in ("rate_limit", "quota", "usage_limit"):
        gh = FakeGh()
        info = {"issue": "#1555", "error_class": error_class}
        finish_failed(info, gh)
        calls = [" ".join(call) for call in gh.calls]
        check(f"reference/{error_class}: add lane-ready", any("--add-label lane-ready" in call for call in calls))
        check(f"reference/{error_class}: remove in-progress", any("--remove-label in-progress" in call for call in calls))
        check(f"reference/{error_class}: remove lane-failed", any("--remove-label lane-failed" in call for call in calls))
        check(f"reference/{error_class}: explanatory comment", any("not a card failure" in call for call in calls))
        check(f"reference/{error_class}: note_quota", info.get("quota_noted") is True)

    for error_class in ("exec_error", "verify_failed", ""):
        gh = FakeGh()
        finish_failed({"issue": "#1555", "error_class": error_class}, gh)
        calls = [" ".join(call) for call in gh.calls]
        check(f"reference/{error_class or 'empty'}: lane-failed", any("--add-label lane-failed" in call for call in calls))


def check_live_ad(path):
    spec = importlib.util.spec_from_file_location("autodispatch_contract", path)
    check("AD_PATH: load module", spec is not None and spec.loader is not None)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    classes = getattr(module, "QUOTA_REQUEUE_CLASSES", None)
    check("AD_PATH: quota classes available", classes is not None)
    for error_class in ("rate_limit", "quota", "usage_limit"):
        check(f"AD_PATH/{error_class}: requeued", error_class in classes)
    for error_class in ("exec_error", "verify_failed", ""):
        check(f"AD_PATH/{error_class or 'empty'}: lane-failed", error_class not in classes)


def main():
    run_reference_cases()
    ad_path = os.environ.get("AD_PATH")
    if ad_path:
        check_live_ad(Path(ad_path))
        print(f"AD_PATH contract ({ad_path}): OK")
    else:
        print("AD_PATH contract: SKIP (AD_PATH not set)")
    print("AD quota requeue contract: OK")


if __name__ == "__main__":
    try:
        main()
    except AssertionError as exc:
        print(f"FAIL: {exc}")
        raise SystemExit(1)
