#!/usr/bin/env python3
"""E2E contract for skipping active PAUSE-HOST cooldowns (Refs #904).

Deprecated (Refs #1480): the Instabox nodes are offline since 2026-11-02, so this
check only keeps the historical instabox refresh contract alive.  It no longer
describes the live fleet topology (see docs/fleet-topology-expected.md).
"""

import datetime as dt
import json
from pathlib import Path
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[1]
REFRESH = ROOT / "tools" / "ib-refresh.py"


def main() -> None:
    with tempfile.TemporaryDirectory(dir=ROOT, prefix="ib-cooldown-skip-") as fixture:
        root = Path(fixture)
        pause_dir = root / "autodispatch"
        pause_dir.mkdir()
        calls = root / "calls"
        fake_cli = root / "fake-cli"
        fake_cli.write_text(
            "#!/bin/sh\n"
            f"printf '%s\\n' \"$*\" >> {calls}\n"
            "exit 99\n"
        )
        fake_cli.chmod(0o755)

        until = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)).isoformat().replace("+00:00", "Z")
        (pause_dir / "PAUSE-HOST-ib2").write_text(f"auto:cooldown:until={until}\n")
        fleet = root / "fleet.json"
        fleet.write_text(json.dumps({
            "need_ok": 0,
            "hosts": [
                {"name": "ib2", "cert": "ib2.crt"},
            ],
        }))
        # ib2 is intentionally absent. The fake CLI must not receive ib2, and
        # no resume/wake helper is called.
        result = subprocess.run(
            ["python3", str(REFRESH), "--fleet-config", str(fleet),
             "--renew-command", str(fake_cli), "--pause-dir", str(pause_dir)],
            capture_output=True, text=True, check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        payload = json.loads(result.stdout)
        assert payload["hosts"][0]["status"] == "cooldown"
        fields = dict(field.split("=", 1) for field in payload["status_line"].split())
        assert fields["skipped_cooldown"] == "1"
        assert not calls.exists(), "active cooldown host must not invoke resume/wake/renew CLI"
        assert (pause_dir / "PAUSE-HOST-ib2").exists(), "check must not modify the PAUSE file"
        print("ib-refresh active cooldown skip: OK")
        print("deprecated: instabox nodes are offline; this contract is history only (Refs #1480)")


if __name__ == "__main__":
    main()
