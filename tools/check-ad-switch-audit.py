#!/usr/bin/env python3
"""Offline contract for the switch audit of the autodispatcher (Refs #1827).

The script runs the real tick of the AD against a temporary switch directory: it touches a
`PAUSE-DISPATCH` switch and shows the audit line the next tick appends. It only touches the
temporary fixture (never the live autodispatch directory) and never contacts GitHub.
"""

import importlib.util
import json
import tempfile
from pathlib import Path
from unittest import mock


def load_autodispatch():
    path = Path(__file__).parent / "lane-autodispatch" / "autodispatch.py"
    spec = importlib.util.spec_from_file_location("autodispatch", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    autodispatch = load_autodispatch()
    scratch = Path(__file__).resolve().parents[1] / ".swrm-tmp"  # gitignored scratch root
    scratch.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=str(scratch)) as fixture:
        root = Path(fixture)
        switches = root / "autodispatch"
        switches.mkdir()
        brain = root / "brain" / "log.md"
        brain.parent.mkdir()
        brain.write_text("")
        with mock.patch.object(autodispatch, "D", str(switches)), \
                mock.patch.object(autodispatch, "BRAIN_LOG", str(brain)), \
                mock.patch.object(autodispatch, "queue", return_value=[]), \
                mock.patch.object(autodispatch, "replenish", return_value=[]):
            # a switch appears out of nowhere: the next tick must leave a trace of it
            (switches / "PAUSE-DISPATCH").touch()
            autodispatch.tick({}, {}, 0)
            audit = switches / "switch-audit.jsonl"
            rows = [json.loads(line) for line in audit.read_text().splitlines() if line.strip()]
            assert len(rows) == 1, rows
            row = rows[0]
            assert row["file"] == "PAUSE-DISPATCH", row
            assert row["action"] == "created", row
            assert row["unattributed"] is True, row
            print(json.dumps(row, ensure_ascii=False))
            alert = switches / "health.alert"
            assert alert.exists(), "an unattributed switch must raise a health alert"
            print(alert.read_text().splitlines()[0])
            # a tick without a change writes no further row
            autodispatch.tick({}, {}, 0)
            rows = [json.loads(line) for line in audit.read_text().splitlines() if line.strip()]
            assert len(rows) == 1, rows
    print("OK: the next tick audits a touched PAUSE-DISPATCH switch")
    print("OK: a tick without a change writes no further audit row")


if __name__ == "__main__":
    main()
