#!/bin/bash
# Mac side. Usage: pub.sh <ssh|ibx|local> <target> <repo> <run-id> <branch> <message-file> <pr-title> <pr-body-file> [more run-ids...]
# Bundles each run's branch from its host, imports it on the publish host, makes one hooked commit there, pushes it to GitHub
# and opens a pull request. Prints the PR URL.
set -uo pipefail
PUBLISH_HOST=${PHANES_PUBLISH_HOST:?Set PHANES_PUBLISH_HOST}
REMOTE_REPO=${PHANES_REMOTE_REPO:?Set PHANES_REMOTE_REPO}
printf -v remote_repo_q %q "$REMOTE_REPO"
S=${PHANES_SCRATCH_DIR:?Set PHANES_SCRATCH_DIR to the local transfer directory}
tr=$1; tgt=$2; repo=$3; runid=$4; BR=$5; MSG=$6; TITLE=$7; BODY=$8; shift 8
extra=("$@")
remote_env=""
for name in PHANES_REMOTE_REPO PHANES_REMOTE_WORKTREE PHANES_DEPS_SCRIPT PHANES_DEPS_SOURCE PHANES_PATCH_DIR; do
  value=${!name:?Set the required remote deployment variable}
  printf -v assignment '%s=%q' "$name" "$value"
  remote_env="$remote_env $assignment"
done
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
  if [ "$tr" = ssh ]; then ssh -o ConnectTimeout=10 "$tgt" "cd $repo && git bundle create - swrm/$r ^$BASE 2>/dev/null" > "$f"
  else for n in 1 2 3 4; do instabox ssh "$tgt" -- "cd $repo && git bundle create - swrm/$r ^$BASE 2>/dev/null" > "$f" 2>/dev/null; [ -s "$f" ] && break; sleep 4; done; fi
  [ -s "$f" ] || { echo "no bundle for $r"; exit 3; }
  scp -q "$f" "$PUBLISH_HOST":/tmp/pub-$r.bundle || exit 3
  sha=$(ssh -o ConnectTimeout=10 "$PUBLISH_HOST" "cd $remote_repo_q && git fetch -q /tmp/pub-$r.bundle swrm/$r:refs/pub/$r && git rev-parse refs/pub/$r")
  commits+=("$sha")
done
scp -q "$MSG" "$PUBLISH_HOST":/tmp/pub-msg-$$.txt || exit 3
out=$(ssh -o ConnectTimeout=10 -o ServerAliveInterval=15 "$PUBLISH_HOST" "env $remote_env bash -s -- $BR /tmp/pub-msg-$$.txt ${commits[*]}" < "$(dirname "$0")/pub-remote.sh" 2>&1)
echo "$out" | tail -25
echo "$out" | grep -q "^PUBLISHED" || exit 2
cd "${PHANES_LOCAL_REPO:?Set PHANES_LOCAL_REPO}" || exit 1
git fetch -q "$PUBLISH_HOST" "$BR" 2>&1 | tail -1
sha=$(git rev-parse FETCH_HEAD)
git push -q origin "$sha:refs/heads/$BR" 2>&1 | tail -1
gh pr create ${PHANES_PR_REPO:+--repo "$PHANES_PR_REPO"} --base main --head "$BR" --title "$TITLE" --body-file "$BODY" 2>&1 | tail -1
