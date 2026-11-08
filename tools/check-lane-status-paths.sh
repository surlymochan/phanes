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
  echo "$host  ~/.claude/swrm-executor-candidate/lanes/$lane/state.json"
  echo "$host  ~/.claude/swrm-executor-candidate/runs/<run>/status.json|result.json"
  echo "$host  ~/tools/swrm-executor-candidate/current  (release symlink)"
}

cat <<'TXT'
Expected layout (all fleet hosts + box):
  OPENSWARM_HOME-style state:  ~/.claude/swrm-executor-candidate/lanes/<LANE>/state.json
  run status/result:           ~/.claude/swrm-executor-candidate/runs/<RUN>/{status,result}.json
  release binary:              ~/tools/swrm-executor-candidate/current -> releases/<sha>/swrm

Hosts / lanes:
TXT
expect node-1.example.invalid M1; expect node-2.example.invalid M2; expect node-3.example.invalid M3; expect node-4.example.invalid M4
expect node-5.example.invalid CX1; expect node-5.example.invalid CX3
expect worker-b.example.invalid CX2; expect worker-b.example.invalid CX4
expect worker-a.example.invalid CX5
expect relay.example.invalid A2; expect broker A1
expect box GH1; expect box GH2

if [[ "$probe" -eq 1 ]]; then
  echo
  echo "Probing (BatchMode, 12s cap)…"
  for spec in \
    "node-1.example.invalid:M1" "node-2.example.invalid:M2" "node-3.example.invalid:M3" "node-4.example.invalid:M4" "node-5.example.invalid:CX1" "node-5.example.invalid:CX3" \
    "worker-b.example.invalid:CX2" "worker-b.example.invalid:CX4" "worker-a.example.invalid:CX5" "relay.example.invalid:A2" "broker:A1"
  do
    host=${spec%%:*}; lane=${spec##*:}
    if timeout 12 ssh -o BatchMode=yes -o ConnectTimeout=5 "$host" \
      "test -f \$HOME/.claude/swrm-executor-candidate/lanes/$lane/state.json && test -L \$HOME/tools/swrm-executor-candidate/current && echo OK" \
      >/tmp/clsp-$host-$lane.out 2>/tmp/clsp-$host-$lane.err; then
      echo "OK $host $lane $(cat /tmp/clsp-$host-$lane.out)"
    else
      echo "FAIL $host $lane $(tr '\n' ' ' </tmp/clsp-$host-$lane.err | head -c 120)"
    fi
  done
fi
