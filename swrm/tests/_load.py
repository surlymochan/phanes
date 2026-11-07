"""Load bin/swrm as a module with an isolated OPENSWARM_HOME, so tests never touch ~/.claude/swrm."""
import atexit, importlib.machinery, importlib.util, os, shutil, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FIXTURE_STATE = os.path.join(HERE, "fixtures", "state.json")

_home = tempfile.mkdtemp(prefix="swrm-test-home-")
atexit.register(shutil.rmtree, _home, ignore_errors=True)
os.environ["OPENSWARM_HOME"] = _home
for _k in list(os.environ):
    if _k.startswith(("OPENSWARM_", "XSWARM_")) and _k != "OPENSWARM_HOME":
        os.environ.pop(_k)

# Never fall back to the machine-wide fleet coordinator during tests.
os.environ["OPENSWARM_FLEET_GUARD_CONFIG"] = os.path.join(_home, "fleet-guard.disabled.json")


def load():
    loader = importlib.machinery.SourceFileLoader("swrm", os.path.join(ROOT, "bin", "swrm"))
    spec = importlib.util.spec_from_loader("swrm", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    mod.DEFAULT_KNOWLEDGE_REPO = os.path.join(_home, "agent-knowledge")
    return mod
