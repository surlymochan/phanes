#!/usr/bin/env bash
# Compatibility entry point of the Swrm CI gate.
#
# The gate itself lives in tools/ci-swrm.sh since #1894 replaced the vendored
# openswarm with swrm as the single source, but existing automation still calls
# tools/ci-openswarm.sh.  This wrapper keeps that name working and never drifts
# from the real gate: it runs the same script with the same arguments.
#
# Refs #1816
set -euo pipefail
exec bash "$(cd "$(dirname "$0")" && pwd)/ci-swrm.sh" "$@"
