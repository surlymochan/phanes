"""Load bin/swrm as a module with an isolated OPENSWARM_HOME, so tests never touch ~/.claude/swrm."""
import atexit, importlib.machinery, importlib.util, os, shutil, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FIXTURE_STATE = os.path.join(HERE, "fixtures", "state.json")

# Unversioned fixtures below this checkout must not discover its .git and
# consequently clean the real executor scratch directory.
_original_environment = dict(os.environ)
_ceiling = os.path.dirname(ROOT)
os.environ["GIT_CEILING_DIRECTORIES"] = os.pathsep.join(
    filter(None, (os.environ.get("GIT_CEILING_DIRECTORIES"), _ceiling)))


def _restore_environment():
    os.environ.clear()
    os.environ.update(_original_environment)


atexit.register(_restore_environment)
_home = tempfile.mkdtemp(prefix="swrm-test-home-")
atexit.register(shutil.rmtree, _home, ignore_errors=True)
os.environ["OPENSWARM_HOME"] = _home
for _k in list(os.environ):
    if _k.startswith(("OPENSWARM_", "XSWARM_")) and _k != "OPENSWARM_HOME":
        os.environ.pop(_k)
# Keep both in-process calls and CLI children away from the host coordinator.
# Tests exercising fleet admission explicitly replace these paths with fixtures.
os.environ["OPENSWARM_FLEET_GUARD_CONFIG"] = os.path.join(_home, "fleet-guard.json")
os.environ["OPENSWARM_FLEET_CAPACITY_DIR"] = os.path.join(_home, "fleet-capacity")

# Never fall back to the machine-wide fleet coordinator during tests.
os.environ["OPENSWARM_FLEET_GUARD_CONFIG"] = os.path.join(_home, "fleet-guard.disabled.json")


def load():
    loader = importlib.machinery.SourceFileLoader("swrm", os.path.join(ROOT, "bin", "swrm"))
    spec = importlib.util.spec_from_loader("swrm", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    cleanup = mod.cleanup_executor_tmp

    def cleanup_owned_fixture(worktree):
        target = mod.executor_worktree_root(worktree)
        if os.path.realpath(target) == os.path.realpath(_ceiling):
            raise AssertionError("test attempted to clean the real checkout scratch root")
        return cleanup(worktree)

    mod.cleanup_executor_tmp = cleanup_owned_fixture
    mod.DEFAULT_KNOWLEDGE_REPO = os.path.join(_home, "agent-knowledge")
    return mod
