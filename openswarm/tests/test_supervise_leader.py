"""Tests for the fenced supervisor lease (Refs #1548)."""
import importlib.util
import multiprocessing
import pathlib
import tempfile
import threading
import unittest

MODULE = pathlib.Path(__file__).resolve().parents[1] / "lib" / "supervise" / "leader.py"
spec = importlib.util.spec_from_file_location("supervise_leader", MODULE)
leader = importlib.util.module_from_spec(spec)
spec.loader.exec_module(leader)


def _command_acquire_worker(path, lock_path, start, results, holder):
    store = command_store(path, lock_path)
    lease = leader.Lease(store, holder, 30, lambda: 1000)
    start.wait()
    results.put(lease.acquire())


def command_store(path, lock_path):
    get = ["python3", "-c", "import pathlib,sys; p=pathlib.Path(sys.argv[1]); print(p.read_text() if p.exists() else '{}')", str(path)]
    put = ["python3", "-c", "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text(sys.stdin.read())", str(path)]
    lock_code = (
        "import os,pathlib,sys,time,uuid; p=pathlib.Path(sys.argv[1]); handle=uuid.uuid4().hex\n"
        "while True:\n"
        " try:\n"
        "  fd=os.open(p, os.O_CREAT|os.O_EXCL|os.O_WRONLY); os.write(fd,handle.encode()); os.close(fd); print(handle); break\n"
        " except FileExistsError: time.sleep(.005)"
    )
    unlock_code = (
        "import pathlib,sys; p=pathlib.Path(sys.argv[1]); "
        "p.unlink() if p.exists() and p.read_text()==sys.argv[2] else None"
    )
    lock = ["python3", "-c", lock_code, str(lock_path)]
    unlock = ["python3", "-c", unlock_code, str(lock_path)]
    return leader.CommandStore(get, put, lock, unlock)


class Clock:
    def __init__(self, now=1000):
        self.now = now

    def __call__(self):
        return self.now


class LeaderLeaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = Clock()
        self.store = leader.FileStore(pathlib.Path(self.tmp.name) / "leader.json")

    def test_acquire_and_renew(self):
        lease = leader.Lease(self.store, "host-a", 30, self.clock)
        self.assertTrue(lease.acquire())
        self.assertEqual(lease.current(), {"holder": "host-a", "token": 1, "expires_at": 1030.0})
        self.clock.now += 10
        self.assertTrue(lease.renew())
        self.assertEqual(lease.current()["expires_at"], 1040.0)

    def test_live_lease_cannot_be_stolen_and_expired_lease_can(self):
        a = leader.Lease(self.store, "a", 30, self.clock)
        b = leader.Lease(self.store, "b", 30, self.clock)
        self.assertTrue(a.acquire())
        self.assertFalse(b.acquire())
        self.clock.now = 1030
        self.assertTrue(b.acquire())
        self.assertEqual(b.token, 2)

    def test_paused_holder_steps_down_after_fencing_token_advances(self):
        a = leader.Lease(self.store, "a", 30, self.clock)
        b = leader.Lease(self.store, "b", 30, self.clock)
        self.assertTrue(a.acquire())
        self.clock.now += 600
        self.assertTrue(b.acquire())
        self.assertFalse(a.renew())
        self.assertTrue(a.stepped_down)

    def test_concurrent_acquire_has_one_winner(self):
        barrier = threading.Barrier(8)
        results = []
        def acquire(n):
            lease = leader.Lease(self.store, str(n), 30, self.clock)
            barrier.wait()
            results.append(lease.acquire())
        threads = [threading.Thread(target=acquire, args=(n,)) for n in range(8)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(sum(results), 1)

    def test_command_store_round_trip_with_fake_commands(self):
        path = pathlib.Path(self.tmp.name) / "command.json"
        store = command_store(path, pathlib.Path(self.tmp.name) / "command.lock")
        lease = leader.Lease(store, "cmd", 20, self.clock)
        self.assertTrue(lease.acquire())
        self.assertEqual(lease.current()["holder"], "cmd")
        self.assertTrue(lease.release())
        self.assertIsNone(lease.token)
        self.assertFalse(lease.renew())

    def test_command_store_concurrent_processes_have_one_winner(self):
        ctx = multiprocessing.get_context("fork")
        path = pathlib.Path(self.tmp.name) / "shared.json"
        lock_path = pathlib.Path(self.tmp.name) / "shared.lock"
        start, results = ctx.Event(), ctx.Queue()
        workers = [
            ctx.Process(target=_command_acquire_worker, args=(path, lock_path, start, results, "host-%d" % n))
            for n in range(6)
        ]
        for worker in workers:
            worker.start()
        start.set()
        for worker in workers:
            worker.join(10)
            self.assertEqual(worker.exitcode, 0)
        self.assertEqual(sum(results.get(timeout=2) for _ in workers), 1)


if __name__ == "__main__":
    unittest.main()
