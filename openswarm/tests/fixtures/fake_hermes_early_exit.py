#!/usr/bin/env python3
"""Exit during Hermes ACP initialization after leaving a group descendant."""
import os
import subprocess
import sys


child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
marker = os.environ.get("FAKE_CURSOR_CHILD_PID")
if marker:
    with open(marker, "w") as stream:
        stream.write(str(child.pid))
sys.exit(17)
