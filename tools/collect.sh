#!/bin/bash
# Run on an executor host (REPO env var = its openbot repo). Writes a git bundle of every openswarm/* branch
# committed on or after SINCE (default: the M10 batch start) to stdout; prints the branch list to stderr.
REPO=${REPO:?}
SINCE=${SINCE:-2026-10-28T11:00:00Z}
BASE=${BASE:-b50d5ce925}
cd "$REPO" || exit 9
refs=""
while read -r ref date; do
  if [[ "$date" > "$SINCE" ]]; then
    refs="$refs $ref"
    echo "$ref $(git rev-parse --short "$ref") $date" >&2
  fi
done < <(git for-each-ref --format='%(refname) %(committerdate:iso-strict)' --sort=committerdate refs/heads/openswarm/ | python3 -c "
import sys,datetime
for l in sys.stdin:
    ref,d=l.split()
    t=datetime.datetime.fromisoformat(d.replace('Z','+00:00')).astimezone(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    print(ref,t)")
[ -n "$refs" ] || { echo "no branches" >&2; exit 0; }
git bundle create - $refs ^$BASE 2>/dev/null
