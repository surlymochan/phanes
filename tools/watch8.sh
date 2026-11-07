#!/bin/bash
# Print one line when a run of the batch finishes; exit when all have finished.
RUNS="gocc:ssh:20261028-193632-661c67:C1 govpn2:ssh:20261028-193635-0604db:K2"
[ -n "${1:-}" ] && RUNS="$1"
done_list=""
while true; do
  left=0
  for r in $RUNS; do
    IFS=: read -r host tr id lane <<< "$r"
    case " $done_list " in *" $id "*) continue ;; esac
    left=$((left + 1))
    q="f=\$HOME/.claude/openswarm/runs/$id/result.json; test -f \$f && python3 -c \"import json;d=json.load(open('\$f'));r=d.get('result') or {};s=r.get('summary','') if isinstance(r,dict) else str(r);v=(d.get('verify') or {}).get('rc');print(d.get('status'),'verify_rc=',v,'commit=',(d.get('commit') or '-')[:10],'|',s[:200].replace(chr(10),' '))\""
    [ "$tr" = ssh ] || { echo "unsupported transport: $tr" >&2; exit 2; }
    out=$(ssh -o ConnectTimeout=8 "$host" "$q" 2>/dev/null)
    if [ -n "$out" ] && ! echo "$out" | grep -q "banner exchange"; then
      echo "[$lane $id] $out"
      done_list="$done_list $id"
    fi
  done
  [ "$left" -eq 0 ] && { echo "ALL-DONE"; exit 0; }
  sleep 120
done
