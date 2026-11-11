#!/usr/bin/env bash
# Compatibility entry point for the gate's old name (Refs #1690, #1714).
#
# The swrm merge (#1894) renamed the OpenSwarm gate to tools/ci-swrm.sh, but the
# documented acceptance command of the lane cards still calls
# `bash tools/ci-openswarm.sh`.  This wrapper forwards to the renamed gate
# instead of failing with "no such file".
set -euo pipefail
cd "$(dirname "$0")/.."
exec bash tools/ci-swrm.sh "$@"
