#!/bin/bash
# Mac side. Add one executor run commit as a new, hooked commit on top of an existing PR branch worktree on gocc.
# Usage: addfix.sh <ibx|ssh> <target> <repo> <run-id> <branch> <message-file>
set -uo pipefail
S=/private/tmp/claude-501/-Users-chenchao-Documents-workspace-nibiru-openbot-openbot/62926203-41d2-4936-8582-8fe69de7f53c/scratchpad
tr=$1; tgt=$2; repo=$3; r=$4; BR=$5; MSG=$6
f=$S/af-$r.bundle
if [ "$tr" = ssh ]; then ssh -o ConnectTimeout=10 "$tgt" "cd $repo && git bundle create - openswarm/$r ^b50d5ce925 2>/dev/null" > "$f"
else for n in 1 2 3 4; do instabox ssh "$tgt" -- "cd $repo && git bundle create - openswarm/$r ^b50d5ce925 2>/dev/null" > "$f" 2>/dev/null; [ -s "$f" ] && break; sleep 4; done; fi
[ -s "$f" ] || { echo "no bundle"; exit 3; }
scp -q "$f" gocc:/tmp/af-$r.bundle; scp -q "$MSG" gocc:/tmp/af-msg-$r.txt
W=pub-$(echo "$BR" | tr '/' '-')
ssh -o ConnectTimeout=10 -o ServerAliveInterval=15 gocc "export PATH=\$HOME/.bun/bin:\$HOME/.local/node-v24.14.1/bin:/opt/homebrew/bin:/usr/bin:/bin:\$PATH; cd ~/work/openbot && git fetch -q /tmp/af-$r.bundle openswarm/$r:refs/pub/$r && sha=\$(git rev-parse refs/pub/$r) && cd ~/work/$W && git cherry-pick -n \$sha >/tmp/af-cp.log 2>&1 || { echo CONFLICT; tail -5 /tmp/af-cp.log; exit 5; }; git reset -q TASKS.md 2>/dev/null; git commit -q -F /tmp/af-msg-$r.txt >/tmp/af-commit.log 2>&1; rc=\$?; [ \$rc -eq 0 ] || { echo COMMIT_FAILED; grep -E 'error TS|Type checks failed|ERROR' /tmp/af-commit.log | head -8; exit 4; }; git push -q origin HEAD 2>&1 | tail -1; echo ADDED \$(git rev-parse --short HEAD)" 2>&1 | tail -10
bash $S/pushbr.sh "$BR"
