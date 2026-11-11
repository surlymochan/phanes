"""Two-process reproduction of the pre-2147 shared-root race.

Run at the repository root before the fix, or pass --expect-isolated after it.
The barrier selects the vulnerable interleaving instead of relying on timing.
It loads the old victim setup, then runs the actual Agy success test in a child.
"""
import multiprocessing as mp
import os, pathlib, subprocess, sys, unittest
mp.set_start_method("fork")
root = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / 'swrm/tests'))
old = subprocess.check_output(['git', 'show', '2432a545e:swrm/tests/test_dispatch_pause.py'], text=True)
def killer(conn):
    import test_agy
    conn.recv()
    result = unittest.TestResult()
    test_agy.AgyBackend('test_success_parses_result_and_records_usage').run(result)
    conn.send((len(result.failures), len(result.errors)))
isolated = '--expect-isolated' in sys.argv
for name in ('test_legacy_filename_can_be_read_and_resumed', 'test_independent_holds_match_only_their_dispatch_target'):
    ns = {'__file__': str(root / 'swrm/tests/test_dispatch_pause.py'), '__name__': 'old_pause'}
    exec(compile(old, ns['__file__'], 'exec'), ns)
    a, b = mp.Pipe()
    proc = mp.Process(target=killer, args=(b,))
    proc.start()
    case = ns['PauseStoreTests'](name)
    case.setUp()
    if 'legacy' in name:
        case.store.dir.mkdir()
    else:
        for scope in ('host:worker@health', 'lane:L1', 'backend:pi'):
            case.store.pause(scope, now=10)
    a.send(True)
    print('killer failures/errors:', a.recv())
    if 'legacy' in name:
        try:
            (case.store.dir / 'host_worker.json').write_text('{}')
        except FileNotFoundError:
            assert not isolated
            print(name, 'REPRODUCED FileNotFoundError')
        else:
            assert isolated
            print(name, 'isolated')
    else:
        print(name, 'is_paused =', case.store.is_paused(host='worker', now=10))
        assert case.store.is_paused(host='worker', now=10) == isolated
    case.doCleanups()
    proc.join()
