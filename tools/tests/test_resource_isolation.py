"""Run the shared fixture guard through the existing suite entry point."""
import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "test_isolation_guard", Path(__file__).resolve().parents[2] / "tools/test_isolation_guard.py")
_guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_guard)
IsolationGuardTests = _guard.IsolationGuardTests
