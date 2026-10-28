#!/bin/bash
# Mac side. Usage: pub.sh <ssh|ibx|local> <target> <repo> <run-id> <branch> <message-file> <pr-title> <pr-body-file> [more run-ids...]
# Bundles each run's branch from its host, imports it on gocc, makes one hooked commit there, pushes it to GitHub
# and opens a pull request. Prints the PR URL.
set -uo pipefail
S=/private/tmp/claude-501/-Users-chenchao-Documents-workspace-nibiru-openbot-openbot/62926203-41d2-4936-8582-8fe69de7f53c/scratchpad
tr=$1; tgt=$2; repo=$3; runid=$4; BR=$5; MSG=$6; TITLE=$7; BODY=$8; shift 8
extra=("$@")
BASE=${BASE:-fd5719fc7f}
rx(){ # run a command on the source host
  local n out
  for n in 1 2 3 4; do
    if [ "$tr" = ssh ]; then out=$(ssh -o ConnectTimeout=10 "$tgt" "$@" 2>&1; echo "rc=$?")
    else out=$(instabox ssh "$tgt" -- "$@" 2>&1; echo "rc=$?"); fi
    echo "$out" | grep -q "banner exchange\|Connection to" && { sleep 4; continue; }
    break
  done
  printf '%s\n' "$out"
}
commits=()
for r in "$runid" ${extra[@]+"${extra[@]}"}; do
  [ -n "$r" ] || continue
  f=$S/pub-$r.bundle
  if [ "$tr" = ssh ]; then ssh -o ConnectTimeout=10 "$tgt" "cd $repo && git bundle create - openswarm/$r ^$BASE 2>/dev/null" > "$f"
  else for n in 1 2 3 4; do instabox ssh "$tgt" -- "cd $repo && git bundle create - openswarm/$r ^$BASE 2>/dev/null" > "$f" 2>/dev/null; [ -s "$f" ] && break; sleep 4; done; fi
  [ -s "$f" ] || { echo "no bundle for $r"; exit 3; }
  scp -q "$f" gocc:/tmp/pub-$r.bundle || exit 3
  sha=$(ssh -o ConnectTimeout=10 gocc "cd ~/work/openbot && git fetch -q /tmp/pub-$r.bundle openswarm/$r:refs/pub/$r && git rev-parse refs/pub/$r")
  commits+=("$sha")
done
scp -q "$MSG" gocc:/tmp/pub-msg-$$.txt || exit 3
out=$(ssh -o ConnectTimeout=10 -o ServerAliveInterval=15 gocc "bash -s -- $BR /tmp/pub-msg-$$.txt ${commits[*]}" < $S/pub-remote.sh 2>&1)
echo "$out" | tail -25
echo "$out" | grep -q "^PUBLISHED" || exit 2
cd /Users/chenchao/Documents/workspace/nibiru/openbot/openbot || exit 1
git fetch -q gocc "$BR" 2>&1 | tail -1
sha=$(git rev-parse FETCH_HEAD)
git push -q origin "$sha:refs/heads/$BR" 2>&1 | tail -1
gh pr create -R nibiru-labs/openbot --base main --head "$BR" --title "$TITLE" --body-file "$BODY" 2>&1 | tail -1
