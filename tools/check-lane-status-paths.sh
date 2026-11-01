#!/usr/bin/env bash
# Document + optionally probe lane status.json / state.json paths used by autodispatch.
# Usage:
#   bash tools/check-lane-status-paths.sh           # print expected paths only
#   bash tools/check-lane-status-paths.sh --probe   # SSH probe (box must have aliases)
set -euo pipefail
probe=0
[[ "${1:-}" == "--probe" ]] && probe=1

expect() {
  local host="$1" lane="$2"
  echo "$host  ~/.claude/openswarm-executor-candidate/lanes/$lane/state.json"
  echo "$host  ~/.claude/openswarm-executor-candidate/runs/<run>/status.json|result.json"
  echo "$host  ~/tools/openswarm-executor-candidate/current  (release symlink)"
}

cat <<'TXT'
Expected layout (all fleet hosts + box):
  OPENSWARM_HOME-style state:  ~/.claude/openswarm-executor-candidate/lanes/<LANE>/state.json
  run status/result:           ~/.claude/openswarm-executor-candidate/runs/<RUN>/{status,result}.json
  release binary:              ~/tools/openswarm-executor-candidate/current -> releases/<sha>/openswarm

Hosts / lanes:
TXT
expect ib1 M1; expect ib2 M2; expect ib3 M3; expect ib4 M4
expect ib5 CX1; expect ib5 CX3
expect govpn2 CX2; expect govpn2 CX4
expect govpn CX5
expect gorelay A2; expect gocc A1
expect box GH1; expect box GH2

if [[ "$probe" -eq 1 ]]; then
  echo
  echo "Probing (BatchMode, 12s cap)…"
  for spec in \
    "ib1:M1" "ib2:M2" "ib3:M3" "ib4:M4" "ib5:CX1" "ib5:CX3" \
    "govpn2:CX2" "govpn2:CX4" "govpn:CX5" "gorelay:A2" "gocc:A1"
  do
    host=${spec%%:*}; lane=${spec##*:}
    if timeout 12 ssh -o BatchMode=yes -o ConnectTimeout=5 "$host" \
      "test -f \$HOME/.claude/openswarm-executor-candidate/lanes/$lane/state.json && test -L \$HOME/tools/openswarm-executor-candidate/current && echo OK" \
      >/tmp/clsp-$host-$lane.out 2>/tmp/clsp-$host-$lane.err; then
      echo "OK $host $lane $(cat /tmp/clsp-$host-$lane.out)"
    else
      echo "FAIL $host $lane $(tr '\n' ' ' </tmp/clsp-$host-$lane.err | head -c 120)"
    fi
  done
fi
