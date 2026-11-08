#!/usr/bin/env bash
# The CI gate of the repository, under its old name (Refs #1696).
#
# #1894 (swrm became the single source) renamed the gate: the script that used
# to live here is now tools/ci-swrm.sh.  The wrapper keeps the old entry point
# working, so a job that still calls tools/ci-openswarm.sh runs the same gate
# instead of failing with "no such file".
#
# The contract and the review delivery mapping live in tools/ci-swrm.md.
set -euo pipefail

cd "$(dirname "$0")/.."
exec bash tools/ci-swrm.sh "$@"
