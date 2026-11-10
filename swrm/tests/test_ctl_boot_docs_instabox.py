"""The control-plane and dual-mode docs must not document the retired Instabox fleet (Refs #1877).

The Instabox fleet is retired: its SSH certificate renewal loop (`ib-refresh`),
the supervisor that watched it (`ctl-loop`), the heartbeat and status files they
wrote and the probe counter they fed belong to a fleet that no longer exists.
The boot runbook (`docs/ctl-boot.md`), the boot check script
(`docs/check-ctl-boot.sh`), the dual-mode runbooks (`docs/dual-mode-ssh.md`,
`docs/dual-mode-checklist.md`), the dual-mode status note
(`brain/dual-mode-status.md`) and the backlog cards still described that fleet,
so an operator reading them looks for loops, heartbeats and probe counters that
no longer exist.

This suite is the reproduction of that removal (issue #1877, PR #2115): every
test that names the retired fleet fails on the tree that still documents it and
passes on the tree that dropped it.  The remaining tests are guards, because
the removal must not take the loops that stay with it.

Everything runs offline: the suite reads the documents of the checkout and runs
the offline self-test of the boot check script.  It never opens a connection
and never touches a control-plane directory.

    cd swrm && python3 -m unittest discover -s tests -p 'test_ctl_boot_docs_instabox.py' -v
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs"

CTL_BOOT_DOC = DOCS / "ctl-boot.md"
CTL_BOOT_CHECK = DOCS / "check-ctl-boot.sh"
DUAL_MODE_SSH = DOCS / "dual-mode-ssh.md"
DUAL_MODE_CHECKLIST = DOCS / "dual-mode-checklist.md"
DUAL_MODE_STATUS = ROOT / "brain" / "dual-mode-status.md"

# The card that asked for the certificate renewal preflight: it described a
# loop of the retired fleet and is gone with it.
RETIRED_BACKLOG_CARD = DOCS / "backlog" / "next" / "20261101-1140-03.md"

# The card that survived the removal: it may not mention the retired fleet
# either.
KEPT_BACKLOG_CARD = DOCS / "backlog" / "next" / "20261101-0643-17.md"

# The certificate renewal loop, its supervisor and the fleet they served.
RETIRED_LOOP_TOKENS = ("ib-refresh", "ctl-loop")

# The retired fleet itself, named in prose.
RETIRED_FLEET_TOKENS = RETIRED_LOOP_TOKENS + ("instabox",)

# The probe the retired loop ran: the switch that enabled it and the counter
# of the healthy hosts it fed.  The `hosts_ok` counter itself stays in the
# boot runbook: the health loop still reports it.
CTL_BOOT_PROBE_SWITCH = ("ssh_probe",)
DUAL_MODE_PROBE_TOKENS = ("hosts_ok", "ssh_probe")

# The offline checks that drove the retired fleet.
RETIRED_CHECK_SCRIPTS = (
    "check-ctl-loop-reap.py",
    "check-ctl-hb-refresh.py",
    "check-fleet-batchmode-probe.py",
    "check-dual-hosts-ok.py",
    "check-soak-summary.py",
)

# The loops that stay: the removal must not take them with it.
REMAINING_LOOPS = ("lane-autodispatch", "lane-merge", "bus-worker-loop", "health-loop")


class RetiredInstaboxFleetDocumentationTests(unittest.TestCase):
    """The documents of the control plane and of the dual mode."""

    def assert_no_tokens(self, path, tokens):
        """The document names none of the retired tokens."""
        lines = path.read_text(encoding="utf-8").splitlines()
        for number, line in enumerate(lines, start=1):
            lowered = line.lower()
            for token in tokens:
                if token in lowered:
                    self.fail(
                        f"{path.relative_to(ROOT)}:{number} still documents {token}: "
                        f"{line.strip()}"
                    )

    # ------------------------------------------------------------------ the tests

    def test_the_boot_runbook_drops_the_retired_certificate_loop(self):
        """`docs/ctl-boot.md` describes no loop of the retired fleet."""
        self.assert_no_tokens(CTL_BOOT_DOC, RETIRED_FLEET_TOKENS)

    def test_the_boot_runbook_drops_the_probe_switch_of_the_retired_fleet(self):
        """`docs/ctl-boot.md` offers no probe switch of the retired fleet."""
        self.assert_no_tokens(CTL_BOOT_DOC, CTL_BOOT_PROBE_SWITCH)

    def test_the_boot_runbook_drops_the_offline_checks_of_the_retired_fleet(self):
        """`docs/ctl-boot.md` advertises no offline check of the retired fleet."""
        text = CTL_BOOT_DOC.read_text(encoding="utf-8")
        for script in RETIRED_CHECK_SCRIPTS:
            with self.subTest(script=script):
                self.assertNotIn(
                    script,
                    text,
                    f"docs/ctl-boot.md still advertises {script}",
                )

    def test_the_boot_check_script_drops_the_retired_binaries(self):
        """`docs/check-ctl-boot.sh` looks for no binary of the retired fleet."""
        self.assert_no_tokens(CTL_BOOT_CHECK, RETIRED_FLEET_TOKENS)

    def test_the_dual_mode_ssh_runbook_drops_the_retired_probe_contract(self):
        """`docs/dual-mode-ssh.md` describes no probe of the retired fleet."""
        self.assert_no_tokens(DUAL_MODE_SSH, RETIRED_LOOP_TOKENS + DUAL_MODE_PROBE_TOKENS)

    def test_the_dual_mode_checklist_drops_the_retired_certificate_hint(self):
        """`docs/dual-mode-checklist.md` sends nobody to a retired certificate."""
        self.assert_no_tokens(DUAL_MODE_CHECKLIST, RETIRED_FLEET_TOKENS)

    def test_the_dual_mode_status_note_drops_the_retired_loop(self):
        """`brain/dual-mode-status.md` reports no loop of the retired fleet."""
        self.assert_no_tokens(DUAL_MODE_STATUS, RETIRED_FLEET_TOKENS)

    def test_the_backlog_card_of_the_retired_loop_is_removed(self):
        """The card that asked for the certificate preflight is gone with it."""
        self.assertFalse(
            RETIRED_BACKLOG_CARD.exists(),
            f"{RETIRED_BACKLOG_CARD.relative_to(ROOT)} still asks for the retired loop",
        )

    def test_the_kept_backlog_card_names_no_retired_host(self):
        """The card that survived the removal names no host of the retired fleet."""
        self.assert_no_tokens(KEPT_BACKLOG_CARD, ("instabox",))


class RetiredInstaboxFleetRemovalGuardsTests(unittest.TestCase):
    """The removal takes the retired fleet with it and nothing else."""

    def test_the_boot_runbook_keeps_the_loops_that_remain(self):
        """`docs/ctl-boot.md` still documents the loops that stay."""
        text = CTL_BOOT_DOC.read_text(encoding="utf-8")
        for loop in REMAINING_LOOPS:
            with self.subTest(loop=loop):
                self.assertIn(loop, text, f"docs/ctl-boot.md dropped {loop}")
        for path in ("autodispatch/heartbeat", "bus-worker/heartbeat", "start-control-plane"):
            with self.subTest(path=path):
                self.assertIn(path, text, f"docs/ctl-boot.md dropped {path}")

    def test_the_boot_check_script_keeps_the_loops_that_remain(self):
        """`docs/check-ctl-boot.sh` still verifies the loops that stay."""
        text = CTL_BOOT_CHECK.read_text(encoding="utf-8")
        for term in ("lane-autodispatch", "bus-worker", "autodispatch/heartbeat",
                     "bus-worker/heartbeat", "start-control-plane"):
            with self.subTest(term=term):
                self.assertIn(term, text, f"docs/check-ctl-boot.sh dropped {term}")

    def test_the_retirement_of_the_fleet_stays_recorded(self):
        """The runbook still says that the fleet is retired."""
        text = DUAL_MODE_SSH.read_text(encoding="utf-8")
        self.assertIn("Instabox", text, "docs/dual-mode-ssh.md forgot the retired fleet")
        self.assertIn("退役", text, "docs/dual-mode-ssh.md forgot the retirement")

    def test_the_boot_check_script_still_passes_its_own_self_test(self):
        """The boot check script keeps working with the loops that remain."""
        bash = shutil.which("bash")
        if bash is None:
            self.skipTest("bash is not available")
        with tempfile.TemporaryDirectory(prefix="ctl-boot-self-test-") as scratch:
            env = {**os.environ, "TMPDIR": scratch}
            proc = subprocess.run(
                [bash, str(CTL_BOOT_CHECK), "--self-test"],
                capture_output=True,
                text=True,
                env=env,
                timeout=120,
            )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)


if __name__ == "__main__":
    unittest.main()
