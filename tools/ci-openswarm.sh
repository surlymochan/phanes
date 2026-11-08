#!/usr/bin/env bash
# Compatibility alias for the renamed CI gate (Refs #1480).
#
# #1894 renamed the vendored `openswarm/` tree to `swrm/`, and with it the gate script
# `tools/ci-openswarm.sh` -> `tools/ci-swrm.sh` (.github/workflows/phanes.yml calls the
# new name). This alias keeps the old entry point working, so a host that still calls
# `tools/ci-openswarm.sh` runs exactly the same gate.
set -euo pipefail
cd "$(dirname "$0")"
exec bash ci-swrm.sh "$@"
