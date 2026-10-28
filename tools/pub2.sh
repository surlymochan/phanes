#!/bin/bash
# Mac side. Assemble one pull request from commits already on gocc (refs/m10/*), in the order given.
# Usage: pub2.sh <branch> <message-file> <pr-title> <pr-body-file> <sha> [<sha> ...]
set -uo pipefail
S=/private/tmp/claude-501/-Users-chenchao-Documents-workspace-nibiru-openbot-openbot/62926203-41d2-4936-8582-8fe69de7f53c/scratchpad
BR=$1; MSG=$2; TITLE=$3; BODY=$4; shift 4
scp -q "$MSG" gocc:/tmp/pub-msg-$$.txt || exit 3
out=$(ssh -o ConnectTimeout=10 -o ServerAliveInterval=15 gocc "bash -s -- $BR /tmp/pub-msg-$$.txt $*" < $S/pub-remote.sh 2>&1)
echo "$out" | tail -25
echo "$out" | grep -q "^PUBLISHED" || exit 2
cd /Users/chenchao/Documents/workspace/nibiru/openbot/openbot || exit 1
git fetch -q gocc "$BR" 2>&1 | tail -1
sha=$(git rev-parse FETCH_HEAD)
git push -q origin "$sha:refs/heads/$BR" 2>&1 | tail -1
gh pr create -R nibiru-labs/openbot --base main --head "$BR" --title "$TITLE" --body-file "$BODY" 2>&1 | tail -1
