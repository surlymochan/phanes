#!/bin/bash
# Mac side: push a gocc branch to GitHub as a fast-forward. Usage: pushbr.sh <branch>
cd /Users/chenchao/Documents/workspace/nibiru/openbot/openbot || exit 1
git fetch -q gocc "$1" 2>&1 | tail -1
sha=$(git rev-parse FETCH_HEAD)
git push -q origin "$sha:refs/heads/$1" 2>&1 | tail -1
echo "pushed $1 ${sha:0:10}"
