#!/usr/bin/env bash
# Offline consistency check for the dual-mode SSH and fleet topology docs.
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
doc="$root/docs/dual-mode-ssh.md"

if [[ ! -f "$doc" ]]; then
  echo "MISSING $doc" >&2
  exit 1
fi

for required in phanes_ctl ib1 govpn2 gocc; do
  if grep -Fq -- "$required" "$doc"; then
    echo "OK $required"
  else
    echo "MISSING $required" >&2
    exit 1
  fi
done

# Copilot may be mentioned as explicitly disabled, but it must not be part of
# the default automatic fleet host/backend configuration.
if grep -Eiq 'OPENSWARM_AUTO_(FLEET_HOSTS|HOST_BACKENDS).*copilot|default.{0,40}copilot|copilot.{0,40}default|默认.{0,40}copilot|copilot.{0,40}默认' "$doc"; then
  echo "UNEXPECTED default copilot dispatch configuration" >&2
  exit 1
fi

echo "OK no default copilot dispatch"
