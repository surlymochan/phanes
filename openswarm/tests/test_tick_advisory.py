"""Dispatch tick coverage advisory (Refs #1850)."""
import importlib.util
import pathlib
import unittest

spec = importlib.util.spec_from_file_location(
    "dispatch_engine_advisory", pathlib.Path(__file__).parents[1] / "lib/dispatch/engine.py")
engine = importlib.util.module_from_spec(spec)
spec.loader.exec_module(engine)


class TickAdvisoryTests(unittest.TestCase):
    def test_risky_change_without_tick_test(self):
        event = engine.tick_test_advisory(
            'diff --git a/engine.py b/engine.py\n+dispatch_lane()\n')
        self.assertEqual(event['event'], 'tick_test_advisory')
        self.assertEqual(event['rules_version'], engine.TICK_ADVISORY_RULES_VERSION)

    def test_added_tick_test_suppresses_advisory(self):
        diff = ('diff --git a/engine.py b/engine.py\n+pause_lane()\n'
                'diff --git a/tests/test_engine.py b/tests/test_engine.py\n+        d.tick(lanes, state)\n')
        self.assertIsNone(engine.tick_test_advisory(diff))

    def test_unrelated_change_has_no_event(self):
        self.assertIsNone(engine.tick_test_advisory(
            'diff --git a/engine.py b/engine.py\n+value = 42\n'))

    def test_removed_tick_test_does_not_suppress_advisory(self):
        diff = ('diff --git a/engine.py b/engine.py\n+terminal = True\n'
                'diff --git a/tests/test_engine.py b/tests/test_engine.py\n-        d.tick(lanes, state)\n')
        self.assertIsNotNone(engine.tick_test_advisory(diff))

    def test_non_calls_do_not_suppress_advisory(self):
        for text in ('# TODO: d.tick()', 'message = "d.tick()"', 'def tick(self):',
                     'message = """\nd.tick()\n"""'):
            with self.subTest(text=text):
                diff = ('diff --git a/engine.py b/engine.py\n+dispatch()\n'
                        'diff --git a/tests/test_engine.py b/tests/test_engine.py\n'
                        + ''.join('+' + line + '\n' for line in text.splitlines()))
                self.assertIsNotNone(engine.tick_test_advisory(diff))
