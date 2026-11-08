"""Test package marker for ``openswarm/tests``.

The suite is normally run from the repository root (``python -m pytest openswarm/tests``
or ``python -m unittest discover -s openswarm/tests``).  The marker keeps this directory a
package, so the test modules of this directory and the ones of ``tools/tests`` (which carry
the same file names) never collide under a single test run.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
