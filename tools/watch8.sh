#!/bin/bash
# Print one line when a run of the batch finishes; exit when all have finished.
RUNS=${1:-${PHANES_WATCH_RUNS:?Set PHANES_WATCH_RUNS or pass the run list}}
done_list=""
while true; do
  left=0
  for r in $RUNS; do
    IFS=: read -r host tr id lane <<< "$r"
    case " $done_list " in *" $id "*) continue ;; esac
    left=$((left + 1))
    q="f=\$HOME/.claude/swrm/runs/$id/result.json; test -f \$f && python3 -c \"import json;d=json.load(open('\$f'));r=d.get('result') or {};s=r.get('summary','') if isinstance(r,dict) else str(r);v=(d.get('verify') or {}).get('rc');print(d.get('status'),'verify_rc=',v,'commit=',(d.get('commit') or '-')[:10],'|',s[:200].replace(chr(10),' '))\""
    if [ "$tr" = ssh ]; then out=$(ssh -o ConnectTimeout=8 "$host" "$q" 2>/dev/null)
    else out=$(instabox ssh "$host" -- "$q" 2>/dev/null); fi
    if [ -n "$out" ] && ! echo "$out" | grep -q "banner exchange"; then
      echo "[$lane $id] $out"
      done_list="$done_list $id"
    fi
  done
  [ "$left" -eq 0 ] && { echo "ALL-DONE"; exit 0; }
  sleep 120
done
