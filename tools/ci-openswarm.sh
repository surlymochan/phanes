#!/usr/bin/env bash
# CI gate of the OpenSwarm component (Refs #1867).
#
# The steps below are the verbatim copy of the OpenSwarm job of
# .github/workflows/phanes.yml: the workflow calls this script, so the two can
# never drift apart. On top of that the script appends the tools/ unittests of
# the directories the change touches.
#
# The contract and the review delivery mapping live in tools/ci-openswarm.md.
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

echo "OpenSwarm gate: only the current Python version is covered ($(python --version))."

# Step A: unit tests (phanes.yml "Unit tests", working directory openswarm).
(
  cd openswarm
  python -m unittest discover -s tests -v
)

# Step B: offline CLI smoke test (phanes.yml "Offline CLI smoke test").
(
  cd openswarm
  export OPENSWARM_STATE_FILE=examples/demo-state.json
  export OPENSWARM_HOME="$(mktemp -d)"
  python bin/openswarm status
  python bin/openswarm route --type impl --difficulty 3
)

changed_paths="$(detect_changed_paths)"
if grep -q '^tools/lane-autodispatch/' <<<"$changed_paths"; then
  (cd "$root/tools/lane-autodispatch" && python -m unittest discover -s . -p 'test_*.py' -v)
fi
if grep -q '^tools/tests/' <<<"$changed_paths"; then
  (cd "$root" && python -m unittest discover -s tools/tests -p 'test_*.py' -v)
fi
