#!/usr/bin/env bash
# Poll open PRs and keep the merge supervisor visibly alive, including idle ticks (Refs #1111).
set -euo pipefail

HEARTBEAT_FILE="${MERGE_HEARTBEAT:-$HOME/phanes-ctl/autodispatch/merge-heartbeat}"
TICK_SECONDS="${MERGE_TICK_SECONDS:-60}"

write_heartbeat() {
  mkdir -p "$(dirname "$HEARTBEAT_FILE")"
  touch "$HEARTBEAT_FILE"
}

run_tick() {
  # Write before the query so a slow or empty poll cannot look like a dead loop.
  write_heartbeat
  local prs
  prs="$(gh pr list --state open --json number --limit 100)"
  if [[ "$prs" == "[]" ]]; then
    # An idle tick is still healthy; refresh the heartbeat before returning.
    write_heartbeat
    return 0
  fi

  # Merge policy is supplied by the host's gh configuration/branch protections.
  while read -r number; do
    [[ -n "$number" ]] || continue
    gh pr merge "$number" --squash --delete-branch
  done < <(printf '%s' "$prs" | python3 -c 'import json, sys; print("\n".join(str(pr["number"]) for pr in json.load(sys.stdin)))')
  write_heartbeat
}

main() {
  while :; do
    run_tick
    [[ "${MERGE_LOOP_ONCE:-0}" == 1 ]] && return 0
    sleep "$TICK_SECONDS"
  done
}

main "$@"
