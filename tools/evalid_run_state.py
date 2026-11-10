"""Rebuild an evalid attempt from immutable, accepted run receipts.

For existing controller installations, ``events_path`` or ``accepted_events``
supplies accepted evidence and run bindings; swrm results alone do not contain
those fields. The legacy standalone format is also supported.
"""

from __future__ import annotations

import json
import re
from pathlib import Path


STAGES = ('spec', 'spec-check', 'red', 'green', 'holdout', 'review', 'delivery')
_DIGEST = re.compile(r'sha256:[0-9a-f]{64}\Z')
_CHANGE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]*\Z')
_RUN_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z')
_NATIVE_STAGES = {'delivery': 'review'}
_RUN_STAGES = frozenset(('spec', 'spec-check', 'red', 'green', 'review'))


class ReceiptError(ValueError):
    """Run evidence is incomplete, inconsistent, or cannot authorize progress."""


def _read(path: Path) -> dict:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate JSON key')
            result[key] = value
        return result

    try:
        if path.is_symlink():
            raise ValueError('symbolic link')
        value = json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=unique)
        if not isinstance(value, dict):
            raise ValueError('expected object')
        return value
    except (OSError, UnicodeError, ValueError) as error:
        raise ReceiptError(f'invalid receipt: {path}') from error


def _validate_run_bindings(receipt: dict, change: str, attempt: int,
                           stage: str, artifact: str) -> None:
    """Reject any receipt field that conflicts with its normalized binding."""
    for field in ('change_id', 'evalid_change_id'):
        if field in receipt and (not isinstance(receipt[field], str) or
                                 receipt[field] != change):
            raise ValueError('run change mismatch')
    for field in ('attempt', 'evalid_attempt'):
        if field in receipt and (type(receipt[field]) is not int or
                                 receipt[field] != attempt):
            raise ValueError('run attempt mismatch')
    for field in ('stage', 'evalid_stage'):
        if field in receipt:
            value = receipt[field]
            if value != stage and _NATIVE_STAGES.get(value) != stage:
                raise ValueError('run stage mismatch')
    for field in ('artifact_digest', 'evalid_artifact_digest'):
        if field in receipt and (not isinstance(receipt[field], str) or
                                 receipt[field] != artifact):
            raise ValueError('run artifact mismatch')
    if 'accepted' in receipt and receipt['accepted'] is not True:
        raise ValueError('unaccepted run')


def _migrate_runs(root: Path, events_path: Path | None, accepted_events: list | None) -> dict:
    """Validate legacy accepted evidence against actual runs and replay it.

    The log is migration input, not proof that a referenced run completed.
    Events without a run (intake, local evidence, delivery) remain necessary
    to reconstruct the controller's private state.
    """
    from tools.evalid_roles_control import RoleController, Rejected

    controller = RoleController.__new__(RoleController)
    controller._changes, controller._event_keys = {}, {}
    histories, used_runs, run_receipts = {}, set(), {}
    try:
        def unique(pairs):
            value = {}
            for key, item in pairs:
                if key in value:
                    raise ValueError('duplicate JSON key')
                value[key] = item
            return value
        if events_path is not None:
            if events_path.is_symlink():
                raise ValueError('symbolic link')
            events = [json.loads(line, object_pairs_hook=unique)
                      for line in events_path.read_text(encoding='utf-8').splitlines()
                      if line.strip()]
        else:
            if not isinstance(accepted_events, list):
                raise ValueError('invalid accepted evidence')
            events = accepted_events
        # Validate the envelope before ordering it; Spec and its check form
        # successive pairs identified by the controller's Spec version.
        for event in events:
            if not isinstance(event, dict):
                raise ValueError('invalid event')
            change, attempt = event['change_id'], event['attempt']
            stage, data = event['stage'], event['data']
            if (not isinstance(change, str) or not _CHANGE.fullmatch(change) or
                    type(attempt) is not int or attempt < 0 or
                    stage not in ('intake', *STAGES) or not isinstance(data, dict) or
                    not isinstance(event.get('input_digest'), str) or
                    not _DIGEST.fullmatch(event['input_digest'])):
                raise ValueError('invalid event fields')
            if stage in ('spec', 'spec-check') and (type(data.get('spec_version')) is not int or
                                                    data['spec_version'] < 1):
                raise ValueError('invalid spec version')

        def order(event):
            stage, data = event['stage'], event['data']
            if stage == 'intake':
                position = (0, 0, 0)
            elif stage in ('spec', 'spec-check'):
                position = (1, data['spec_version'], stage == 'spec-check')
            else:
                position = (2, STAGES.index(stage), 0)
            return (event['change_id'], event['attempt'], *position)

        for event in sorted(events, key=order):
            change, attempt = event['change_id'], event['attempt']
            stage, data = event['stage'], event['data']
            key = (change, attempt)
            history = histories.setdefault(key, [])
            expected = RoleController._expected_stage(history) if history else 'intake'
            if stage != expected or RoleController._key(event) in controller._event_keys:
                raise ValueError('conflicting or out-of-order event')
            if stage == 'spec':
                controller._validate_spec_append(event, history)
                if not isinstance(data.get('spec_digest'), str) or not _DIGEST.fullmatch(data['spec_digest']):
                    raise ValueError('invalid spec digest')
            if 'evidence_digest' in data and (not isinstance(data['evidence_digest'], str) or
                                              not _DIGEST.fullmatch(data['evidence_digest'])):
                raise ValueError('invalid evidence digest')
            if stage == 'spec-check':
                specs = [item for item in history if item['stage'] == 'spec']
                if (not specs or data.get('parent_spec_digest') != specs[-1]['data'].get('spec_digest') or
                        data.get('spec_version') != len(specs) or data.get('verdict') not in ('PASS', 'FAIL')):
                    raise ValueError('invalid spec-check binding')
            if stage == 'green' and (data.get('status') != 'PASS' or
                                     not _DIGEST.fullmatch(data.get('evidence_digest', '') or '')):
                raise ValueError('invalid green evidence')
            if stage == 'review' and data.get('verdict') not in ('PASS', 'FAIL', 'NEEDS-BOSS'):
                raise ValueError('invalid review verdict')
            run_id = data.get('run_id')
            if stage in _RUN_STAGES and run_id is None:
                raise ValueError('missing run id')
            if run_id is not None:
                if (not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id) or
                        run_id in used_runs):
                    raise ValueError('invalid or duplicate run id')
                directory = root / run_id
                if (directory.is_symlink() or not directory.is_dir() or
                        directory.resolve().parent != root.resolve()):
                    raise ValueError('missing or escaping run')
                result, status = _read(directory / 'result.json'), _read(directory / 'status.json')
                if (result.get('id') != run_id or result.get('status') != 'done' or
                        status.get('id') != run_id or status.get('state') != 'finished' or
                        status.get('status') != 'done'):
                    raise ValueError('invalid or nonterminal run')
                artifact = data.get('evidence_digest', data.get('spec_digest', event['input_digest']))
                if not isinstance(artifact, str) or not _DIGEST.fullmatch(artifact):
                    raise ValueError('invalid artifact digest')
                for receipt in (result, status):
                    _validate_run_bindings(receipt, change, attempt, stage, artifact)
                used_runs.add(run_id)
                run_receipts.setdefault(key, []).append({'run_id': run_id, 'stage': stage,
                    'attempt': attempt, 'status': 'done', 'artifact_digest': artifact})
            history.append(event)
            controller._reduce(event)
        # A run lacking accepted evidence is ambiguous and cannot authorize progress.
        for directory in root.iterdir():
            if directory.is_symlink() or not directory.is_dir() or directory.name not in used_runs:
                raise ValueError('unmatched run')
    except (OSError, UnicodeError, ValueError, KeyError, TypeError, Rejected) as error:
        raise ReceiptError('invalid migration evidence') from error
    states = {}
    for key in sorted(histories):
        states[key] = controller.state(*key)
        states[key]['run_receipts'] = run_receipts.get(key, [])
    return states


def scan_runs(runs_dir: str | Path, *, events_path: str | Path | None = None,
              accepted_events: list | None = None) -> dict:
    """Return deterministic states keyed by (change_id, attempt), or fail closed.

    Migration evidence reconstructs the controller state including revisions;
    standalone receipts require one receipt per stage. Gaps, failures and
    in-flight runs prevent recovery from authorizing work.
    """
    root = Path(runs_dir)
    if root.is_symlink() or not root.is_dir():
        raise ReceiptError('runs directory missing or invalid')
    if events_path is not None and accepted_events is not None:
        raise ReceiptError('ambiguous accepted evidence')
    if events_path is not None or accepted_events is not None:
        return _migrate_runs(root, Path(events_path) if events_path is not None else None,
                             accepted_events)
    grouped = {}
    try:
        entries = sorted(root.iterdir(), key=lambda path: path.name)
        for directory in entries:
            if directory.is_symlink() or not directory.is_dir():
                raise ReceiptError(f'invalid run directory: {directory}')
            result = _read(directory / 'result.json')
            status = _read(directory / 'status.json')
            run_id = result.get('id')
            change = result.get('change_id')
            attempt = result.get('attempt')
            stage = result.get('stage')
            artifact = result.get('artifact_digest')
            if (not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id) or
                    run_id != directory.name or
                    status.get('id') != run_id or status.get('state') != 'finished' or
                    status.get('status') != 'done' or result.get('status') != 'done' or
                    result.get('accepted') is not True or
                    not isinstance(change, str) or not _CHANGE.fullmatch(change) or
                    type(attempt) is not int or attempt < 0 or stage not in STAGES or
                    not isinstance(artifact, str) or not _DIGEST.fullmatch(artifact)):
                raise ReceiptError(f'invalid or nonterminal run: {directory}')
            _validate_run_bindings(result, change, attempt, stage, artifact)
            _validate_run_bindings(status, change, attempt, stage, artifact)
            stages = grouped.setdefault((change, attempt), {})
            if stage in stages:
                raise ReceiptError(f'conflicting stage receipts: {change}/{attempt}/{stage}')
            stages[stage] = {'run_id': run_id, 'stage': stage, 'attempt': attempt,
                             'status': 'done', 'artifact_digest': artifact}
    except ReceiptError:
        raise
    except (OSError, ValueError, TypeError) as error:
        raise ReceiptError('invalid run receipt') from error

    states = {}
    for key in sorted(grouped):
        stages = grouped[key]
        count = len(stages)
        if set(stages) != set(STAGES[:count]):
            raise ReceiptError(f'missing predecessor: {key}')
        states[key] = {'change_id': key[0], 'attempt': key[1],
                       'next': STAGES[count] if count < len(STAGES) else 'complete',
                       'receipts': [stages[stage] for stage in STAGES[:count]]}
    return states
