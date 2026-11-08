"""Compatibility entry point for the #1594 pi stream retry regression.

The vendored openswarm tree was replaced by swrm as the single source (#1894): the pi lane
implementation lives in `swrm/bin/swrm` and its regression test in `swrm/tests/`.  The issue
and its acceptance command still name the openswarm paths, so this module keeps that command
working: it runs exactly the same test cases, from the same file.
"""
import importlib.util
import pathlib
import sys

SWRM_TEST = pathlib.Path(__file__).resolve().parents[2] / "swrm" / "tests" / "test_pi_stream_retry.py"
sys.path.insert(0, str(SWRM_TEST.parent))

_spec = importlib.util.spec_from_file_location("swrm_test_pi_stream_retry", SWRM_TEST)
_module = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _module
_spec.loader.exec_module(_module)

PiStreamRetryContractTests = _module.PiStreamRetryContractTests
PiStreamRetryLaneTests = _module.PiStreamRetryLaneTests

if __name__ == "__main__":
    import unittest

    unittest.main()
