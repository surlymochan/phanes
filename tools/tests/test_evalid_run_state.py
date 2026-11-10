import json
import tempfile
import unittest
from pathlib import Path

from tools.evalid_run_state import ReceiptError, scan_runs
from tools.evalid_roles_control import RoleController, digest


class RunStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.temp.cleanup)
        self.runs = Path(self.temp.name)

    def receipt(self, run_id, stage, *, change='change-1', attempt=0,
                status='done', artifact=None):
        directory = self.runs / run_id
        directory.mkdir()
        value = {'id': run_id, 'change_id': change, 'attempt': attempt,
                 'stage': stage, 'status': status, 'accepted': True,
                 'artifact_digest': artifact or 'sha256:' + 'a' * 64}
        (directory / 'result.json').write_text(json.dumps(value))
        (directory / 'status.json').write_text(json.dumps(
            {'id': run_id, 'state': 'finished' if status not in ('running', 'queued') else status,
             'status': status}))
        return value

    def test_restart_and_unordered_full_chain(self):
        stages = ('spec', 'spec-check', 'red', 'green', 'holdout', 'review', 'delivery')
        for run_id, stage in zip(('z', 'y', 'x', 'w', 'v', 'u', 't'), stages):
            self.receipt(run_id, stage)
        first = scan_runs(self.runs)
        self.assertEqual(first, scan_runs(self.runs))
        state = first[('change-1', 0)]
        self.assertEqual(state['next'], 'complete')
        self.assertEqual([r['stage'] for r in state['receipts']], list(stages))
        self.assertEqual(state['receipts'][0]['run_id'], 'z')
        self.assertEqual(state['receipts'][0]['artifact_digest'], 'sha256:' + 'a' * 64)

    def test_partial_chain_and_attempts(self):
        self.receipt('spec', 'spec')
        self.receipt('other', 'spec', attempt=1)
        states = scan_runs(self.runs)
        self.assertEqual(states[('change-1', 0)]['next'], 'spec-check')
        self.assertEqual(states[('change-1', 1)]['next'], 'spec-check')

    def test_conflicts_and_gaps_fail_closed(self):
        self.receipt('one', 'spec')
        self.receipt('two', 'spec', artifact='sha256:' + 'b' * 64)
        with self.assertRaises(ReceiptError):
            scan_runs(self.runs)

    def test_nonterminal_never_advances(self):
        for status in ('queued', 'running'):
            with self.subTest(status=status):
                self.receipt(status, 'spec', status=status)
                with self.assertRaises(ReceiptError):
                    scan_runs(self.runs)

    def test_corrupt_and_failed_runs_fail_closed(self):
        self.receipt('bad', 'spec', status='failed')
        with self.assertRaises(ReceiptError):
            scan_runs(self.runs)
        (self.runs / 'bad' / 'result.json').write_text('{')
        with self.assertRaises(ReceiptError):
            scan_runs(self.runs)

    def test_missing_predecessor_and_mismatched_id_fail_closed(self):
        self.receipt('build', 'green')
        with self.assertRaises(ReceiptError):
            scan_runs(self.runs)
        result = self.runs / 'build' / 'result.json'
        value = json.loads(result.read_text())
        value.update(id='different', stage='spec')
        result.write_text(json.dumps(value))
        with self.assertRaises(ReceiptError):
            scan_runs(self.runs)

    def test_unaccepted_evidence_cannot_advance(self):
        self.receipt('spec', 'spec')
        result = self.runs / 'spec' / 'result.json'
        value = json.loads(result.read_text())
        value['accepted'] = False
        result.write_text(json.dumps(value))
        with self.assertRaises(ReceiptError):
            scan_runs(self.runs)

    def test_default_scan_rejects_conflicting_receipt_bindings(self):
        self.receipt('spec', 'spec')
        result_path = self.runs / 'spec' / 'result.json'
        status_path = self.runs / 'spec' / 'status.json'
        original_result = json.loads(result_path.read_text())
        original_status = json.loads(status_path.read_text())
        other_digest = 'sha256:' + 'b' * 64
        conflicts = (
            ('status stage', status_path, original_status, 'stage', 'green'),
            ('status attempt', status_path, original_status, 'attempt', 1),
            ('status artifact', status_path, original_status, 'artifact_digest', other_digest),
            ('status acceptance', status_path, original_status, 'accepted', False),
            ('status change', status_path, original_status, 'change_id', 'other-change'),
            ('status stage alias', status_path, original_status, 'evalid_stage', 'green'),
            ('status attempt alias', status_path, original_status, 'evalid_attempt', 1),
            ('result stage alias', result_path, original_result, 'evalid_stage', 'green'),
            ('result attempt alias', result_path, original_result, 'evalid_attempt', 1),
            ('result change alias', result_path, original_result, 'evalid_change_id', 'other-change'),
            ('result artifact alias', result_path, original_result, 'evalid_artifact_digest', other_digest),
        )
        for label, path, original, field, value in conflicts:
            with self.subTest(field=label):
                changed = dict(original)
                changed[field] = value
                path.write_text(json.dumps(changed))
                with self.assertRaises(ReceiptError):
                    scan_runs(self.runs)
                path.write_text(json.dumps(original))

    def test_migrate_real_runs_and_spec_revision(self):
        events = []
        def event(stage, data, **extra):
            events.append({'change_id': 'change-1', 'attempt': 0, 'stage': stage,
                           'input_digest': digest(data), 'data': data, **extra})
        def run(run_id):
            directory = self.runs / run_id
            directory.mkdir()
            (directory / 'result.json').write_text(json.dumps({'id': run_id, 'status': 'done', 'commit': 'abc'}))
            (directory / 'status.json').write_text(json.dumps({'id': run_id, 'state': 'finished', 'status': 'done'}))
        a, b, rejection = (digest(x) for x in ('spec-a', 'spec-b', 'rejection'))
        event('intake', {'issue_ref': '#1973'}, private_holdout={'secret': 'suite'})
        for name in ('z-spec', 'y-check', 'x-spec', 'w-check', 'v-red', 'u-green', 't-review'):
            run(name)
        native_review = self.runs / 't-review' / 'result.json'
        review_receipt = json.loads(native_review.read_text())
        review_receipt.update(stage='delivery', evalid_stage='delivery', evalid_attempt=0,
                              evalid_change_id='change-1',
                              evalid_artifact_digest=digest('review'))
        native_review.write_text(json.dumps(review_receipt))
        event('spec', {'run_id': 'z-spec', 'spec_digest': a, 'spec_version': 1, 'controller_status': 'SPEC_PENDING'})
        event('spec-check', {'run_id': 'y-check', 'verdict': 'FAIL', 'parent_spec_digest': a, 'spec_version': 1, 'evidence_digest': rejection, 'controller_status': 'SPEC_PENDING'})
        event('spec', {'run_id': 'x-spec', 'spec_digest': b, 'spec_version': 2, 'versioned': True, 'parent_receipt': rejection, 'controller_status': 'SPEC_PENDING'})
        event('spec-check', {'run_id': 'w-check', 'verdict': 'PASS', 'parent_spec_digest': b, 'spec_version': 2, 'controller_status': 'SPEC_PENDING'})
        for stage, name in (('red', 'v-red'), ('green', 'u-green')):
            event(stage, {'run_id': name, 'evidence_digest': digest(stage), 'status': 'PASS', 'controller_status': stage.upper() + '_ACCEPTED'})
        event('holdout', {'status': 'PASS', 'controller_status': 'REVIEW_PENDING'}, private_evidence={'proof': 'private'})
        event('review', {'run_id': 't-review', 'evidence_digest': digest('review'), 'verdict': 'PASS', 'controller_status': 'REVIEW_PENDING'})
        event('delivery', {'controller_status': 'VERIFIED'})
        log = self.runs.parent / (self.runs.name + '-events.jsonl')
        self.addCleanup(log.unlink)
        log.write_text(''.join(json.dumps(item) + '\n' for item in events))
        controller = RoleController.__new__(RoleController)
        controller._changes, controller._event_keys = {}, {}
        for item in events:
            controller._reduce(item)
        expected = controller.state('change-1')
        expected['run_receipts'] = [
            {'run_id': item['data']['run_id'], 'stage': item['stage'], 'attempt': 0,
             'status': 'done', 'artifact_digest': item['data'].get('evidence_digest',
                 item['data'].get('spec_digest', item['input_digest']))}
            for item in events if 'run_id' in item['data']]
        self.assertEqual(scan_runs(self.runs, events_path=log)[('change-1', 0)], expected)
        self.assertEqual(scan_runs(self.runs, events_path=log), scan_runs(self.runs, events_path=log))
        self.assertEqual(scan_runs(self.runs, accepted_events=events)[('change-1', 0)], expected)
        self.assertEqual(scan_runs(self.runs, accepted_events=list(reversed(events)))[('change-1', 0)], expected)
        for stage, field, value in (('green', 'status', 'FAIL'),
                                    ('green', 'evidence_digest', None),
                                    ('review', 'verdict', 'queued')):
            with self.subTest(stage=stage, field=field):
                bad = json.loads(json.dumps(events))
                data = next(item['data'] for item in bad if item['stage'] == stage)
                if value is None:
                    del data[field]
                else:
                    data[field] = value
                with self.assertRaises(ReceiptError):
                    scan_runs(self.runs, accepted_events=bad)
        with self.assertRaises(ReceiptError):
            scan_runs(self.runs, accepted_events=events + [events[-1]])
        bad = json.loads(json.dumps(events))
        bad[2]['data']['run_id'] = 'z-spec'
        with self.assertRaises(ReceiptError):
            scan_runs(self.runs, accepted_events=bad)
        bad = json.loads(json.dumps(events))
        bad[3]['data']['parent_receipt'] = digest('wrong rejection')
        with self.assertRaises(ReceiptError):
            scan_runs(self.runs, accepted_events=bad)
        result = self.runs / 'x-spec' / 'result.json'
        result.write_text(json.dumps({'id': 'x-spec', 'status': 'running'}))
        with self.assertRaises(ReceiptError):
            scan_runs(self.runs, events_path=log)

    def test_real_run_with_unordered_accepted_evidence(self):
        directory = self.runs / 'real-run'
        directory.mkdir()
        (directory / 'result.json').write_text(json.dumps(
            {'id': 'real-run', 'status': 'done', 'commit': 'abc', 'backend': 'codex'}))
        (directory / 'status.json').write_text(json.dumps(
            {'id': 'real-run', 'state': 'finished', 'status': 'done'}))
        spec_digest = digest('spec artifact')
        events = [
            {'change_id': 'change-1', 'attempt': 2, 'stage': 'intake',
             'input_digest': digest('intake'), 'data': {'issue_ref': '#1973'}},
            {'change_id': 'change-1', 'attempt': 2, 'stage': 'spec',
             'input_digest': digest('spec evidence'), 'data': {'run_id': 'real-run',
                 'spec_digest': spec_digest, 'spec_version': 1}},
        ]
        state = scan_runs(self.runs, accepted_events=events)[('change-1', 2)]
        self.assertEqual(state['next'], 'spec-check')
        self.assertEqual(state['run_receipts'], [{'run_id': 'real-run', 'stage': 'spec',
            'attempt': 2, 'status': 'done', 'artifact_digest': spec_digest}])
        self.assertEqual(scan_runs(self.runs, accepted_events=events[::-1])[('change-1', 2)], state)
        conflict = json.loads(json.dumps(events[1]))
        conflict['input_digest'] = digest('conflict')
        with self.assertRaises(ReceiptError):
            scan_runs(self.runs, accepted_events=events + [conflict])
        for status in ('queued', 'running'):
            with self.subTest(status=status):
                (directory / 'status.json').write_text(json.dumps(
                    {'id': 'real-run', 'state': status, 'status': status}))
                with self.assertRaises(ReceiptError):
                    scan_runs(self.runs, accepted_events=events[::-1])

    def test_migration_run_stage_requires_matching_actual_run(self):
        events = [
            {'change_id': 'change-1', 'attempt': 0, 'stage': 'intake',
             'input_digest': digest('intake'), 'data': {'issue_ref': '#1973'}},
            {'change_id': 'change-1', 'attempt': 0, 'stage': 'spec',
             'input_digest': digest('spec'), 'data': {
                 'spec_digest': digest('spec artifact'), 'spec_version': 1}},
        ]
        with self.assertRaises(ReceiptError):
            scan_runs(self.runs, accepted_events=events)

    def test_migration_rejects_parent_directory_run_id(self):
        runs = self.runs / 'runs'
        runs.mkdir()
        (self.runs / 'result.json').write_text(json.dumps(
            {'id': '..', 'status': 'done'}))
        (self.runs / 'status.json').write_text(json.dumps(
            {'id': '..', 'state': 'finished', 'status': 'done'}))
        events = [
            {'change_id': 'change-1', 'attempt': 0, 'stage': 'intake',
             'input_digest': digest('intake'), 'data': {'issue_ref': '#1973'}},
            {'change_id': 'change-1', 'attempt': 0, 'stage': 'spec',
             'input_digest': digest('spec'), 'data': {'run_id': '..',
                 'spec_digest': digest('spec artifact'), 'spec_version': 1}},
        ]
        with self.assertRaises(ReceiptError):
            scan_runs(runs, accepted_events=events)

    def test_migration_rejects_invalid_run_paths(self):
        events = [
            {'change_id': 'change-1', 'attempt': 0, 'stage': 'intake',
             'input_digest': digest('intake'), 'data': {}},
            {'change_id': 'change-1', 'attempt': 0, 'stage': 'spec',
             'input_digest': digest('spec'), 'data': {'run_id': '.',
                 'spec_digest': digest('artifact'), 'spec_version': 1}},
        ]
        (self.runs / 'escape').symlink_to(self.runs.parent, target_is_directory=True)
        for run_id in ('.', '..', '/absolute', 'a/b', 'escape'):
            with self.subTest(run_id=run_id):
                events[1]['data']['run_id'] = run_id
                with self.assertRaises(ReceiptError):
                    scan_runs(self.runs, accepted_events=events)

    def test_migration_checks_native_stage_attempt_and_artifact_digest(self):
        directory = self.runs / 'actual-run'
        directory.mkdir()
        (directory / 'status.json').write_text(json.dumps(
            {'id': 'actual-run', 'state': 'finished', 'status': 'done'}))
        result = {'id': 'actual-run', 'status': 'done', 'evalid_stage': 'green',
                  'evalid_attempt': 0, 'artifact_digest': digest('spec artifact')}
        result_path = directory / 'result.json'
        result_path.write_text(json.dumps(result))
        events = [
            {'change_id': 'change-1', 'attempt': 0, 'stage': 'intake',
             'input_digest': digest('intake'), 'data': {'issue_ref': '#1973'}},
            {'change_id': 'change-1', 'attempt': 0, 'stage': 'spec',
             'input_digest': digest('spec'), 'data': {'run_id': 'actual-run',
                 'spec_digest': digest('spec artifact'), 'spec_version': 1}},
        ]
        with self.assertRaises(ReceiptError):
            scan_runs(self.runs, accepted_events=events)

        result['evalid_stage'] = 'spec'
        result['artifact_digest'] = digest('different artifact')
        result_path.write_text(json.dumps(result))
        with self.assertRaises(ReceiptError):
            scan_runs(self.runs, accepted_events=events)

        result['artifact_digest'] = digest('spec artifact')
        result['evalid_attempt'] = 1
        result_path.write_text(json.dumps(result))
        with self.assertRaises(ReceiptError):
            scan_runs(self.runs, accepted_events=events)

    def test_migration_rejects_conflicting_run_identity_acceptance_and_status_stage(self):
        directory = self.runs / 'actual-run'
        directory.mkdir()
        result_path = directory / 'result.json'
        status_path = directory / 'status.json'
        result = {'id': 'actual-run', 'status': 'done', 'change_id': 'change-1',
                  'attempt': 0, 'accepted': True, 'stage': 'spec'}
        status = {'id': 'actual-run', 'state': 'finished', 'status': 'done',
                  'evalid_stage': 'spec'}
        result_path.write_text(json.dumps(result))
        status_path.write_text(json.dumps(status))
        events = [
            {'change_id': 'change-1', 'attempt': 0, 'stage': 'intake',
             'input_digest': digest('intake'), 'data': {'issue_ref': '#1973'}},
            {'change_id': 'change-1', 'attempt': 0, 'stage': 'spec',
             'input_digest': digest('spec'), 'data': {'run_id': 'actual-run',
                 'spec_digest': digest('spec artifact'), 'spec_version': 1}},
        ]
        mismatches = (
            ('attempt', result, 'attempt', 1, result_path),
            ('change_id', result, 'change_id', 'other-change', result_path),
            ('accepted', result, 'accepted', False, result_path),
            ('status stage', status, 'evalid_stage', 'green', status_path),
        )
        for label, original, field, value, path in mismatches:
            with self.subTest(field=label):
                changed = dict(original)
                changed[field] = value
                path.write_text(json.dumps(changed))
                with self.assertRaises(ReceiptError):
                    scan_runs(self.runs, accepted_events=events)
                path.write_text(json.dumps(original))


if __name__ == '__main__':
    unittest.main()
