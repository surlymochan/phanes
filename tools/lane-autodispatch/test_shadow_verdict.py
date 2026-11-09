"""Shadow evidence is append-only and never a merge authority."""
import importlib.util
import json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import pytest

spec = importlib.util.spec_from_file_location('shadow_ad', Path(__file__).with_name('autodispatch.py'))
ad = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ad)


def record(path, **changes):
    fields = dict(pr=1, lane_verdict='pass', tester_verdict='fail',
                  timestamp='2026-01-01T00:00:00Z')
    fields.update(changes)
    return ad.record_shadow_verdict(path, **fields)


def test_append_idempotence_and_backfill(tmp_path):
    path = tmp_path / 'shadow.jsonl'
    assert record(path)
    first = path.read_bytes()
    assert not record(path, timestamp='2026-01-02T00:00:00+00:00')
    assert path.read_bytes() == first
    assert ad.shadow_verdict_stats(path) == dict(samples=1, disagreements=1, resolved=0,
        lane_hits=0, tester_hits=0, lane_accuracy=None, tester_accuracy=None)
    assert record(path, final_outcome='fail')
    filled = path.read_bytes()
    assert filled.startswith(first)
    assert not record(path, final_outcome='fail')
    assert path.read_bytes() == filled
    stats = ad.shadow_verdict_stats(path)
    assert stats == dict(samples=1, disagreements=1, resolved=1, lane_hits=0,
                        tester_hits=1, lane_accuracy=0.0, tester_accuracy=1.0)
    with pytest.raises(ValueError):
        record(path, final_outcome='pass')
    assert path.read_bytes() == filled


@pytest.mark.parametrize('changes', [dict(pr=True), dict(pr=0), dict(pr='1'),
    dict(lane_verdict='approve'), dict(tester_verdict='unknown'),
    dict(timestamp='yesterday'), dict(timestamp='2026-01-01T00:00:00'),
    dict(final_outcome='merged'), dict(final_outcome='insufficient_evidence')])
def test_invalid_records_do_not_write(tmp_path, changes):
    path = tmp_path / 'shadow.jsonl'
    with pytest.raises(ValueError):
        record(path, **changes)
    assert not path.exists()


def test_insufficient_evidence_and_concurrent_retries(tmp_path):
    path = tmp_path / 'shadow.jsonl'
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: record(path, tester_verdict='insufficient_evidence'), range(8)))
    assert sum(results) == 1
    record(path, tester_verdict='insufficient_evidence', final_outcome='pass')
    stats = ad.shadow_verdict_stats(path)
    assert (stats['samples'], stats['lane_hits'], stats['tester_hits']) == (1, 1, 0)
    with pytest.raises(ValueError):
        record(path, tester_verdict='pass')
    assert len(path.read_text().splitlines()) == 2


def test_io_and_corrupt_log_fail_without_merge(tmp_path, monkeypatch):
    monkeypatch.setattr(ad, 'sh', lambda *a, **k: pytest.fail('external action'))
    with pytest.raises(OSError):
        record(tmp_path / 'missing' / 'shadow.jsonl')
    path = tmp_path / 'shadow.jsonl'
    path.write_text('{broken')
    with pytest.raises(ValueError):
        record(path)
    assert path.read_text() == '{broken'
    assert ad.shadow_verdict_stats(tmp_path / 'absent') ['samples'] == 0


def test_failed_flush_never_calls_external_actions(tmp_path, monkeypatch):
    monkeypatch.setattr(ad, 'sh', lambda *a, **k: pytest.fail('external action'))
    def fail_sync(_):
        raise OSError('simulated storage failure')
    monkeypatch.setattr(ad.os, 'fsync', fail_sync)
    with pytest.raises(OSError, match='storage failure'):
        record(tmp_path / 'shadow.jsonl')


def test_multiple_samples_and_initial_final_outcome(tmp_path):
    path = tmp_path / 'shadow.jsonl'
    record(path, final_outcome='pass')
    record(path, pr=2, lane_verdict='fail', tester_verdict='fail', final_outcome='fail')
    record(path, pr=3, tester_verdict='pass')
    stats = ad.shadow_verdict_stats(path)
    assert stats == dict(samples=3, disagreements=1, resolved=2, lane_hits=2,
                        tester_hits=1, lane_accuracy=1.0, tester_accuracy=0.5)
    assert all(isinstance(json.loads(line), dict) for line in path.read_text().splitlines())
