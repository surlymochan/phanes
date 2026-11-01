#!/usr/bin/env python3
"""Offline regression check for SSH options containing spaces (Refs #972)."""
import importlib.util
import pathlib
import shlex


ROOT = pathlib.Path(__file__).parents[1]
SOURCE = ROOT / "tools" / "lane-autodispatch" / "autodispatch.py"
spec = importlib.util.spec_from_file_location("autodispatch", SOURCE)
autodispatch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(autodispatch)


def main():
    options = [
        "-o", "ConnectTimeout=20",
        "-o", "ProxyCommand=ssh -W %h:%p bastion with space",
        "-o", "ProxyJump=jump host",
    ]
    expected = ["ssh", *options]

    # Status probes and Git harvest must start from the same quoted contract.
    assert shlex.split(autodispatch.git_ssh_command(options)) == expected
    assert autodispatch.ssh_argv(options) == expected
    fetch = autodispatch.git_fetch_command(options, "/repo", "host:repo", "main", "lane")
    assert shlex.split(fetch[2].split("=", 1)[1]) == expected

    print("ssh option quoting contract: OK")


if __name__ == "__main__":
    main()
