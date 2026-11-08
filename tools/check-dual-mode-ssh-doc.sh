#!/usr/bin/env bash
# Offline consistency check for the dual-mode SSH and fleet topology docs.
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
doc="$root/docs/dual-mode-ssh.md"

if [[ ! -f "$doc" ]]; then
  echo "MISSING $doc" >&2
  exit 1
fi

# The SSH example must cover every controlled host of the current topology
# (docs/fleet-topology-expected.md); a retired host must not come back.
for required in fleet_operator gores1 gores2 gocc gorelay govpn govpn2; do
  if grep -Fq -- "$required" "$doc"; then
    echo "OK $required"
  else
    echo "MISSING $required" >&2
    exit 1
  fi
done

# A retired Instabox host must not be part of the deployment instructions.
if grep -Eq '\bib[1-5]\b' "$doc"; then
  echo "UNEXPECTED retired host in the dual-mode SSH runbook" >&2
  exit 1
fi

# Copilot may be mentioned as explicitly disabled, but it must not be part of
# the default automatic fleet host/backend configuration.
if grep -Eiq 'OPENSWARM_AUTO_(FLEET_HOSTS|HOST_BACKENDS).*copilot|default.{0,40}copilot|copilot.{0,40}default|默认.{0,40}copilot|copilot.{0,40}默认' "$doc"; then
  echo "UNEXPECTED default copilot dispatch configuration" >&2
  exit 1
fi

echo "OK no default copilot dispatch"
