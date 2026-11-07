#!/bin/bash
# Assign one card to one remote lane. Usage: dispatch.sh <ssh|ibx> <host-or-box> <lane> <card> <title> [verify]
C=${PHANES_CARDS_DIR:?Set PHANES_CARDS_DIR to the task-card directory}
tr=$1; tgt=$2; lane=$3; card=$4; title=$5; ver=${6:-}
B=${7:-b50d5ce925}
vflag=""
if [ -n "$ver" ]; then vflag="--verify '$ver'"; fi
remote="cat > /tmp/card-$lane.md && export PATH=\$HOME/.local/bin:\$HOME/bin:\$PATH && swrm lane assign $lane --base $B --task-file /tmp/card-$lane.md --title '$title' $vflag --budget-minutes 45"
for t in 1 2 3 4; do
  if [ "$tr" = ssh ]; then
    out=$(ssh -o ConnectTimeout=8 "$tgt" "$remote" < "$C/$card" 2>&1)
  else
    out=$(instabox ssh "$tgt" -- "$remote" < "$C/$card" 2>&1)
  fi
  if ! echo "$out" | grep -q "banner exchange"; then break; fi
  sleep 4
done
id=$(echo "$out" | grep -oE '"id": "[^"]+"' | head -1)
err=$(echo "$out" | grep -iE 'error|refus' | head -1)
echo "$lane $id $err"
