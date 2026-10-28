#!/bin/bash
# Runs on gocc. Builds one commit on top of main from one or more run commits, with the normal pre-commit hook.
# Usage: pub-remote.sh <branch> <message-file> <commit> [<commit> ...]
# The commits must already be in ~/work/openbot (fetched from a bundle). Their combined change is applied
# with cherry-pick -n in order, so the result is a single commit. Prints PUBLISHED <sha> on success.
set -uo pipefail
BR=$1; MSG=$2; shift 2
export PATH=$HOME/.bun/bin:$HOME/.local/node-v24.14.1/bin:/opt/homebrew/bin:/usr/bin:/bin:$PATH
BASE=${BASE:-fd5719fc7f}
W=$HOME/work/pub-$(echo "$BR" | tr '/' '-')
cd ~/work/openbot || exit 9
if [ -e "$W" ]; then echo "worktree exists: $W" >&2; exit 8; fi
git worktree add -q -b "$BR" "$W" "$BASE" || exit 7
cd "$W"
bash ~/.claude/openswarm/link-workspace-deps.sh ~/work/pr41-successor >/dev/null || exit 6
for c in "$@"; do
  git cherry-pick -n "$c" >/tmp/pub-cp.log 2>&1 || { echo "CONFLICT on $c"; cat /tmp/pub-cp.log | tail -5; git status --short | head -10; exit 5; }
done
# The GitHub token has no workflow scope, so a branch that changes a workflow file cannot be pushed. Keep that
# part as a patch and leave the workflow files as they are on main.
if [ -n "$(git diff --cached --name-only -- .github/workflows)" ]; then
  git diff --cached -- .github/workflows > "$HOME/work/ci-wiring-$(echo "$BR" | tr '/' '-').patch"
  git restore --source=HEAD --staged --worktree -- .github/workflows
  echo "CI wiring withheld: $HOME/work/ci-wiring-$(echo "$BR" | tr '/' '-').patch" >&2
fi
# the executor's notes file and local build output never belong in a commit
git reset -q TASKS.md 2>/dev/null || true
git status --short | grep -E "^\?\?" | head -3 >&2 || true
git commit -q -F "$MSG" >/tmp/pub-commit.log 2>&1
rc=$?
if [ $rc -ne 0 ]; then echo "COMMIT_FAILED rc=$rc"; grep -vE "^ (create|delete|rename|mode) |lint/|^\s*[0-9]+ │|^\s*$|^\s+[│>i]" /tmp/pub-commit.log | tail -25; exit 4; fi
git push -q origin "$BR" 2>&1 | tail -1
echo "PUBLISHED $(git rev-parse HEAD) $(git diff --shortstat HEAD~1 HEAD)"
