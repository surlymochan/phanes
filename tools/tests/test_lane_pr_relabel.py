"""Offline delivery relabel failures, using authoritative gh readbacks."""
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    'ad_relabel', Path(__file__).resolve().parents[1] / 'lane-autodispatch/autodispatch.py')
AD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AD)


class RelabelTests(unittest.TestCase):
    def run_transition(self, stale=0, broken=None, transient=False):
        labels = {'lane-ready', 'in-progress', 'p1', 'backend:codex'}
        calls, notes = [], []
        writes = 0
        def gh(args):
            nonlocal writes
            kind = args[1]
            calls.append(kind)
            fails = broken == kind and (not transient or calls.count(kind) == 1)
            if broken == kind + '-rc': return 1, '', 'connection reset'
            if fails: raise OSError('connection reset')
            if kind == 'view':
                if broken == 'post-read' and writes:
                    raise OSError('connection reset')
                return 0, json.dumps({'labels': [{'name': x} for x in labels]}), ''
            writes += 1
            if writes > stale:
                for i, arg in enumerate(args):
                    if arg == '--remove-label': labels.discard(args[i + 1])
                    if arg == '--add-label': labels.add(args[i + 1])
            return 0, '', ''
        with patch.object(AD, 'gh', gh), patch.object(AD, 'log', lambda msg, brain=True: notes.append((msg, brain))):
            ok = AD.ensure_delivery_state(1772)
        return ok, labels, calls, notes

    def test_metadata_preserved(self):
        ok, labels, calls, _ = self.run_transition()
        self.assertTrue(ok)
        self.assertEqual(labels, {'lane-pr', 'p1', 'backend:codex'})
        self.assertEqual(calls, ['view', 'edit', 'view'])

    def test_stale_readback_retries(self):
        ok, labels, calls, _ = self.run_transition(stale=1)
        self.assertTrue(ok)
        self.assertEqual(labels & AD.DELIVERY_LABELS, {'lane-pr'})
        self.assertEqual(calls, ['view', 'edit', 'view'] * 2)

    def test_nonconvergence_warns_and_fails(self):
        ok, _, calls, notes = self.run_transition(stale=3)
        self.assertFalse(ok)
        self.assertEqual(calls, ['view', 'edit', 'view'] * 3)
        self.assertTrue(any(brain and 'WARNING' in msg for msg, brain in notes))

    def test_read_and_write_exceptions_retry(self):
        for kind in ('view', 'edit'):
            with self.subTest(kind=kind):
                self.assertTrue(self.run_transition(broken=kind, transient=True)[0])

    def test_persistent_exceptions_warn_and_fail(self):
        for kind in ('view', 'edit', 'view-rc', 'edit-rc', 'post-read'):
            with self.subTest(kind=kind):
                ok, _, calls, notes = self.run_transition(broken=kind)
                self.assertFalse(ok)
                self.assertEqual(calls.count('view') if kind in ('view', 'view-rc') else calls.count('edit'), 1 if kind == 'post-read' else 3)
                if kind in ('view', 'view-rc'): self.assertNotIn('edit', calls)
                self.assertTrue(any(brain and 'WARNING' in msg for msg, brain in notes))
