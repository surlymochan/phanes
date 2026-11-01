#!/usr/bin/env bash
# Offline path existence check for box control-plane boot docs (no SSH).
# Usage:
#   bash docs/check-ctl-boot.sh [ctl-dir]        # check ctl-dir (default: $HOME/phanes-ctl)
#   bash docs/check-ctl-boot.sh --self-test      # run self-test in temporary fixture
#   bash docs/check-ctl-boot.sh --doc-only       # check docs/ctl-boot.md content alignment
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"
doc_file="$repo_root/docs/ctl-boot.md"

check_doc() {
  if [[ ! -f "$doc_file" ]]; then
    echo "MISSING doc $doc_file" >&2
    return 1
  fi
  local missing=0
  for term in "lane-autodispatch" "bus-worker" "ib-refresh" \
              "autodispatch/heartbeat" "bus-worker/heartbeat" "ctl-loop.heartbeat" \
              "start-control-plane"; do
    if ! grep -qF "$term" "$doc_file"; then
      echo "DOC MISSING: $term" >&2
      missing=1
    fi
  done
  if [[ "$missing" -eq 0 ]]; then
    echo "OK doc alignment in $doc_file"
  fi
  return "$missing"
}

check_paths() {
  local base="$1"
  local miss=0
  local bin_dir="$base/bin"

  echo "Checking control-plane binaries in $bin_dir..."
  local bins=(start-control-plane ctl-loop lane-autodispatch lane-merge bus-worker-loop health-loop ib-refresh)
  for b in "${bins[@]}"; do
    if [[ -x "$bin_dir/$b" || -f "$bin_dir/$b" ]]; then
      echo "OK bin $b"
    else
      echo "MISSING bin $bin_dir/$b"
      miss=1
    fi
  done

  echo "Checking heartbeat and status paths under $base..."
  # lane-autodispatch heartbeat
  if [[ -f "$base/autodispatch/heartbeat" ]]; then
    echo "OK heartbeat autodispatch/heartbeat"
  else
    echo "MISSING heartbeat $base/autodispatch/heartbeat"
    miss=1
  fi

  # bus-worker heartbeat or dir
  if [[ -f "$base/bus-worker/heartbeat" || -d "$base/bus-worker" ]]; then
    echo "OK heartbeat/dir bus-worker/heartbeat"
  else
    echo "MISSING bus-worker heartbeat/dir $base/bus-worker"
    miss=1
  fi

  # ib-refresh heartbeat / status
  if [[ -f "$base/ctl-loop.heartbeat" || -f "$base/ib-refresh.status" ]]; then
    echo "OK heartbeat ctl-loop.heartbeat or ib-refresh.status"
  else
    echo "MISSING ib-refresh heartbeat $base/ctl-loop.heartbeat or $base/ib-refresh.status"
    miss=1
  fi

  return "$miss"
}

run_self_test() {
  echo "Running self-test (offline mock fixture)..."
  check_doc

  local sim_dir
  sim_dir="$(mktemp -d "${TMPDIR:-/tmp}/ctl-boot-test.XXXXXX")"
  # Clean up before returning
  mkdir -p "$sim_dir/bin" "$sim_dir/autodispatch" "$sim_dir/bus-worker"
  touch "$sim_dir/bin/start-control-plane" \
        "$sim_dir/bin/ctl-loop" \
        "$sim_dir/bin/lane-autodispatch" \
        "$sim_dir/bin/lane-merge" \
        "$sim_dir/bin/bus-worker-loop" \
        "$sim_dir/bin/health-loop" \
        "$sim_dir/bin/health-check.py" \
        "$sim_dir/bin/ib-refresh"
  chmod +x "$sim_dir/bin"/*

  touch "$sim_dir/autodispatch/heartbeat"
  touch "$sim_dir/bus-worker/heartbeat"
  touch "$sim_dir/ctl-loop.heartbeat"
  touch "$sim_dir/ib-refresh.status"

  local test_res=0
  check_paths "$sim_dir" || test_res=$?
  rm -rf "$sim_dir"
  if [[ "$test_res" -ne 0 ]]; then
    return "$test_res"
  fi
  echo "OK self-test passed cleanly"
}

# Parse options
target=""
for arg in "$@"; do
  case "$arg" in
    --doc-only)
      check_doc
      exit 0
      ;;
    --self-test|--mock)
      run_self_test
      exit 0
      ;;
    *)
      target="$arg"
      ;;
  esac
done

target="${target:-$HOME/phanes-ctl}"
check_paths "$target"
