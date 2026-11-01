#!/usr/bin/env python3
"""Offline contract for AD low-queue refill sizing and backlog card filtering."""
import importlib.util
import pathlib


ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "tools/lane-autodispatch/autodispatch.py"


def load_module():
    spec = importlib.util.spec_from_file_location("ad_ready_target_contract", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    ad = load_module()

    # A low queue is topped up to TARGET; starvation still wins when it is larger.
    assert ad.refill_need(3, low=10, target=20, starve=0) == 17
    assert ad.refill_need(9, low=10, target=20, starve=23) == 23
    assert ad.refill_need(12, low=10, target=20, starve=0) == 0

    thin = "# title\nlabels: p1\n"
    full = "# title\nlabels: p1\n\nImplement the task and verify it offline.\n"
    assert ad.is_thin_card(thin)
    assert not ad.is_thin_card(full)

    # This contract must remain offline: importing the module is the only I/O.
    print("ad ready target contract: OK")


if __name__ == "__main__":
    main()
