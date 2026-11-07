#!/bin/bash
# Mac side. Add one executor run commit as a new, hooked commit on top of an existing PR branch worktree on the publish host.
# Usage: addfix.sh <ibx|ssh> <target> <repo> <run-id> <branch> <message-file>
set -uo pipefail
PUBLISH_HOST=${PHANES_PUBLISH_HOST:?Set PHANES_PUBLISH_HOST}
REMOTE_REPO=${PHANES_REMOTE_REPO:?Set PHANES_REMOTE_REPO}
printf -v remote_repo_q %q "$REMOTE_REPO"
S=${PHANES_SCRATCH_DIR:?Set PHANES_SCRATCH_DIR to the local transfer directory}
tr=$1; tgt=$2; repo=$3; r=$4; BR=$5; MSG=$6
f=$S/af-$r.bundle
if [ "$tr" = ssh ]; then ssh -o ConnectTimeout=10 "$tgt" "cd $repo && git bundle create - swrm/$r ^b50d5ce925 2>/dev/null" > "$f"
else for n in 1 2 3 4; do instabox ssh "$tgt" -- "cd $repo && git bundle create - swrm/$r ^b50d5ce925 2>/dev/null" > "$f" 2>/dev/null; [ -s "$f" ] && break; sleep 4; done; fi
[ -s "$f" ] || { echo "no bundle"; exit 3; }
scp -q "$f" "$PUBLISH_HOST":/tmp/af-$r.bundle; scp -q "$MSG" "$PUBLISH_HOST":/tmp/af-msg-$r.txt
W=${PHANES_REMOTE_WORKTREE:?Set PHANES_REMOTE_WORKTREE}
printf -v remote_worktree_q %q "$W"
ssh -o ConnectTimeout=10 -o ServerAliveInterval=15 "$PUBLISH_HOST" "export PATH=\$HOME/.bun/bin:/opt/homebrew/bin:/usr/bin:/bin:\$PATH; cd $remote_repo_q && git fetch -q /tmp/af-$r.bundle swrm/$r:refs/pub/$r && sha=\$(git rev-parse refs/pub/$r) && cd $remote_worktree_q && git cherry-pick -n \$sha >/tmp/af-cp.log 2>&1 || { echo CONFLICT; tail -5 /tmp/af-cp.log; exit 5; }; git reset -q TASKS.md 2>/dev/null; git commit -q -F /tmp/af-msg-$r.txt >/tmp/af-commit.log 2>&1; rc=\$?; [ \$rc -eq 0 ] || { echo COMMIT_FAILED; grep -E 'error TS|Type checks failed|ERROR' /tmp/af-commit.log | head -8; exit 4; }; git push -q origin HEAD 2>&1 | tail -1; echo ADDED \$(git rev-parse --short HEAD)" 2>&1 | tail -10
bash "${PHANES_PUSH_SCRIPT:?Set PHANES_PUSH_SCRIPT}" "$BR"
