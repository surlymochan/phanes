#!/bin/bash
# Mac side: push a publish-host branch to GitHub as a fast-forward. Usage: pushbr.sh <branch>
cd "${PHANES_LOCAL_REPO:?Set PHANES_LOCAL_REPO}" || exit 1
git fetch -q "${PHANES_PUBLISH_HOST:?Set PHANES_PUBLISH_HOST}" "$1" 2>&1 | tail -1
sha=$(git rev-parse FETCH_HEAD)
git push -q origin "$sha:refs/heads/$1" 2>&1 | tail -1
echo "pushed $1 ${sha:0:10}"
