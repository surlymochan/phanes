#!/usr/bin/env bash
# Offline path check for box control-plane boot docs. No SSH.
set -euo pipefail
root="${1:-$HOME/phanes-ctl/bin}"
need=(start-control-plane ctl-loop lane-autodispatch lane-merge bus-worker-loop health-loop health-check.py ib-refresh)
miss=0
for n in "${need[@]}"; do
  if [[ -x "$root/$n" || -f "$root/$n" ]]; then
    echo "OK $n"
  else
    echo "MISSING $n"; miss=1
  fi
done
exit "$miss"
