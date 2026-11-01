#!/usr/bin/env bash
# Offline consistency check for the dual-mode SSH and fleet topology docs.
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
doc="$root/docs/dual-mode-ssh.md"
topology_doc="$root/docs/fleet-topology-expected.md"

if [[ ! -f "$doc" ]]; then
  echo "MISSING $doc" >&2
  exit 1
fi

if [[ ! -f "$topology_doc" ]]; then
  echo "MISSING $topology_doc" >&2
  exit 1
fi

# The expected fleet hosts live in the topology document; never keep a second copy here.
hosts=$(python3 - "$topology_doc" <<'PY'
import json
import re
import sys

text = open(sys.argv[1], encoding="utf-8").read()
match = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
if not match:
    raise SystemExit("fleet-topology-expected.md must contain a JSON topology block")
hosts = json.loads(match.group(1))["hosts"]
if not isinstance(hosts, list) or not hosts:
    raise SystemExit("fleet-topology-expected.md topology block has no hosts")
print(" ".join(hosts))
PY
)

for required in phanes_ctl $hosts; do
  if grep -Fq -- "$required" "$doc"; then
    echo "OK $required"
  else
    echo "MISSING $required" >&2
    exit 1
  fi
done

# The retired Instabox hosts must not come back into the live fleet documentation.
if grep -Eq 'ib[1-5]' "$doc" "$topology_doc"; then
  echo "UNEXPECTED retired instabox host in the fleet documentation" >&2
  exit 1
fi

# Copilot may be mentioned as explicitly disabled, but it must not be part of
# the default automatic fleet host/backend configuration.
if grep -Eiq 'OPENSWARM_AUTO_(FLEET_HOSTS|HOST_BACKENDS).*copilot|default.{0,40}copilot|copilot.{0,40}default|默认.{0,40}copilot|copilot.{0,40}默认' "$doc"; then
  echo "UNEXPECTED default copilot dispatch configuration" >&2
  exit 1
fi

echo "OK no default copilot dispatch"
