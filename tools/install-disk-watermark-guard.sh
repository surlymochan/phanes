#!/usr/bin/env bash
# Deploy the govpn2 disk watermark guard into the control plane (Refs #1666).
#
# The installer does what the documentation of #1666 promises:
#   1. it deploys the guard *and* its probe dependency into the same directory
#      (the guard loads the probe from there, so one of the two without the
#      other is a broken deployment);
#   2. it wires the resident loop into the live `start-control-plane` script,
#      not only into the example of the repository;
#   3. it replaces the guard that is still running: an old process would keep
#      the old code alive, so the "re-install updates the guard" claim only
#      holds when the old process is stopped first;
#   4. it verifies that the resident guard of the new version really runs.
#
# Usage: bash tools/install-disk-watermark-guard.sh [control-plane-dir]
#        (default: $HOME/phanes-ctl)
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"
ctl="${1:-$HOME/phanes-ctl}"
host="${DISK_WATERMARK_HOST:-govpn2}"
bin="$ctl/bin"
pause_dir="$ctl/autodispatch"
brain_log="$ctl/brain.log"
guard_src="$repo_root/tools/disk-watermark-guard.py"
probe_src="$repo_root/tools/disk-usage-probe.py"
guard_dst="$bin/disk-watermark-guard.py"
probe_dst="$bin/disk-usage-probe.py"
start_script="$bin/start-control-plane"

for source in "$guard_src" "$probe_src"; do
  if [[ ! -f "$source" ]]; then
    echo "MISSING source $source" >&2
    exit 1
  fi
done

version="$(sed -n 's/^GUARD_VERSION = \([0-9][0-9]*\).*/\1/p' "$guard_src")"
version="${version:-0}"
install_started="$(python3 -c 'import time; print(time.time())')"

# 1. stop the resident guard that is still running: the loop is restarted below
#    with the new files, so the old process must not survive the install.
stop_running_guard() {
  local pattern="$guard_dst"
  local pids
  pids="$(pgrep -f "$pattern" 2>/dev/null || true)"
  if [[ -z "$pids" ]]; then
    echo "stop: no resident disk watermark guard is running"
    return 0
  fi
  echo "stop: the resident disk watermark guard (pids: $(echo "$pids" | tr '\n' ' '))"
  # shellcheck disable=SC2086
  kill $pids 2>/dev/null || true
  for _ in $(seq 1 50); do
    pids="$(pgrep -f "$pattern" 2>/dev/null || true)"
    [[ -z "$pids" ]] && return 0
    sleep 0.2
  done
  pids="$(pgrep -f "$pattern" 2>/dev/null || true)"
  if [[ -n "$pids" ]]; then
    echo "kill: the resident disk watermark guard did not stop (pids: $(echo "$pids" | tr '\n' ' '))"
    # shellcheck disable=SC2086
    kill -9 $pids 2>/dev/null || true
    sleep 0.5
  fi
}

# 2. deploy the guard and its probe dependency.
deploy_guard() {
  mkdir -p "$bin" "$pause_dir"
  install -m 0755 "$guard_src" "$guard_dst"
  install -m 0755 "$probe_src" "$probe_dst"
  echo "deploy: $guard_dst"
  echo "deploy: $probe_dst"
}

# 3. wire the resident loop into the live start script (idempotent).
wire_start_script() {
  local line="start_one disk-watermark \"$guard_dst --loop --pause-dir $pause_dir --brain-log $brain_log --host $host\""
  if [[ ! -f "$start_script" ]]; then
    install -m 0755 "$repo_root/tools/lane-autodispatch/start-control-plane.example" "$start_script"
    echo "deploy: $start_script (from the example of the repository)"
  fi
  python3 - "$start_script" "$line" <<'PY'
import sys

path, line = sys.argv[1], sys.argv[2]
with open(path) as stream:
    rows = stream.read().splitlines()
# An older install line is replaced, never duplicated.
kept = [row for row in rows if "disk-watermark-guard" not in row]
if line not in kept:
    kept.extend(["", "# disk watermark guard (Refs #1666): the resident loop of the lane host disk guard.",
                 line])
with open(path, "w") as stream:
    stream.write("\n".join(kept) + "\n")
PY
  echo "wire: the disk watermark loop is started by $start_script"
}

# 4. start the resident loop.
start_resident_guard() {
  "$start_script"
}

# 5. verify that the resident guard of the new version is really running.
state_is_current() {
  python3 - "$1" "$2" "$3" <<'PY'
import json
import sys

path, version, started = sys.argv[1], int(sys.argv[2]), float(sys.argv[3])
try:
    with open(path) as stream:
        record = json.load(stream)
except (OSError, ValueError):
    sys.exit(1)
if not isinstance(record, dict) or record.get("guard") != version:
    sys.exit(1)
updated = record.get("updated_at")
if isinstance(updated, bool) or not isinstance(updated, (int, float)):
    sys.exit(1)
if updated < started:
    sys.exit(1)
sys.exit(0)
PY
}

verify_resident_guard() {
  local pattern="$guard_dst"
  local state_file="$pause_dir/disk-watermark-$host.json"
  local pids
  for _ in $(seq 1 100); do
    pids="$(pgrep -f "$pattern" 2>/dev/null || true)"
    if [[ -n "$pids" ]] && state_is_current "$state_file" "$version" "$install_started"; then
      echo "OK: the resident disk watermark guard (version $version) runs (pids: $(echo "$pids" | tr '\n' ' '))"
      echo "OK: the state of $host is $state_file"
      return 0
    fi
    sleep 0.2
  done
  echo "FAIL: the resident disk watermark guard did not report its new version" >&2
  return 1
}

stop_running_guard
deploy_guard
wire_start_script
start_resident_guard
verify_resident_guard

echo "OK: the disk watermark guard of $host is deployed, wired and running"
