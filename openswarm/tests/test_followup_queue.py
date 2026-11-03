"""The persistent FOLLOW-UP queue: a follow-up the throttle dropped is not lost (Refs #1868).

The module is loaded by file path: ``openswarm/lib/dispatch`` is a namespace package
(no ``__init__.py``), so it cannot be imported as a package from the repository root.
"""
import importlib.util
import pathlib
import tempfile
import threading
import unittest

HERE = pathlib.Path(__file__).resolve().parent
MODULE = HERE.parent / "lib" / "dispatch" / "followup_queue.py"
spec = importlib.util.spec_from_file_location("dispatch_followup_queue", MODULE)
followup_queue = importlib.util.module_from_spec(spec)
spec.loader.exec_module(followup_queue)


class FollowupQueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="openswarm-followup-")
        self.addCleanup(self.tmp.cleanup)
        self.path = pathlib.Path(self.tmp.name) / "followups.jsonl"
        self.queue = followup_queue.FollowupQueue(self.path)

    def test_an_appended_follow_up_is_kept_on_disk(self):
        self.assertTrue(self.queue.append(42, "abc123", "run-1", "the follow-up text"))
        rows = self.queue.entries()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["pr"], 42)
        self.assertEqual(rows[0]["sha"], "abc123")
        self.assertEqual(rows[0]["run"], "run-1")
        self.assertEqual(rows[0]["text"], "the follow-up text")
        self.assertTrue(self.path.exists())

    def test_the_queue_is_read_back_by_another_reader(self):
        self.queue.append(42, "abc123", "run-1", "text")
        other = followup_queue.FollowupQueue(self.path)
        self.assertEqual(len(other.entries()), 1)
        self.assertEqual(len(other), 1)

    def test_the_same_follow_up_is_written_once(self):
        self.assertTrue(self.queue.append(42, "abc123", "run-1", "text"))
        self.assertFalse(self.queue.append(42, "abc123", "run-2", "text"))
        self.assertEqual(len(self.queue.entries()), 1)

    def test_the_dedup_key_is_pr_sha_and_the_head_of_the_text(self):
        text = "x" * 80 + "a"
        self.assertTrue(self.queue.append(42, "abc123", "run-1", text))
        # the same head of the text: the same follow-up
        self.assertFalse(self.queue.append(42, "abc123", "run-1", "x" * 80 + "b"))
        # another PR and another head: another follow-up
        self.assertTrue(self.queue.append(43, "abc123", "run-1", text))
        self.assertTrue(self.queue.append(42, "def456", "run-1", text))
        self.assertEqual(len(self.queue.entries()), 3)

    def test_an_empty_follow_up_is_not_queued(self):
        self.assertFalse(self.queue.append(42, "abc123", "run-1", "   "))
        self.assertEqual(self.queue.entries(), [])

    def test_drain_returns_the_entries_and_empties_the_file(self):
        self.queue.append(42, "abc123", "run-1", "first")
        self.queue.append(43, "def456", "run-2", "second")
        rows = self.queue.drain()
        self.assertEqual([row["text"] for row in rows], ["first", "second"])
        self.assertEqual(self.queue.entries(), [])
        self.assertEqual(self.queue.drain(), [])

    def test_the_module_helpers_use_the_named_file(self):
        self.assertTrue(followup_queue.append(self.path, 42, "abc", "run-1", "text"))
        rows = followup_queue.drain(self.path)
        self.assertEqual([row["pr"] for row in rows], [42])
        self.assertEqual(followup_queue.drain(self.path), [])

    def test_a_torn_line_does_not_lose_the_other_entries(self):
        self.queue.append(42, "abc123", "run-1", "first")
        with open(self.path, "a", encoding="utf-8") as stream:
            stream.write("{not json\n")
        self.queue.append(43, "def456", "run-2", "second")
        rows = self.queue.drain()
        self.assertEqual([row["text"] for row in rows], ["first", "second"])

    def test_a_torn_tail_without_a_newline_does_not_lose_the_new_follow_up(self):
        """A write that was cut short leaves a tail line without a newline."""
        self.queue.append(42, "abc123", "run-1", "first")
        with open(self.path, "a", encoding="utf-8") as stream:
            stream.write('{"pr": 43, "sha": "def')
        self.assertTrue(self.queue.append(44, "ghi789", "run-3", "second"))
        rows = self.queue.drain()
        self.assertEqual([row["text"] for row in rows], ["first", "second"])

    def test_a_follow_up_written_during_a_drain_is_not_lost(self):
        """A follow-up appended while the queue is drained is not thrown away."""
        self.queue.append(42, "abc123", "run-1", "first")
        reading = threading.Event()   # the drain has read the file
        written = threading.Event()   # the concurrent append has returned

        class SlowQueue(followup_queue.FollowupQueue):
            def _read(self):
                raw = super()._read()
                reading.set()
                # give the concurrent append the chance to land between the read of
                # the drain and the emptying of the file
                written.wait(1)
                return raw

        queue = SlowQueue(self.path)

        def writer():
            reading.wait(2)
            followup_queue.FollowupQueue(self.path).append(43, "def456", "run-2", "second")
            written.set()

        thread = threading.Thread(target=writer)
        thread.start()
        rows = queue.drain()
        thread.join(5)
        self.assertEqual([row["text"] for row in rows], ["first"])
        # the follow-up written during the drain is still queued
        self.assertEqual([row["text"] for row in queue.entries()], ["second"])

    def test_a_missing_queue_file_is_an_empty_queue(self):
        self.assertEqual(self.queue.entries(), [])
        self.assertEqual(self.queue.drain(), [])


if __name__ == "__main__":
    unittest.main()
