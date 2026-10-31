#!/usr/bin/env bash
# Invoke only the verified rollout stages that were deferred for the TTL probe.
set -euo pipefail

template="$(dirname "$0")/deploy-hub-fleet.sh"
library=$(mktemp /tmp/phanes-deploy-library.XXXXXX)
trap 'rm -f "$library"' EXIT

# Load the original preflight, backup, rollback and readiness functions, but
# omit its final all-host invocation.  Since this wrapper lives beside the
# template, its source-root check still addresses the clean deployment tree.
# The first local-only block is preflight; the second is the final all-host
# dispatch. Retain the former and cut only at the latter.
awk '/^if \(\( ! local_only \)\); then$/ { seen++; if (seen == 2) exit } { print }' "$template" > "$library"
source "$library" "$@"

remote_apply gores1 "$gores1_runtime" "$gores1_repo" 'K1,M1,M2,M3,M4'
local_apply
install_local_runtime
