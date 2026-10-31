#!/usr/bin/env python3
"""Cursor print-mode fixture for headless lane lifecycle tests."""
import os
import subprocess
import sys


def leave_child_then_fail():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    marker = os.environ.get("FAKE_CURSOR_CHILD_PID")
    if marker:
        with open(marker, "w") as stream:
            stream.write(str(child.pid))
    os._exit(17)


prompt = sys.argv[-1] if sys.argv else ""
if "LANE_TEST_EXIT_WITH_CHILD" in prompt:
    leave_child_then_fail()
sys.stdout.write('{"type":"result","result":"{}","is_error":false}\n')
