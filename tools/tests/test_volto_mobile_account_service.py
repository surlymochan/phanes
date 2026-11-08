"""Regression guard for the mobile account service of a self-hosted build (Refs #10).

Issue #10 asks for an iOS build that is compiled by the operator or uploaded to
TestFlight to be able to point at a self-hosted account service. The account
service of the phone is the service that holds the account, sends the one-time
code of the email sign-in and hands out the session, so a self-hosted account
service is only useful to the phone when the phone asks that service.

The mobile app names the account service in the build environment. The probe of
this guard runs one email sign-in of the real mobile app against a stubbed
network and reports the account service the app asked for.

What the guard holds
--------------------
* A release build - a self-compiled or TestFlight build has no `__DEV__` - uses
  the account service named in the environment.
* A development build keeps using the account service named in the environment.
* A build that names no account service keeps using the hosted account service.
* An account service address that is not a secure origin is not used.

The probe loads the real sources of the `volto/` tree. It needs a Node runtime
that can load TypeScript sources, which the probe reports as a skip when the
runtime is too old. The last test reads the source of the mobile app instead,
so the release build rule is held in every environment.

    python3 -m unittest discover -s tools/tests -p 'test_volto_mobile_account_service.py'
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
VOLTO_DIR = REPO_ROOT / "volto"
MOBILE_AUTH = VOLTO_DIR / "apps/mobile/src/features/auth/api/mobile-auth.ts"
HARNESS = Path(__file__).resolve().parent / "fixtures" / "mobile-account-service-harness.mjs"

HOSTED_ACCOUNT_SERVICE = "https://api.openbot.run"
CURRENT_ENV_NAME = "EXPO_PUBLIC_VOLTO_API_URL"
LEGACY_ENV_NAME = "EXPO_PUBLIC_OPENBOT_API_URL"
ENV_NAMES = (CURRENT_ENV_NAME, LEGACY_ENV_NAME)

# The probe loads TypeScript sources, which needs the transform of Node 22.7.
MINIMUM_NODE = (22, 7)
NODE_TIMEOUT_SECONDS = 120

STATEMENT_START = re.compile(r"^(export\s+)?(async\s+)?function\s|^(export\s+)?const\s")


def node_version() -> tuple[int, int] | None:
    """Return the version of the Node runtime of the machine, if there is one."""
    executable = shutil.which("node")
    if not executable:
        return None
    try:
        finished = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"v(\d+)\.(\d+)", finished.stdout)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def node_runtime() -> str | None:
    """Return the Node runtime that can load the sources of the tree."""
    executable = shutil.which("node")
    if not executable:
        return None
    version = node_version()
    if version is None or version < MINIMUM_NODE:
        return None
    return executable


def account_service_requests(
    *,
    dev: bool,
    api_url: str | None,
    env_name: str = CURRENT_ENV_NAME,
) -> list[str]:
    """Return the account service addresses one email sign-in asked for."""
    executable = node_runtime()
    if executable is None:
        raise unittest.SkipTest("no Node runtime that can load the TypeScript sources")
    environment = dict(os.environ)
    environment["VOLTO_ROOT"] = str(VOLTO_DIR)
    environment["VOLTO_MOBILE_TEST_DEV"] = "1" if dev else "0"
    environment["VOLTO_MOBILE_TEST_ENV"] = env_name
    environment.pop("VOLTO_MOBILE_TEST_URL", None)
    if api_url:
        environment["VOLTO_MOBILE_TEST_URL"] = api_url
    finished = subprocess.run(
        [executable, "--experimental-transform-types", "--no-warnings", str(HARNESS)],
        capture_output=True,
        text=True,
        timeout=NODE_TIMEOUT_SECONDS,
        env=environment,
        check=False,
    )
    if finished.returncode != 0:
        raise AssertionError(f"the probe failed:\n{finished.stderr.strip()}")
    lines = [line for line in finished.stdout.splitlines() if line.strip()]
    if not lines:
        raise AssertionError("the probe reported no result")
    report = json.loads(lines[-1])
    return [request["url"] for request in report["requests"]]


def enclosing_block(lines: list[str], index: int) -> list[str]:
    """Return the lines of the block that holds the given line."""
    start = None
    for position in range(index, -1, -1):
        line = lines[position]
        if line.strip() and not line[:1].isspace() and STATEMENT_START.match(line):
            start = position
            break
    if start is None:
        return lines[index : index + 1]
    depth = 0
    for position in range(start, len(lines)):
        depth += lines[position].count("{") - lines[position].count("}")
        if position > start and depth <= 0:
            return lines[start : position + 1]
    return lines[start:]


def account_service_source() -> str:
    """Return the source of the mobile account service module."""
    if not MOBILE_AUTH.is_file():
        raise AssertionError(f"the mobile account service module is missing: {MOBILE_AUTH}")
    return MOBILE_AUTH.read_text(encoding="utf-8")


class MobileAccountServiceTest(unittest.TestCase):
    """The account service a mobile build talks to."""

    def test_release_build_uses_the_configured_account_service(self) -> None:
        """A self-compiled or TestFlight build asks the account service it names."""
        requests = account_service_requests(
            dev=False,
            api_url="https://accounts.example.com",
        )
        self.assertEqual(
            requests,
            ["https://accounts.example.com/v1/auth/email/start"],
            "a release build must use the account service named in its environment",
        )

    def test_release_build_uses_the_legacy_configured_account_service(self) -> None:
        """The released environment name still names the account service."""
        requests = account_service_requests(
            dev=False,
            api_url="https://accounts.example.com",
            env_name=LEGACY_ENV_NAME,
        )
        self.assertEqual(
            requests,
            ["https://accounts.example.com/v1/auth/email/start"],
            "a release build must use the account service named in its environment",
        )

    def test_development_build_uses_the_configured_account_service(self) -> None:
        """A development build keeps asking the account service it names."""
        requests = account_service_requests(
            dev=True,
            api_url="https://accounts.example.com",
        )
        self.assertEqual(
            requests,
            ["https://accounts.example.com/v1/auth/email/start"],
            "a development build must use the account service named in its environment",
        )

    def test_release_build_without_configuration_uses_the_hosted_account_service(self) -> None:
        """A build that names no account service keeps the hosted one."""
        requests = account_service_requests(dev=False, api_url=None)
        self.assertEqual(
            requests,
            [f"{HOSTED_ACCOUNT_SERVICE}/v1/auth/email/start"],
            "a build without an account service must keep the hosted account service",
        )

    def test_release_build_refuses_an_insecure_account_service(self) -> None:
        """An account service that is not a secure origin is not used."""
        requests = account_service_requests(
            dev=False,
            api_url="http://accounts.example.com",
        )
        for request in requests:
            self.assertFalse(
                request.startswith("http://accounts.example.com"),
                f"the account service must not be used over plain HTTP: {request}",
            )

    def test_account_service_is_not_gated_on_the_development_flag(self) -> None:
        """The account service choice does not depend on the kind of build."""
        source = account_service_source()
        lines = source.splitlines()
        gated = []
        for index, line in enumerate(lines):
            if "__DEV__" not in line:
                continue
            block = "\n".join(enclosing_block(lines, index))
            if any(name in block for name in ENV_NAMES) or "VOLTO_CONTROL_PLANE_ORIGIN" in block:
                gated.append(index + 1)
        self.assertEqual(
            gated,
            [],
            "the account service of a release build must not depend on the development flag",
        )
