#!/usr/bin/env bash
# Poll open PRs and keep the merge supervisor visibly alive, including idle ticks (Refs #1184).
set -euo pipefail

HEARTBEAT_FILE="${MERGE_HEARTBEAT:-$HOME/phanes-ctl/autodispatch/merge-heartbeat}"
TICK_SECONDS="${MERGE_TICK_SECONDS:-60}"
# JSON control plane: enabled, shadow_samples, tester_accuracy, lane_accuracy,
# boss_confirmed; verdicts maps PR numbers to {verdict, head_sha}.
VERDICT_CONFIG="${MERGE_VERDICT_CONFIG:-$HOME/phanes-ctl/autodispatch/merge-verdict.json}"

tester_enabled() {
  python3 -c '
import json, math, sys
try:
    c = json.load(sys.stdin)
    samples = c.get("shadow_samples")
    tester, lane = c.get("tester_accuracy"), c.get("lane_accuracy")
    valid_accuracy = lambda x: type(x) in (int, float) and math.isfinite(x) and 0 <= x <= 1
    enabled = (c.get("enabled") is True and c.get("boss_confirmed") is True
               and type(samples) is int and samples >= 30
               and valid_accuracy(tester) and valid_accuracy(lane) and tester >= lane)
    sys.exit(0 if enabled else 1)
except (ValueError, TypeError, AttributeError, OverflowError):
    sys.exit(1)
' <<< "$1"
}

tester_pass_head() {
  python3 -c '
import json, sys
try:
    verdict = json.load(sys.stdin)["verdicts"][sys.argv[1]]
    head = verdict["head_sha"]
    if verdict["verdict"] == "PASS" and isinstance(head, str) and head:
        print(head)
        sys.exit(0)
except (ValueError, TypeError, KeyError):
    pass
sys.exit(1)
' "$2" <<< "$1"
}

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

  # Take one control snapshot per tick; invalid or absent control keeps the
  # existing review/branch-protection path. Activated control fails closed.
  local control='' tester_active=0 expected_head current_head
  control="$(cat "$VERDICT_CONFIG" 2>/dev/null)" || control=''
  if tester_enabled "$control"; then
    tester_active=1
  fi
  while read -r number; do
    [[ -n "$number" ]] || continue
    if [[ "$tester_active" == 1 ]]; then
      expected_head="$(tester_pass_head "$control" "$number")" || continue
      current_head="$(gh pr view "$number" --json headRefOid --jq .headRefOid)" || continue
      [[ "$current_head" == "$expected_head" ]] || continue
      gh pr merge "$number" --squash --delete-branch --match-head-commit "$expected_head"
    else
      gh pr merge "$number" --squash --delete-branch
    fi
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
