#!/usr/bin/env bash
# CI gate of the Swrm component (Refs #1867).
#
# The steps below are the verbatim copy of the Swrm job of
# .github/workflows/phanes.yml: the workflow calls this script, so the two can
# never drift apart. On top of that the script appends the tools/ unittests of
# the directories the change touches.
#
# The contract and the review delivery mapping live in tools/ci-swrm.md.
set -euo pipefail

if ! command -v python >/dev/null 2>&1; then
  python() { command python3 "$@"; }
fi

cd "$(dirname "$0")/.."
root="$PWD"

# Paths touched by the current change set: committed, staged, unstaged and
# untracked ones.
#
# Rename detection is disabled on purpose: git reports only the target path of
# a rename, so a file moved out of tools/lane-autodispatch/ or tools/tests/
# would otherwise slip past the path based test selection below.
detect_changed_paths() {
  local base="${BASE_SHA:-}"
  if [[ -z "$base" ]]; then
    # Local run without an explicit base: use the default branch when there is one.
    for candidate in main master origin/main origin/master; do
      if git rev-parse --verify --quiet "${candidate}^{commit}" >/dev/null 2>&1; then
        base="$candidate"
        break
      fi
    done
    if [[ -z "$base" ]]; then
      base=HEAD
      echo "WARNING: no default branch found; only local changes are selected." >&2
    fi
  elif [[ "$base" =~ ^0+$ ]]; then
    base=HEAD
  elif ! git rev-parse --verify --quiet "${base}^{commit}" >/dev/null 2>&1; then
    echo "Cannot resolve change base: $base" >&2
    return 1
  fi
  git diff --name-only --no-renames "${base}...HEAD"
  git diff --name-only --no-renames HEAD
  git ls-files --others --exclude-standard
}

if [[ "${1:-}" == "--print-changed-paths" ]]; then
  detect_changed_paths
  exit 0
fi

# Keep tests and offline probes away from the operator's control plane.
mkdir -p "$root/.tmp/test-tmp"
# The tests bind AF_UNIX sockets (the lane control socket) below the temporary
# directory, and a socket path is limited to about 108 bytes: a long checkout path
# makes those tests fail before they test anything.  The temporary directory of the
# checkout is used when it is short enough, the system one otherwise.
if [ "${#root}" -gt 40 ]; then
  export TMPDIR="${SWRM_CI_TMPDIR:-/tmp}"
else
  export TMPDIR="$root/.tmp/test-tmp"
fi
test_home="$(mktemp -d "$TMPDIR/ci.XXXXXX")"
while IFS= read -r name; do
  unset "$name"
done < <(compgen -v | grep -E '^(OPENSWARM_|XSWARM_)' || true)
export HOME="$test_home/home"
export OPENSWARM_HOME="$test_home/state"
export OPENSWARM_FLEET_GUARD_CONFIG="$test_home/fleet-guard.disabled.json"
export OPENSWARM_FLEET_CAPACITY_DIR="$test_home/capacity"
export XDG_CONFIG_HOME="$test_home/config"
export XDG_CACHE_HOME="$test_home/cache"
export XDG_DATA_HOME="$test_home/data"
export GH_CONFIG_DIR="$test_home/gh"
mkdir -p "$HOME" "$OPENSWARM_HOME" "$OPENSWARM_FLEET_CAPACITY_DIR" "$XDG_CONFIG_HOME" "$XDG_CACHE_HOME" "$XDG_DATA_HOME" "$GH_CONFIG_DIR"

echo "Swrm gate: only the current Python version is covered ($(python --version))."

python -m pip install "pytest>=7" "pytest-xdist>=3"

# Step A: unit tests (phanes.yml "Unit tests", working directory swrm).
(
  cd swrm
  python -m pytest tests -v -n auto
)

# Step B: offline CLI smoke test (phanes.yml "Offline CLI smoke test").
(
  cd swrm
  export OPENSWARM_STATE_FILE=examples/demo-state.json
  export OPENSWARM_HOME="$(mktemp -d)"
  python bin/swrm status
  python bin/swrm route --type impl --difficulty 3
)

changed_paths="$(detect_changed_paths)"
if grep -q '^tools/lane-autodispatch/' <<<"$changed_paths"; then
  (cd "$root/tools/lane-autodispatch" && python -m unittest discover -s . -p 'test_*.py' -v)
fi
if grep -q '^tools/tests/' <<<"$changed_paths"; then
  (cd "$root" && python -m unittest discover -s tools/tests -p 'test_*.py' -v)
fi
