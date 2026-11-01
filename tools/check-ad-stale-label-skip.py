#!/usr/bin/env python3
"""Offline contract check for stale lane-ready search results (Refs #985)."""
import importlib.util
import json
import pathlib


ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "tools/lane-autodispatch/autodispatch.py"


def load_module():
    spec = importlib.util.spec_from_file_location("autodispatch_stale_label_contract", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    module = load_module()
    fixtures = {
        985: ["lane-ready", "lane-failed"],
        986: ["lane-ready"],
        987: ["lane-ready", "in-progress"],
    }

    def fake_gh(args, timeout=60):
        number = int(args[2])
        return 0, json.dumps({"state": "OPEN", "labels": [{"name": label} for label in fixtures[number]]}), ""

    module.gh = fake_gh
    assert not module.fresh_ready(985, {}), "lane-failed residual must be skipped"
    assert module.fresh_ready(986, {}), "fresh lane-ready must remain dispatchable"
    assert not module.fresh_ready(987, {}), "claimed lane-ready must be skipped"

    # the re-read labels decide whether a stale hit is a lane-failed residual
    assert module.issue_labels(985) == {"lane-ready", "lane-failed"}, "authoritative labels must be re-read"
    assert module.issue_labels(986) == {"lane-ready"}, "authoritative labels must be re-read"
    residual = "lane-failed" in module.issue_labels(985) and "lane-ready" in module.issue_labels(985)
    assert residual, "lane-failed + lane-ready residual must be detected"
    assert not ("lane-failed" in module.issue_labels(986) and "lane-ready" in module.issue_labels(986)), (
        "a plain lane-ready issue is not a residual"
    )

    # the residual skips stay observable in the lanes-status snapshot
    module.note_stale_skip(residual)
    status = module.status_snapshot({}, {}, {}, 0, 0)
    assert status["_stale_lane_failed_skips"] == 1, "lane-failed residual skips must be observable"
    assert status["_stale_stripped_n"] >= 1 and status["_stale_search_skips"] >= 1, (
        "stale search skips must stay observable"
    )

    source = MODULE_PATH.read_text()
    docs = (ROOT / "docs/ctl-boot.md").read_text()
    for text in (
        "stale search hit lane-failed + lane-ready",
        "_stale_lane_failed_skips",
        "remove lane-ready label",
    ):
        assert text in source or text in docs, text
    print("ad stale label skip contract: OK")


if __name__ == "__main__":
    main()
