"""Controller role dispatch and finalization, without a real control plane."""
import contextlib
import io
import hashlib
import json
import pathlib
import shlex
import sys
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "bin"))

import _load

xs = _load.load()
roles = xs.evalid_roles


def card(role='evalid-review'):
    return ('---\nevalid_version: 1\nrole: ' + role + '\nchange_id: example\n'
            'input_evidence: ["sha256:' + 'a' * 64 + '"]\n---\npublic task')


def arguments(**overrides):
    values = dict(evalid_stage='delivery', evalid_assignment_index=0,
                  evalid_minimax_unavailable=False, evalid_agy_model=None,
                  evalid_repair_round=None, artifact_sink='/unused-sink', evalid_output='evidence',
                  backend=None, model=None, effort=None, rework_of=None, verify_baseline=False, verify='public verify')
    return SimpleNamespace(**(values | overrides))


class EvalidRoleWiringTests(unittest.TestCase):
    def test_evalid_subprocess_preserves_machine_exit_classes(self):
        with tempfile.TemporaryDirectory() as temp:
            output = pathlib.Path(temp) / 'role-result.json'
            output.write_text(json.dumps({'kind': 'review', 'verdict': 'FAIL'}))
            command = xs.build_evalid_command('xflow', request_file='request.json', output_file=str(output),
                                             python='python3')
            self.assertEqual(command, ['python3', 'xflow', 'role', 'run', '--request', 'request.json',
                                       '--output', str(output)])
            for code in (0, 1, 2, 3):
                result = xs.run_evalid(command, str(output), runner=mock.Mock(
                    return_value=subprocess.CompletedProcess(command, code, '', '')))
                self.assertEqual(result['returncode'], code)
                self.assertEqual(result['result']['verdict'], 'FAIL')

    def test_legacy_yaml_is_not_parsed_as_role_metadata(self):
        for task in ('---\nrole: author\nchange_id: legacy\nnested: {x: [1, 2]}\n---\ntask',
                     '---\nrole: author\nunterminated', 'ordinary task'):
            self.assertEqual(xs.resolve_evalid_task(task, arguments()), (None, None, task))
        with self.assertRaises(roles.RoleError):
            xs.resolve_evalid_task('---\nevalid_version: 1\nrole: typo\n---', arguments())

    def test_controller_header_and_sink_are_mandatory(self):
        with self.assertRaises(roles.RoleError):
            xs.resolve_evalid_task('task', arguments(), {'role': 'evalid-spec'})
        args = arguments(evalid_stage='spec', evalid_change_id='example',
                         evalid_input_evidence=['sha256:' + 'a' * 64])
        lane = dict(role='evalid-spec', backend='codex', model='gpt-6.1-sol', effort='high')
        info, profile, task = xs.resolve_evalid_task('legacy body', args, lane)
        self.assertEqual(info['change_id'], 'example')
        self.assertEqual(info['input_evidence'], args.evalid_input_evidence)
        self.assertIn('legacy body', task)
        for missing in ('evalid_stage', 'artifact_sink', 'evalid_output'):
            with self.subTest(missing=missing), self.assertRaises(roles.RoleError):
                xs.resolve_evalid_task(card(), arguments(**{missing: None}))
        with self.assertRaises(roles.RoleError):
            xs.resolve_evalid_task(card().replace('a' * 64, 'bad'), arguments())
        for output in ('../escape', '/absolute', 'evidence/../escape', '.git/evidence'):
            with self.subTest(output=output), self.assertRaises(roles.RoleError):
                xs.resolve_evalid_task(card(), arguments(evalid_output=output))
        with self.assertRaises(roles.RoleError):
            xs.resolve_evalid_task(card(), arguments(verify_baseline=True))
        with self.assertRaises(roles.RoleError):
            xs.resolve_evalid_task('---\nevalid_version: 1\n', arguments())
        with self.assertRaisesRegex(roles.RoleError, '--verify'):
            xs.resolve_evalid_task(card(), arguments(verify=None))

    def test_card_only_role_accepts_matching_lane_and_rejects_profile_conflicts(self):
        lane = dict(backend='codex', model='gpt-6.1-terra', effort='high')
        info, _, _ = xs.resolve_evalid_task(card(), arguments(), lane)
        self.assertEqual(info['role'], 'evalid-review')
        for conflict in ({'role': 'evalid-spec'}, {'model': 'wrong'}, {'effort': 'low'}):
            with self.subTest(conflict=conflict), self.assertRaises(roles.RoleError):
                xs.resolve_evalid_task(card(), arguments(), lane | conflict)

    def test_all_roles_disable_automatic_review_and_repairs_are_bounded(self):
        for role, stage in [('evalid-spec', 'spec'), ('evalid-tdd', 'red'), ('evalid-review', 'delivery')]:
            args = arguments(evalid_stage=stage)
            xs.resolve_evalid_task(card(role), args)
            self.assertTrue(args.no_auto_review)
        for repair, effort in [(0, 'medium'), (1, 'high')]:
            args = arguments(evalid_stage='green', evalid_repair_round=repair, rework_of='previous')
            _, profile, _ = xs.resolve_evalid_task(card('evalid-tdd'), args)
            self.assertEqual((profile.model, profile.effort), ('gpt-6.1-sol', effort))
            lane = dict(role='evalid-tdd', backend='codex', model=profile.model, effort=effort)
            xs.resolve_evalid_task(card('evalid-tdd'), args, lane)
            with self.assertRaises(roles.RoleError):
                xs.resolve_evalid_task(card('evalid-tdd'), args, lane | {'effort': 'low'})
        with self.assertRaises(roles.NeedsBoss):
            xs.resolve_evalid_task(card('evalid-tdd'), arguments(
                evalid_stage='green', evalid_repair_round=2, rework_of='previous'))
        with self.assertRaises(roles.RoleError):
            xs.resolve_evalid_task(card(), arguments(model='wrong'))

    def test_lane_dispatch_transmits_frozen_repair_and_output_contract(self):
        for repair, effort in [(0, 'medium'), (1, 'high')]:
            lane = dict(name='BUILD', role='evalid-tdd', backend='codex',
                        model='gpt-6.1-sol', effort=effort)
            argv = ['swrm', 'lane', 'assign', 'BUILD', '--prompt', card('evalid-tdd'),
                    '--evalid-stage', 'green', '--evalid-repair-round', str(repair),
                    '--rework-of', 'previous', '--artifact-sink', '/unused-sink',
                    '--evalid-output', 'evidence', '--verify', 'public verify']
            with mock.patch.object(sys, 'argv', argv), mock.patch.object(xs, 'lanes_by_name', return_value=[lane]), \
                 mock.patch.object(xs, 'check_budget'), mock.patch.object(xs, 'lane_call', return_value={'ok': True}) as call, \
                 contextlib.redirect_stdout(io.StringIO()):
                xs.main()
            request = call.call_args.args[1]
            self.assertEqual(request['evalid_repair_round'], repair)
            self.assertEqual(request['evalid_output'], 'evidence')
            self.assertEqual(request['rework_of'], 'previous')

    def test_collection_preserves_runs_and_changed_bytes_and_rejects_symlinks(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            output = root / 'work/evidence'
            output.mkdir(parents=True)
            subprocess.run(['git', 'init', '-q', str(root / 'work')], check=True)
            report = output / 'proof.json'
            report.write_text('first')
            def collect(run='one'):
                return xs.collect_evalid_outputs(root / 'work', root / 'sink', 'evidence', run, 'executor')
            first = collect()
            self.assertEqual(first, collect())
            report.write_text('second')
            second = collect()
            third = collect('two')
            self.assertNotEqual(first[-1]['batch'], second[-1]['batch'])
            self.assertNotEqual(second[0]['batch'], third[0]['batch'])
            self.assertEqual((root / 'sink' / first[0]['batch'] / 'evidence/proof.json').read_text(), 'first')
            (output / 'escape').symlink_to(root)
            with self.assertRaises(roles.RoleError):
                collect()

    def test_inventory_includes_native_ignored_untracked_staged_and_deleted_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            work = root / 'work'
            work.mkdir()
            def git(*args):
                return subprocess.run(['git', '-C', str(work), *args], check=True,
                                      capture_output=True, text=True).stdout
            git('init', '-q')
            (work / '.gitignore').write_text('native/\n')
            (work / 'old.json').write_text('old')
            git('add', '.')
            git('-c', 'user.name=test', '-c', 'user.email=test@example.invalid',
                'commit', '-qm', 'fixture')
            base = git('rev-parse', 'HEAD').strip()
            (work / 'old.json').unlink()
            payloads = {'native/proofs/red-0.json': '{}',
                        'native/snapshots/tree.tar': 'snapshot',
                        'outside/report with spaces.json': 'report',
                        'outside/staged.json': 'staged', 'evidence/unrelated': 'not proof'}
            for name, payload in payloads.items():
                path = work / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(payload)
            git('add', 'outside/staged.json')
            payloads['committed-report.json'] = 'committed evidence'
            (work / 'committed-report.json').write_text(payloads['committed-report.json'])
            git('add', 'committed-report.json')
            git('-c', 'user.name=test', '-c', 'user.email=test@example.invalid',
                'commit', '-qm', 'executor output', '--', 'committed-report.json')
            artifacts = xs.collect_evalid_outputs(work, root / 'sink', 'evidence', 'run', 'executor', base=base)
            by_name = {entry['name']: entry for entry in artifacts}
            for name, payload in payloads.items():
                entry = by_name[name]
                self.assertEqual((root / 'sink' / entry['batch'] / name).read_text(), payload)
                self.assertEqual(entry['sha256'], hashlib.sha256(payload.encode()).hexdigest())
            inventory = by_name['controller-inventory.json']
            status = json.loads((root / 'sink' / inventory['batch'] / inventory['name']).read_text())['porcelain']
            self.assertIn(' D old.json', status)
            self.assertIn('!! native/', status)
            self.assertIn('missing required role artifact', xs.validate_evalid_collection(
                artifacts, root / 'sink', 'delivery', {'rc': 0}))

    def test_tdd_requires_manifest_proofs_snapshots_and_referenced_bytes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            work = root / 'work'
            work.mkdir()
            subprocess.run(['git', 'init', '-q', str(work)], check=True)
            (work / 'evidence').mkdir()
            (work / 'evidence/unrelated').write_text('not a role package')
            def check():
                artifacts = xs.collect_evalid_outputs(work, root / 'sink', 'evidence', 'run', 'verify')
                return xs.validate_evalid_collection(artifacts, root / 'sink', 'red', {'rc': 0})
            self.assertIn('tdd.json', check())
            manifest = work / 'tdd.json'
            manifest.write_text(json.dumps({'phase': 'red', 'proof_refs': ['sha256:' + 'a' * 64]}))
            self.assertIn('unresolved', check())
            (work / 'red-0.json').write_text('{}')
            manifest.write_text(json.dumps({'phase': 'red', 'proof_refs': [
                'sha256:' + hashlib.sha256(b'{}').hexdigest()]}))
            self.assertIn('snapshot', check())
            (work / 'snapshot.tar').write_bytes(b'snapshot')
            self.assertIsNone(check())
            manifest.write_text(json.dumps({'payload': {'proof_refs': [
                'sha256:' + hashlib.sha256(b'{}').hexdigest()]},
                'artifacts': [{'name': 'lost-report', 'sha256': 'f' * 64}]}))
            self.assertIn('missing bytes', check())
            for malformed in ({'artifacts': None}, {'artifacts': [None]},
                              {'artifacts': [{'sha256': None}]}, {'payload': []}):
                manifest.write_text(json.dumps(malformed))
                self.assertIn('role evidence rejected', check())

    def test_finalize_excludes_explicit_and_native_evidence_even_if_staged(self):
        ws = dict(mode='worktree', root='/fixture', base='b' * 40, branch='test',
                  repo='/fixture', linked=[], evalid_output='custom-output')
        source = {'implementation.py', 'tests/fixtures/spec.json', 'src/review.json'}
        names = [*sorted(source), 'custom-output/raw.bin', 'native/proofs/red-0.json',
                 'native/snapshots/tree.tar', 'report.json']
        def git(command, *args, **kwargs):
            if command[:2] == ['diff', 'HEAD']:
                return 0, '\0'.join(names), ''
            if command[0] == 'rev-parse':
                return 0, ws['base'], ''
            return 0, '', ''
        with mock.patch.object(xs, 'git', side_effect=git) as call:
            result = xs.finalize_workspace(ws, 'fixture', 'codex', '/unused', keep=True)
        excluded = set(names) - source
        self.assertEqual(set(result['excluded_files']), excluded)
        reset = next(c.args[0] for c in call.call_args_list if c.args[0][0] == 'reset')
        self.assertEqual(set(reset[4:]), excluded)
        add = next(c.args[0] for c in call.call_args_list if c.args[0][0] == 'add')
        for name in excluded:
            self.assertIn(':(exclude,literal)' + name, add)

    def test_one_shot_and_resident_finalization_collect_before_cleanup_and_after_verify(self):
        # Real git inventories, verify subprocesses, sink/result/status bytes.
        # Only executor, routing, ledger and destructive git transport are mocked.
        cases = [(resident, controlled, failure)
                 for resident in (False, True)
                 for controlled, failure in [(False, None), (True, None), (True, 'missing'),
                                             (True, 'sink-error'), (True, 'verify-error'),
                                             (True, 'verify-null'), (True, 'verify-fail'), (True, 'malformed')]]
        cases.append((True, True, 'no-verify'))
        real_git = xs.git
        for resident, controlled, failure in cases:
            with self.subTest(resident=resident, controlled=controlled, failure=failure), \
                 tempfile.TemporaryDirectory() as temp, contextlib.ExitStack() as stack:
                root = pathlib.Path(temp)
                work = root / 'work'
                output = work / 'evidence'
                output.mkdir(parents=True)
                subprocess.run(['git', 'init', '-q', str(work)], check=True)
                (work / '.gitignore').write_text('native/\nnative-verify/\n')
                (work / 'subject.py').write_text('reviewer edit')
                (output / 'snapshot.json').write_text('unreported snapshot')
                if failure != 'missing':
                    (output / 'review.json').write_text(json.dumps(
                        {'verdict': 'PASS', 'artifacts': [None]} if failure == 'malformed' else {'verdict': 'PASS'}))
                for name in ('native/proofs/proof.json', 'native/snapshots/tree.tar', 'outside/report.json'):
                    path = work / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text('native evidence')
                ws = dict(path=str(work), root=str(work), repo=str(root / 'repo'),
                          mode='worktree', base='b' * 40, branch='test', linked=[])
                cleaned = []
                def cleanup(*args, **kwargs):
                    cleaned.append(True)
                    (work / 'subject.py').write_text('frozen subject')
                    for path in output.glob('*'):
                        path.unlink()
                    return 0, '', ''
                def git(command, *args, **kwargs):
                    if command[0] == 'status':
                        return real_git(command, *args, **kwargs)
                    if not resident and command[0] in ('checkout', 'clean', 'reset'):
                        return cleanup()
                    return 0, '', ''
                code = ("from pathlib import Path; import json; "
                        + ("assert json.loads(Path('evidence/review.json').read_text())['verdict'] == 'PASS'; "
                           if controlled and failure != 'missing' else '')
                        + ("assert Path('subject.py').read_text() == 'frozen subject'; " if controlled else '')
                        + "Path('native-verify').mkdir(exist_ok=True); "
                        "Path('native-verify/report.json').write_text('native verify evidence'); "
                        "print('v' * 2400)"
                        + ('; raise SystemExit(1)' if failure == 'verify-fail' else ''))
                command = shlex.join([sys.executable, '-c', code])
                stack.enter_context(mock.patch.object(xs, 'RUNS', str(root / 'runs')))
                mocks = {}
                for name, kwargs in {
                    'git': {'side_effect': git}, 'lane_git': {'side_effect': cleanup},
                    'append_ledger': {}, 'cleanup_executor_tmp': {},
                    'run_workspace_prepare': {'return_value': None},
                    'link_dependency_dirs': {'return_value': []}, 'exclude_dependency_links': {},
                    'cleanup_worktree': {'side_effect': cleanup},
                    'get_state': {'return_value': {}}, 'probe': {'return_value': {}},
                    'routing_advice': {'return_value': None}, 'admit_budget': {},
                    'quota_used': {'return_value': None}, 'maybe_schedule_auto_review': {},
                    'prepare_workspace': {'return_value': ws},
                    'route': {'return_value': dict(ok=True, backend='codex', model='gpt-6.1-terra', effort='high')},
                    'add_lessons_to_prompt': {'side_effect': lambda prompt, *a: (prompt, [])},
                    'add_patterns_to_prompt': {'side_effect': lambda prompt, *a: (prompt, [])},
                    'run_executor_with_rate_limit': {'return_value': ([dict(seconds=0, error_class=None, meta={})],
                                                                      dict(status='done', files_changed=[]), None)},
                }.items():
                    mocks[name] = stack.enter_context(mock.patch.object(xs, name, **kwargs))
                if failure == 'sink-error':
                    stack.enter_context(mock.patch.object(xs, 'collect_evalid_outputs', side_effect=OSError('sink unavailable')))
                if failure == 'verify-error':
                    stack.enter_context(mock.patch.object(xs, 'run_verify', side_effect=OSError('verify unavailable')))
                if failure == 'verify-null':
                    stack.enter_context(mock.patch.object(xs, 'run_verify', return_value={'rc': None}))
                with contextlib.redirect_stdout(io.StringIO()) as captured:
                    if resident:
                        spec = dict(run_id='resident', task=card(), started=xs.NOW(), budget_minutes=1,
                                    read_only=True, evalid_role='evalid-review' if controlled else None, evalid_stage='delivery',
                                    artifact_sink=str(root / 'sink'), evalid_output='evidence',
                                    verify=None if failure == 'no-verify' else command)
                        result = xs.lane_finalize_run(dict(name='L1', backend='codex', model='gpt-6.1-terra'),
                                                      spec, ws, dict(status='done', files_changed=[]), {}, None, 0, 'events')
                    else:
                        task = card() if controlled else '---\nrole: author\nchange_id: legacy\ncomplex: {x: [1]}\n'
                        argv = ['swrm', 'run', '--prompt', task, '--read-only', '--cwd', str(work),
                                '--verify', command]
                        if controlled:
                            argv += ['--evalid-stage', 'delivery', '--artifact-sink', str(root / 'sink'),
                                     '--evalid-output', 'evidence']
                        stack.enter_context(mock.patch.object(sys, 'argv', argv))
                        with self.assertRaises(SystemExit) as exited:
                            xs.main()
                        self.assertEqual(exited.exception.code, 1 if failure else 0)
                        result = json.loads(captured.getvalue())
                saved = json.loads((pathlib.Path(result['run_dir']) / 'result.json').read_text())
                self.assertEqual(saved, result)
                status = json.loads((pathlib.Path(result['run_dir']) / 'status.json').read_text())
                self.assertEqual(status['state'], 'finished')
                if not controlled:
                    self.assertNotIn('artifacts', result)
                    if not resident:
                        mocks['maybe_schedule_auto_review'].assert_called_once()
                    continue
                mocks['maybe_schedule_auto_review'].assert_not_called()
                ledger = mocks['append_ledger'].call_args.args[0]
                if failure and failure != 'verify-fail':
                    self.assertEqual(result['status'], 'failed')
                    self.assertEqual(result['evalid_receipt']['status'], 'INFRA')
                    self.assertEqual(result['error_class'], 'evalid_evidence_infra')
                    self.assertFalse(result['exec_pass'])
                    self.assertFalse(ledger['exec_pass'])
                    self.assertEqual(len(cleaned), 0 if failure == 'sink-error' else 2)
                    self.assertTrue((output / 'snapshot.json').exists())
                    continue
                self.assertEqual(result['verify']['rc'], 1 if failure else 0)
                self.assertEqual(result['exec_pass'], not bool(failure))
                self.assertEqual(ledger['exec_pass'], result['exec_pass'])
                names = {entry['name'] for entry in result['artifacts']}
                self.assertTrue({'evidence/snapshot.json', 'evidence/review.json',
                                 'evidence/controller-verify.log', 'native-verify/report.json',
                                 'native/proofs/proof.json', 'native/snapshots/tree.tar', 'outside/report.json',
                                 'controller-inventory.json', 'controller-verify.json'} <= names)
                for entry in result['artifacts']:
                    copied = root / 'sink' / entry['batch'] / entry['name']
                    self.assertEqual(hashlib.sha256(copied.read_bytes()).hexdigest(), entry['sha256'])
                    if entry['name'] == 'evidence/controller-verify.log':
                        self.assertEqual(copied.read_text(), 'v' * 2400 + '\n')
                self.assertTrue(cleaned)


if __name__ == '__main__':
    unittest.main()
