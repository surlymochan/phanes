#!/usr/bin/env python3
"""Offline contract check for the ib-refresh certificate reuse threshold.

Deprecated (Refs #1480): the Instabox nodes are offline since 2026-11-02, so this
check only keeps the historical instabox certificate-refresh contract alive.  It no
longer describes the live fleet topology (see docs/fleet-topology-expected.md).
"""

import os
import pathlib
import importlib.util


ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "tools" / "ib-refresh.py"
SPEC = importlib.util.spec_from_file_location("ib_refresh", MODULE_PATH)
IB_REFRESH = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(IB_REFRESH)
cert_reuse_ttl = IB_REFRESH.cert_reuse_ttl
need_renew = IB_REFRESH.need_renew


def main():
    original = os.environ.get("IB_CERT_REUSE_TTL")
    try:
        os.environ.pop("IB_CERT_REUSE_TTL", None)
        assert cert_reuse_ttl() == 1500
        assert need_renew(1499)
        assert not need_renew(1500)

        os.environ["IB_CERT_REUSE_TTL"] = "3000"
        assert cert_reuse_ttl() == 3000
        assert need_renew(2999)
        assert not need_renew(3000)
    finally:
        if original is None:
            os.environ.pop("IB_CERT_REUSE_TTL", None)
        else:
            os.environ["IB_CERT_REUSE_TTL"] = original
    print("OK IB_CERT_REUSE_TTL need_renew contract")
    print("deprecated: instabox nodes are offline; this contract is history only (Refs #1480)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
