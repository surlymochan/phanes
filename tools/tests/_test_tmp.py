"""Use the same owned scratch allocator as the Swrm test suite."""
import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "_tools_broker_wait", Path(__file__).resolve().parents[2] / "swrm/tests/_broker_wait.py")
_helper = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_helper)
short_tmp_root = _helper.short_tmp_root
