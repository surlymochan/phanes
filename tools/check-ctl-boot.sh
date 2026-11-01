#!/usr/bin/env bash
# Offline path check for box control-plane boot docs (Refs #777). No SSH.
# Usage:
#   bash tools/check-ctl-boot.sh [ctl-bin-dir]   # default ctl-bin-dir: $HOME/phanes-ctl/bin
#   bash tools/check-ctl-boot.sh --gate-only     # only the offline PAUSE-BACKEND gate replay
set -euo pipefail
gate_only=0
root=""
for a in "$@"; do
  case "$a" in
    --gate-only) gate_only=1 ;;
    *) root="$a" ;;
  esac
done

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
trap 'rm -rf "$sim"' EXIT

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
