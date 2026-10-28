#!/bin/bash
# Print one line when a run of the batch finishes; exit when all have finished.
RUNS="gocc:ssh:20261028-193632-661c67:C1 govpn2:ssh:20261028-193635-0604db:K2 dot-task-02:ibx:20261028-113638-eec71d:K4 dot-task-05:ibx:20261028-113644-fddbed:K3 dot-task-01:ibx:20261028-113650-f2f582:B1a dot-task-02:ibx:20261028-113734-3022f3:B2a dot-task-03:ibx:20261028-113819-e5a907:B3a dot-task-04:ibx:20261028-113905-6f9831:B4a"
[ -n "${1:-}" ] && RUNS="$1"
done_list=""
while true; do
  left=0
  for r in $RUNS; do
    IFS=: read -r host tr id lane <<< "$r"
    case " $done_list " in *" $id "*) continue ;; esac
    left=$((left + 1))
    q="f=\$HOME/.claude/openswarm/runs/$id/result.json; test -f \$f && python3 -c \"import json;d=json.load(open('\$f'));r=d.get('result') or {};s=r.get('summary','') if isinstance(r,dict) else str(r);v=(d.get('verify') or {}).get('rc');print(d.get('status'),'verify_rc=',v,'commit=',(d.get('commit') or '-')[:10],'|',s[:200].replace(chr(10),' '))\""
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
