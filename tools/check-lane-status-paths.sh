#!/bin/sh
# Check the lane-status path contract without contacting any host by default.
set -eu

DATA_HOME=${OPENSWARM_HOME:-$HOME/.claude/openswarm-executor-candidate}
RELEASE_ROOT=${OPENSWARM_RELEASE_ROOT:-$HOME/tools/openswarm-executor-candidate}

hosts='ib1 ib2 ib3 ib4 ib5 govpn govpn2 gorelay gocc box'

usage() {
    echo "usage: $0 [--dry-run] [--check-local]" >&2
    exit 2
}

mode=dry-run
for arg in "$@"; do
    case "$arg" in
        --dry-run) mode=dry-run ;;
        --check-local) mode=check-local ;;
        *) usage ;;
    esac
done

echo "lane status path contract"
for host in $hosts; do
    printf '%-7s %s\n' "$host" "$DATA_HOME/lanes/<L>/state.json"
done
echo "release root: $RELEASE_ROOT/releases/<sha>"
echo "current link: $RELEASE_ROOT/current -> releases/<sha>"
echo "entrypoint:   $RELEASE_ROOT/current/bin/openswarm-fleet"

if [ "$mode" = check-local ]; then
    [ -d "$DATA_HOME" ] || { echo "missing data directory: $DATA_HOME" >&2; exit 1; }
    [ -L "$RELEASE_ROOT/current" ] || { echo "missing release symlink: $RELEASE_ROOT/current" >&2; exit 1; }
    [ -x "$RELEASE_ROOT/current/bin/openswarm-fleet" ] || {
        echo "missing executable: $RELEASE_ROOT/current/bin/openswarm-fleet" >&2
        exit 1
    }
    echo "local check: OK"
else
    echo "dry-run: no SSH or filesystem probe performed"
fi
