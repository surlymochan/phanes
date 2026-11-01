#!/usr/bin/env python3
"""Offline contract check for transient harvest-fetch stderr classification."""
import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "tools/lane-autodispatch/autodispatch.py"


def load_module():
    spec = importlib.util.spec_from_file_location("autodispatch_fetch_transient", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    module = load_module()
    samples = [
        "fatal: bad pack header",
        "fatal: fetch-pack: unexpected disconnect while reading sideband packet",
    ]
    for sample in samples:
        assert module.should_retry_harvest_fetch(sample, 1), sample
    assert not module.should_retry_harvest_fetch("fatal: repository not found", 1)
    assert not module.should_retry_harvest_fetch(samples[0], module.HARVEST_FETCH_MAX_ATTEMPTS)
    print("ad fetch transient classification: OK")


if __name__ == "__main__":
    main()
