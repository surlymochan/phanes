#!/bin/bash
# Fast-forward a host's openswarm checkout to the bundle on stdin, then restart its idle lanes.
# Usage: osupdate.sh <ssh|ibx> <host>
B=/private/tmp/claude-501/-Users-chenchao-Documents-workspace-nibiru-openbot-openbot/62926203-41d2-4936-8582-8fe69de7f53c/scratchpad/os.bundle
remote='D=$(dirname $(dirname $(readlink -f $(command -v openswarm 2>/dev/null || echo $HOME/.local/bin/openswarm)))); cat > /tmp/os.bundle && cd $D && git fetch -q /tmp/os.bundle main:refs/heads/os-incoming && git merge -q --ff-only os-incoming && echo "$D $(git log --oneline -1)"; export PATH=$HOME/.local/bin:$PATH; for l in $(openswarm lane status 2>/dev/null | python3 -c "import json,sys;print(\" \".join(x[\"name\"] for x in json.load(sys.stdin) if x[\"state\"]==\"idle\"))"); do openswarm lane down $l >/dev/null 2>&1; (nohup openswarm lane up $l >/tmp/lane-up-$l.log 2>&1 &); echo restarted $l; done'
for t in 1 2 3 4; do
  if [ "$1" = ssh ]; then out=$(ssh -o ConnectTimeout=8 "$2" "export PATH=\$HOME/.local/bin:\$PATH; $remote" < "$B" 2>&1)
  else out=$(instabox ssh "$2" -- "export PATH=\$HOME/.local/bin:\$PATH; $remote" < "$B" 2>&1); fi
  echo "$out" | grep -q "banner exchange" || break
  sleep 4
done
echo "== $2"; echo "$out" | tail -4
