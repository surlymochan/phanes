"""Test package marker for ``openswarm/tests``.

The suite is normally run from inside ``openswarm/`` (``python -m unittest discover -s tests``),
which puts this directory on ``sys.path`` so the helpers (``_load``) and the sibling test
modules (``test_batch`` ...) can be imported as top-level modules.  Running the suite from the
repository root (``python -m unittest openswarm.tests.test_lane``) imports the very same files
through this package, so this directory has to be on ``sys.path`` as well.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
