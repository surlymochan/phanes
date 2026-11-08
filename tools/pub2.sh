#!/bin/bash
# Mac side. Assemble one pull request from commits already on "$PUBLISH_HOST" (refs/m10/*), in the order given.
# Usage: pub2.sh <branch> <message-file> <pr-title> <pr-body-file> <sha> [<sha> ...]
set -uo pipefail
PUBLISH_HOST=${PHANES_PUBLISH_HOST:?Set PHANES_PUBLISH_HOST}
remote_env=""
for name in PHANES_REMOTE_REPO PHANES_REMOTE_WORKTREE PHANES_DEPS_SCRIPT PHANES_DEPS_SOURCE PHANES_PATCH_DIR; do
  value=${!name:?Set the required remote deployment variable}
  printf -v assignment '%s=%q' "$name" "$value"
  remote_env="$remote_env $assignment"
done
BR=$1; MSG=$2; TITLE=$3; BODY=$4; shift 4
scp -q "$MSG" "$PUBLISH_HOST":/tmp/pub-msg-$$.txt || exit 3
out=$(ssh -o ConnectTimeout=10 -o ServerAliveInterval=15 "$PUBLISH_HOST" "env $remote_env bash -s -- $BR /tmp/pub-msg-$$.txt $*" < "$(dirname "$0")/pub-remote.sh" 2>&1)
echo "$out" | tail -25
echo "$out" | grep -q "^PUBLISHED" || exit 2
cd "${PHANES_LOCAL_REPO:?Set PHANES_LOCAL_REPO}" || exit 1
git fetch -q "$PUBLISH_HOST" "$BR" 2>&1 | tail -1
sha=$(git rev-parse FETCH_HEAD)
git push -q origin "$sha:refs/heads/$BR" 2>&1 | tail -1
gh pr create ${PHANES_PR_REPO:+--repo "$PHANES_PR_REPO"} --base main --head "$BR" --title "$TITLE" --body-file "$BODY" 2>&1 | tail -1
