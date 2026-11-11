"""Load bin/swrm as a module with an isolated OPENSWARM_HOME, so tests never touch ~/.claude/swrm."""
import atexit, importlib.machinery, importlib.util, os, shutil, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FIXTURE_STATE = os.path.join(HERE, "fixtures", "state.json")

# Unversioned fixtures below this checkout must not discover its .git and
# consequently clean the real executor scratch directory.
_original_environment = dict(os.environ)
_ceiling = os.path.dirname(ROOT)   # the real checkout root (identity checks below)
# The git ceiling must be the temp root, not the checkout: a ceiling stops git from
# ascending *into* the listed directory, so a checkout ceiling would also hide the
# checkout from tests that legitimately run git inside it.  Fixtures live below the
# temp root and can therefore never climb into a checkout that contains it.
_temp_ceiling = os.path.realpath(tempfile.gettempdir())
os.environ["GIT_CEILING_DIRECTORIES"] = os.pathsep.join(
    filter(None, (os.environ.get("GIT_CEILING_DIRECTORIES"), _temp_ceiling)))


def _restore_environment():
    os.environ.clear()
    os.environ.update(_original_environment)


# Record allocations, including TemporaryDirectory (which calls mkdtemp).
# The PID prevents a forked worker from claiming its parent's fixture roots.
_owned_fixture_roots = set()
_mkdtemp = tempfile.mkdtemp


def _registered_mkdtemp(*args, **kwargs):
    directory = _mkdtemp(*args, **kwargs)
    _owned_fixture_roots.add((os.path.realpath(directory), os.getpid()))
    return directory


tempfile.mkdtemp = _registered_mkdtemp
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

    def cleanup_owned_fixture(worktree):
        target = os.path.realpath(mod.executor_worktree_root(worktree))
        if os.path.realpath(target) == os.path.realpath(_ceiling):
            return   # refuse silently: a test must never remove the real checkout's scratch root
        if not any(owner == os.getpid() and
                   os.path.commonpath((target, root)) == root
                   for root, owner in _owned_fixture_roots):
            return   # refuse silently: only roots this process allocated may be cleaned
        # Do not call the production helper: it resolves the worktree again.
        tmpdir = os.path.join(target, ".swrm-tmp")
        if os.path.islink(tmpdir):
            os.unlink(tmpdir)
        else:
            shutil.rmtree(tmpdir, ignore_errors=True)

    mod.cleanup_executor_tmp = cleanup_owned_fixture
    mod.DEFAULT_KNOWLEDGE_REPO = os.path.join(_home, "agent-knowledge")
    return mod
