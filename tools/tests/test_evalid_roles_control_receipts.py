import json
import tempfile
import unittest
from pathlib import Path

from tools.evalid_roles_control import RoleController
from tools.evalid_run_state import ReceiptError


class ControllerReceiptRecoveryTests(unittest.TestCase):
    def test_standalone_receipt_does_not_authorize_controller_transition(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            directory = root / 'control' / 'runs' / 'one'
            directory.mkdir(parents=True)
            (directory / 'result.json').write_text(json.dumps({
                'id': 'one', 'change_id': 'change-1', 'attempt': 0,
                'stage': 'spec', 'status': 'done', 'accepted': True,
                'artifact_digest': 'sha256:' + 'a' * 64}))
            status = directory / 'status.json'
            status.write_text(json.dumps({'id': 'one', 'state': 'finished', 'status': 'done'}))
            self.assertIsNone(RoleController(root / 'control', root).state('change-1'))
            status.write_text(json.dumps({'id': 'one', 'state': 'running', 'status': 'running'}))
            with self.assertRaises(ReceiptError):
                RoleController(root / 'control', root)

    def test_conflicting_runs_refuse_recovery(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            runs = root / 'control' / 'runs'
            for run_id in ('one', 'two'):
                directory = runs / run_id
                directory.mkdir(parents=True)
                (directory / 'result.json').write_text(json.dumps({
                    'id': run_id, 'change_id': 'change-1', 'attempt': 0,
                    'stage': 'spec', 'status': 'done', 'accepted': True,
                    'artifact_digest': 'sha256:' + 'a' * 64}))
                (directory / 'status.json').write_text(json.dumps({
                    'id': run_id, 'state': 'finished', 'status': 'done'}))
            with self.assertRaises(ReceiptError):
                RoleController(root / 'control', root)


class ControllerRefreshRescansReceiptsTests(unittest.TestCase):
    DIGEST = 'sha256:' + 'a' * 64

    def _controller_with_accepted_spec_run(self, root):
        """A log that accepted a spec stage backed by the finished run ``one``."""
        control = root / 'control'
        directory = control / 'runs' / 'one'
        directory.mkdir(parents=True)
        (directory / 'result.json').write_text(json.dumps({
            'id': 'one', 'change_id': 'change-1', 'attempt': 0,
            'stage': 'spec', 'status': 'done', 'accepted': True,
            'artifact_digest': self.DIGEST}))
        status = directory / 'status.json'
        status.write_text(json.dumps({'id': 'one', 'state': 'finished', 'status': 'done'}))
        events = [
            {'change_id': 'change-1', 'attempt': 0, 'stage': 'intake', 'data': {},
             'input_digest': self.DIGEST},
            {'change_id': 'change-1', 'attempt': 0, 'stage': 'spec',
             'data': {'spec_digest': self.DIGEST, 'spec_version': 1, 'run_id': 'one'},
             'input_digest': self.DIGEST}]
        (control / 'events.jsonl').write_text(''.join(json.dumps(item) + '\n' for item in events))
        return RoleController(control, root), status

    def test_refresh_rejects_a_receipt_changed_after_start(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            controller, status = self._controller_with_accepted_spec_run(root)
            controller._refresh()   # unchanged receipts are accepted
            status.write_text(json.dumps({'id': 'one', 'state': 'running', 'status': 'running'}))
            with self.assertRaises(ReceiptError):
                controller._refresh()

    def test_refresh_rejects_a_runs_directory_that_vanished(self):
        import shutil
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            controller, _ = self._controller_with_accepted_spec_run(root)
            shutil.rmtree(root / 'control' / 'runs')
            with self.assertRaises(ReceiptError):
                controller._refresh()
