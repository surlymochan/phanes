"""Load bin/openswarm as a module with an isolated OPENSWARM_HOME, so tests never touch ~/.claude/openswarm."""
import atexit, importlib.machinery, importlib.util, os, shutil, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FIXTURE_STATE = os.path.join(HERE, "fixtures", "state.json")

_home = tempfile.mkdtemp(prefix="openswarm-test-home-")
atexit.register(shutil.rmtree, _home, ignore_errors=True)
os.environ["OPENSWARM_HOME"] = _home
for _k in list(os.environ):
    if _k.startswith(("OPENSWARM_", "XSWARM_")) and _k != "OPENSWARM_HOME":
        os.environ.pop(_k)


def load():
    loader = importlib.machinery.SourceFileLoader("openswarm", os.path.join(ROOT, "bin", "openswarm"))
    spec = importlib.util.spec_from_loader("openswarm", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    mod.DEFAULT_KNOWLEDGE_REPO = os.path.join(_home, "agent-knowledge")
    # The CLI module replaces the openswarm package in sys.modules, so a suite started as
    # `python -m unittest openswarm.tests.<module>` could no longer walk down to the test
    # module.  Keep the already imported test package reachable from the CLI module.
    tests_package = sys.modules.get("openswarm.tests")
    if tests_package is not None:
        mod.tests = tests_package
    return mod
