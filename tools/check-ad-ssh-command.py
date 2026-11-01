#!/usr/bin/env python3
"""Offline check for the autodispatch jump-host ProxyCommand quoting contract (Refs #907).

The jump topology is not hardcoded in `autodispatch.py`: the AD reads it from its runtime
configuration (`$AD_JUMP_HOSTS` or `~/phanes-ctl/autodispatch/jump-hosts.json`), the same single
source of truth the dispatcher uses.  This check therefore keeps only executable assertions:

  * a ProxyCommand value containing spaces stays ONE argv item;
  * the `core.sshCommand` written for the harvest fetch round-trips to the very same argv,
    i.e. no unquoted space breakage that would make `ssh` print its usage.

The fleet host list comes from `docs/fleet-topology-expected.md` (the repo's machine-readable
topology), not from a second copy of the jump table.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import sys

ROOT = Path(__file__).resolve().parents[1]
AUTODISPATCH_PATH = ROOT / "tools" / "lane-autodispatch" / "autodispatch.py"
TOPOLOGY_DOC = ROOT / "docs" / "fleet-topology-expected.md"
# A space-bearing ProxyCommand, used to exercise the quoting contract for every sample host
# whether or not the runtime table configures that host.
SAMPLE_PROXY = "ssh -W %h:%p bastion with space"


def load_autodispatch():
    if not AUTODISPATCH_PATH.is_file():
        raise FileNotFoundError(f"autodispatch script not found at {AUTODISPATCH_PATH}")
    spec = importlib.util.spec_from_file_location("autodispatch", AUTODISPATCH_PATH)
    if spec is None or spec.loader is None:
        raise ImportError(f"failed to load module spec from {AUTODISPATCH_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fleet_hosts() -> list[str]:
    """Return the fleet host set documented in docs/fleet-topology-expected.md."""
    text = TOPOLOGY_DOC.read_text(encoding="utf-8")
    match = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
    if not match:
        raise AssertionError(f"{TOPOLOGY_DOC} must contain a machine-readable topology block")
    hosts = json.loads(match.group(1)).get("hosts")
    assert isinstance(hosts, list) and hosts, f"{TOPOLOGY_DOC} topology block has no hosts"
    return hosts


def hardcoded_jump_hosts(source: str, hosts: list[str]) -> set[str]:
    """Return fleet host names that are hardcoded as dict keys in the autodispatch source."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Dict):
            for key in node.keys:
                if isinstance(key, ast.Constant) and key.value in hosts:
                    found.add(key.value)
    return found


def check_host(ad, host: str, jump_hosts: dict) -> None:
    """Assert the quoting contract for one host under the given jump-host table."""
    opts = ad.ssh_opts(host, jump_hosts=jump_hosts)
    command = ad.git_ssh_command(opts)

    # The quoted command must parse back into exactly the same argv (no unquoted space breakage).
    parsed = shlex.split(command)
    assert parsed == ["ssh", *opts], f"{host}: parsed command differs from options: {parsed} != {['ssh', *opts]}"

    proxies = [opt for opt in opts if opt.startswith("ProxyCommand=")]
    assert len(proxies) <= 1, f"{host}: unexpected ProxyCommand count: {proxies}"
    if proxies:
        value = proxies[0][len("ProxyCommand="):]
        assert " " in value, f"{host}: ProxyCommand value must carry spaces to exercise quoting: {value!r}"
        # The whole ProxyCommand option stays one argv item ...
        assert proxies[0] in parsed, f"{host}: ProxyCommand was split: {parsed}"
        # ... and the unquoted variant is the breakage this contract guards against.
        broken = shlex.split("ssh " + " ".join(opts))
        assert broken != parsed, f"{host}: unquoted command unexpectedly matched the parsed options"
        assert "ProxyCommand=ssh" in broken, f"{host}: unquoted command did not break the option: {broken}"

    # The harvest fetch writes the same quoted command into core.sshCommand.
    fetch = ad.git_fetch_command(opts, "/repo", f"{host}:repo", "refs/heads/main", "lane")
    core = [arg for arg in fetch if arg.startswith("core.sshCommand=")]
    assert len(core) == 1, f"{host}: git_fetch_command must carry exactly one core.sshCommand: {fetch}"
    assert shlex.split(core[0].split("=", 1)[1]) == ["ssh", *opts], (
        f"{host}: core.sshCommand has unquoted space damage: {core[0]}"
    )


def check_loader(ad) -> None:
    """The jump table comes from the AD runtime configuration, not from code."""
    env = ad.JUMP_HOSTS_ENV
    old = os.environ.get(env)
    try:
        os.environ[env] = json.dumps({"ib1": SAMPLE_PROXY})
        assert ad.load_jump_hosts() == {"ib1": SAMPLE_PROXY}, "AD_JUMP_HOSTS must be read as a JSON table"
        os.environ[env] = "{not json"
        assert ad.load_jump_hosts() == {}, "an unreadable jump table must fall back to no injection"
    finally:
        if old is None:
            os.environ.pop(env, None)
        else:
            os.environ[env] = old


def main() -> int:
    try:
        ad = load_autodispatch()
    except Exception as exc:
        print(f"FAIL: could not load autodispatch: {exc}", file=sys.stderr)
        return 1

    try:
        hosts = fleet_hosts()
    except Exception as exc:
        print(f"FAIL: could not read the documented fleet topology: {exc}", file=sys.stderr)
        return 1

    source = AUTODISPATCH_PATH.read_text(encoding="utf-8")
    hardcoded = hardcoded_jump_hosts(source, hosts)
    if hardcoded:
        print(
            f"FAIL: autodispatch hardcodes fleet host(s) {sorted(hardcoded)}; the jump topology must come "
            "from the AD runtime configuration (docs/dual-mode-ssh.md)",
            file=sys.stderr,
        )
        return 1

    # The effective jump table is the one the dispatcher uses (runtime config, not a copy).
    table = dict(getattr(ad, "JUMP_HOSTS", {}) or {})
    print(f"OK: fleet hosts from {TOPOLOGY_DOC.name}: {', '.join(hosts)}")
    print(f"OK: jump-host table from AD runtime config: {', '.join(sorted(table)) or '(empty)'}")

    failed = 0
    for host in hosts:
        for label, jump_hosts in (("configured", table), ("sample-proxy", {host: SAMPLE_PROXY})):
            try:
                check_host(ad, host, jump_hosts)
            except AssertionError as exc:
                print(f"FAIL: {host} [{label}]: {exc}", file=sys.stderr)
                failed += 1

    try:
        check_loader(ad)
    except AssertionError as exc:
        print(f"FAIL: jump-host loader: {exc}", file=sys.stderr)
        failed += 1

    if failed:
        print(f"FAILED: {failed} check(s) failed", file=sys.stderr)
        return 1

    print(f"OK: {len(hosts)} fleet host(s) verified: ProxyCommand with spaces stays one argv item, "
          "core.sshCommand has no unquoted space breakage")
    return 0


if __name__ == "__main__":
    sys.exit(main())
