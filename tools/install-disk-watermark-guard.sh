#!/usr/bin/env bash
# Install/update the probe, guard, and resident-loop startup in the live ctl tree.
set -euo pipefail
repo="$(cd "$(dirname "$0")/.." && pwd)"
ctl="${1:-$HOME/phanes-ctl}"
mkdir -p "$ctl/bin" "$ctl/autodispatch"
install -m 0755 "$repo/tools/disk-usage-probe.py" "$ctl/bin/disk-usage-probe.py"
install -m 0755 "$repo/tools/disk-watermark-guard.py" "$ctl/bin/disk-watermark-guard.py"
start="$ctl/bin/start-control-plane"
if [[ ! -f "$start" ]]; then
  echo "missing live start-control-plane: $start" >&2
  exit 1
fi
if ! grep -q 'disk-watermark-loop' "$start"; then
  sed -i '/^echo "status:"/i start_one disk-watermark-loop "python3 $HOME/phanes-ctl/bin/disk-watermark-guard.py --loop --pause-dir $HOME/phanes-ctl/autodispatch --brain-log $HOME/phanes-ctl/brain.log"' "$start"
fi
echo "installed disk watermark guard; restarting control-plane entrypoint to start loop"
"$start"
pgrep -af 'disk-watermark-guard.py --loop' >/dev/null
echo "OK: resident disk-watermark guard is running"
