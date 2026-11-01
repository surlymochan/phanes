#!/usr/bin/env bash
# Offline path check for box control-plane boot docs (Refs #777, #847). No SSH.
# Usage:
#   bash tools/check-ctl-boot.sh [ctl-bin-dir]   # default ctl-bin-dir: $HOME/phanes-ctl/bin
#   bash tools/check-ctl-boot.sh --gate-only     # only the offline PAUSE-BACKEND gate replay
#   bash tools/check-ctl-boot.sh --self-test     # run offline self-test in temporary fixture
set -euo pipefail
gate_only=0
self_test=0
root=""
for a in "$@"; do
  case "$a" in
    --gate-only) gate_only=1 ;;
    --self-test) self_test=1 ;;
    *) root="$a" ;;
  esac
done

test_sim=""
if [[ "$self_test" -eq 1 ]]; then
  test_sim="$(mktemp -d "${TMPDIR:-/tmp}/ctl-boot-selftest.XXXXXX")"
  mkdir -p "$test_sim/bin" "$test_sim/autodispatch"
  for b in start-control-plane ctl-loop lane-autodispatch lane-merge bus-worker-loop health-loop health-check.py ib-refresh; do
    touch "$test_sim/bin/$b"
    chmod +x "$test_sim/bin/$b"
  done
  cat <<'EOF' >"$test_sim/bin/start-control-plane"
#!/bin/bash
start_one ib-refresh-loop   "$HOME/phanes-ctl/bin/ctl-loop"
start_one lane-autodispatch "$HOME/phanes-ctl/bin/lane-autodispatch"
start_one lane-merge        "$HOME/phanes-ctl/bin/lane-merge"
start_one bus-worker-loop   "$HOME/phanes-ctl/bin/bus-worker-loop"
start_one health-loop       "$HOME/phanes-ctl/bin/health-loop"
EOF
  chmod +x "$test_sim/bin/start-control-plane"
  touch "$test_sim/autodispatch/heartbeat"
  touch "$test_sim/autodispatch/merge-heartbeat"
  touch "$test_sim/ctl-loop.heartbeat"
  touch "$test_sim/autodispatch/health.heartbeat"
  root="$test_sim/bin"
fi

cleanup() {
  [[ -n "$test_sim" && -d "$test_sim" ]] && rm -rf "$test_sim"
  [[ -n "${sim:-}" && -d "${sim:-}" ]] && rm -rf "$sim"
}
trap cleanup EXIT

miss=0
if [[ "$gate_only" -eq 0 ]]; then
  root="${root:-$HOME/phanes-ctl/bin}"
  need=(start-control-plane ctl-loop lane-autodispatch lane-merge bus-worker-loop health-loop health-check.py ib-refresh)
  for n in "${need[@]}"; do
    if [[ -x "$root/$n" || -f "$root/$n" ]]; then
      echo "OK $n"
    else
      echo "MISSING $n"; miss=1
    fi
  done

  # start-control-plane 五件套命令字符串:
  # ib-refresh-loop, lane-autodispatch, lane-merge, bus-worker-loop, health-loop
  scp="$root/start-control-plane"
  if [[ -f "$scp" ]]; then
    for cmd_str in ib-refresh-loop lane-autodispatch lane-merge bus-worker-loop health-loop; do
      if grep -qF "$cmd_str" "$scp"; then
        echo "OK start-control-plane loop $cmd_str"
      else
        echo "MISSING start-control-plane loop $cmd_str in $scp"; miss=1
      fi
    done
  fi

  # Base directory for heartbeats and state
  base="${root%/bin}"

  # 四条心跳文件路径:
  # autodispatch/heartbeat, merge-heartbeat, ctl-loop.heartbeat, health.heartbeat
  if [[ -f "$base/autodispatch/heartbeat" ]]; then
    echo "OK heartbeat autodispatch/heartbeat"
  else
    echo "MISSING heartbeat autodispatch/heartbeat ($base/autodispatch/heartbeat)"; miss=1
  fi

  if [[ -f "$base/autodispatch/merge-heartbeat" || -f "$base/merge-heartbeat" ]]; then
    echo "OK heartbeat merge-heartbeat"
  else
    echo "MISSING heartbeat merge-heartbeat ($base/autodispatch/merge-heartbeat)"; miss=1
  fi

  if [[ -f "$base/ctl-loop.heartbeat" || -f "$base/ib-refresh.status" ]]; then
    echo "OK heartbeat ctl-loop.heartbeat"
  else
    echo "MISSING heartbeat ctl-loop.heartbeat ($base/ctl-loop.heartbeat)"; miss=1
  fi

  if [[ -f "$base/autodispatch/health.heartbeat" || -f "$base/health.heartbeat" ]]; then
    echo "OK heartbeat health.heartbeat"
  else
    echo "MISSING heartbeat health.heartbeat ($base/autodispatch/health.heartbeat)"; miss=1
  fi

  # Pause files live next to the autodispatch state: D = ~/phanes-ctl/autodispatch
  pdir="${PAUSE_DIR:-${root%/bin}/autodispatch}"
  if [[ -d "$pdir" ]]; then
    echo "OK pause-dir $pdir"
  else
    echo "MISSING pause-dir $pdir"; miss=1
  fi
  # Naming convention: PAUSE-BACKEND-<pi|agy|codex>
  for f in "$pdir"/PAUSE-BACKEND-*; do
    [[ -e "$f" ]] || continue
    case "${f##*/PAUSE-BACKEND-}" in
      pi|agy|codex) echo "OK pause-name ${f##*/}" ;;
      *) echo "BAD pause-name ${f##*/}"; miss=1 ;;
    esac
  done
  echo "expected: $pdir/PAUSE-BACKEND-<pi|agy|codex>  (body 'auto: ...' => health-loop may clear it)"
fi

# --- PAUSE-BACKEND gate replay (Refs #768) -------------------------------------
# Contract (docs/ctl-boot.md):
#   * a backend is skipped while PAUSE-BACKEND-<backend> exists
#   * health-loop only clears pause files whose body starts with "auto:"
#   * once the file is gone the same backend is dispatchable again (next tick)
backends=(pi agy codex)
sim="$(mktemp -d "${TMPDIR:-/tmp}/pause-gate.XXXXXX")"

skipped() { [[ -f "$sim/PAUSE-BACKEND-$1" ]]; }
auto_body() { [[ -f "$1" && "$(cat "$1")" == auto:* ]]; }
health_loop_tick() {  # clears only `auto:` bodies, holds manual ones
  local f
  for f in "$sim"/PAUSE-BACKEND-*; do
    [[ -e "$f" ]] || continue
    if auto_body "$f"; then
      rm -f "$f"
    else
      echo "HOLD ${f##*/} (body is not auto:)"
    fi
  done
}
want_state() {  # <backend> <0=dispatchable|1=skipped> <label>
  local b="$1" want="$2" label="$3" got=0
  if skipped "$b"; then got=1; fi
  if [[ "$got" -eq "$want" ]]; then
    echo "OK $label: $b $([[ "$got" -eq 1 ]] && echo skipped || echo dispatchable)"
  else
    echo "FAIL $label: $b expected $([[ "$want" -eq 1 ]] && echo skipped || echo dispatchable), got $([[ "$got" -eq 1 ]] && echo skipped || echo dispatchable)"
    miss=1
  fi
}

for b in "${backends[@]}"; do
  case "PAUSE-BACKEND-$b" in
    PAUSE-BACKEND-pi|PAUSE-BACKEND-agy|PAUSE-BACKEND-codex) echo "OK pause-name PAUSE-BACKEND-$b" ;;
    *) echo "BAD pause-name PAUSE-BACKEND-$b"; miss=1 ;;
  esac
done

# 1) no pause file -> every backend is dispatchable
want_state pi 0 "no-pause"; want_state agy 0 "no-pause"; want_state codex 0 "no-pause"

# 2) auto: pause on agy -> agy is skipped, the other backends keep running
printf 'auto: quota 429\n' >"$sim/PAUSE-BACKEND-agy"
want_state agy 1 "auto-pause"; want_state pi 0 "auto-pause"; want_state codex 0 "auto-pause"

# 3) health-loop clears the auto: pause -> the same backend is dispatchable again
health_loop_tick
want_state agy 0 "auto-cleared"

# 4) manual pause (body not auto:) -> never cleared by health-loop, still skipped
: >"$sim/PAUSE-BACKEND-agy"
want_state agy 1 "manual-pause"
health_loop_tick
want_state agy 1 "manual-pause"

# 5) after the operator removes the file -> the same backend is dispatchable again
rm -f "$sim/PAUSE-BACKEND-agy"
want_state agy 0 "removed"

exit "$miss"
