#!/bin/sh
# Sync the repo-owned xflow skill into installed skill dirs without pruning
# skills that are managed by companion repositories.

set -eu

SCRIPT_PATH=$0
case "$SCRIPT_PATH" in
  /*) ;;
  *) SCRIPT_PATH=$(pwd)/$SCRIPT_PATH ;;
esac

SCRIPT_DIR=$(cd "$(dirname "$SCRIPT_PATH")" && pwd)
PROJECT_ROOT=$(cd "$SCRIPT_DIR/../.." && pwd)

config_value() {
  key_path=$1
  node -e '
const fs = require("fs");
const path = require("path");
const root = process.argv[1];
const keyPath = process.argv[2].split(".");
const configPath = path.join(root, ".as-xflow", "config.json");
if (!fs.existsSync(configPath)) process.exit(0);
const expandHome = (value) => typeof value === "string" && value.startsWith("~/")
  ? path.join(process.env.HOME || "", value.slice(2))
  : value;
let value = JSON.parse(fs.readFileSync(configPath, "utf8"));
for (const key of keyPath) value = value && value[key];
if (Array.isArray(value)) console.log(value.map(expandHome).join("\n"));
else if (value) console.log(expandHome(value));
' "$PROJECT_ROOT" "$key_path"
}

CONFIG_SYNC_SCRIPT=$(config_value skills.sync_script)
CONFIG_EXTRA_SOURCE_DIRS=$(config_value skills.extra_source_dirs)
CONFIG_TARGET_DIRS=$(config_value skills.installed_dir)

default_target_dirs() {
  if [ -d "$HOME/.codex/skills" ]; then
    printf '%s\n' "$HOME/.codex/skills"
    return 0
  fi

  printf '%s\n' "$HOME/.agents/skills"
}

SYNC_SCRIPT=${XFLOW_SKILL_SYNC_SCRIPT:-${SKILLHUB_SYNC_SCRIPT:-${CONFIG_SYNC_SCRIPT:-$HOME/Documents/workspace/pro/as-skillhub/skills/skills_sync.sh}}}
EXTRA_SOURCE_DIRS=${XFLOW_SKILL_SYNC_EXTRA_SOURCE_DIRS:-${SKILL_SYNC_EXTRA_SOURCE_DIRS:-${CONFIG_EXTRA_SOURCE_DIRS:-$HOME/Documents/workspace/pro/as-skillhub/skills}}}
TARGET_DIRS=${SKILL_SYNC_TARGET_DIRS:-${CONFIG_TARGET_DIRS:-$(default_target_dirs)}}
DEFAULT_XMEM_SOURCE_DIR=$(cd "$PROJECT_ROOT/../as-xmem" 2>/dev/null && pwd || true)
XMEM_SOURCE_DIR=${XFLOW_XMEM_SOURCE_DIR:-$DEFAULT_XMEM_SOURCE_DIR}

if [ ! -f "$SYNC_SCRIPT" ]; then
  printf 'sync_installed_xflow_skill.sh: missing skills sync script: %s\n' "$SYNC_SCRIPT" >&2
  exit 1
fi

if [ ! -d "$PROJECT_ROOT/xflow" ]; then
  printf 'sync_installed_xflow_skill.sh: missing source xflow dir: %s\n' "$PROJECT_ROOT/xflow" >&2
  exit 1
fi

extra_source_found=$(mktemp)
printf '%s\n' "$EXTRA_SOURCE_DIRS" | while IFS= read -r source_dir; do
  [ -n "$source_dir" ] || continue
  [ -d "$source_dir" ] || continue
  printf 'yes\n' >"$extra_source_found"
done

if [ ! -s "$extra_source_found" ]; then
  rm -f "$extra_source_found"
  printf 'sync_installed_xflow_skill.sh: no extra source dir exists in:\n%s\n' "$EXTRA_SOURCE_DIRS" >&2
  printf 'Set XFLOW_SKILL_SYNC_EXTRA_SOURCE_DIRS or SKILL_SYNC_EXTRA_SOURCE_DIRS to the as-skillhub skills dir.\n' >&2
  exit 1
fi
rm -f "$extra_source_found"

TAB=$(printf '\t')
EXTRA_MANAGED_SKILLS=$(mktemp)

# The standalone xmem family installer rewrites the xmem install and drops its
# skillhub marker, so xmem is only managed here when that installer is off.
standalone_xmem_install_enabled() {
  [ -n "$XMEM_SOURCE_DIR" ] || return 1
  [ -d "$XMEM_SOURCE_DIR" ] || return 1
  [ -f "$XMEM_SOURCE_DIR/skill.md" ] || return 1
  return 0
}

normalize_skill_source_path() {
  normalize_candidate=$1
  normalize_candidate_dir=${normalize_candidate%/*}
  normalize_candidate_name=${normalize_candidate##*/}
  [ "$normalize_candidate_dir" != "$normalize_candidate" ] || normalize_candidate_dir=.
  normalize_candidate_dir=$(CDPATH= cd -P "$normalize_candidate_dir" 2>/dev/null && pwd -P) || return 1
  if [ "$normalize_candidate_dir" = / ]; then
    printf '/%s\n' "$normalize_candidate_name"
  else
    printf '%s/%s\n' "$normalize_candidate_dir" "$normalize_candidate_name"
  fi
}

# The shared skillhub sync resolves managed skills from a single extra source
# dir, so the aggregated list of extra source dirs makes it prune the managed
# skills owned by the other sources. Snapshot those managed skills before the
# sync, restore them afterwards and keep their source marker intact.
snapshot_extra_managed_skills() {
  printf '%s\n' "$TARGET_DIRS" | while IFS= read -r target_dir; do
    [ -n "$target_dir" ] || continue
    printf '%s\n' "$EXTRA_SOURCE_DIRS" | while IFS= read -r source_dir; do
      [ -n "$source_dir" ] || continue
      [ -d "$source_dir" ] || continue
      # Resolve source identity physically for comparison, while retaining the
      # original .skillhub-source value below for restoration.
      source_dir=$(CDPATH= cd -P "$source_dir" 2>/dev/null && pwd -P) || continue
      for source_path in "${source_dir%/}"/*; do
        [ -d "$source_path" ] || continue
        skill_name=$(basename "$source_path")
        if [ "$skill_name" = "xmem" ] && standalone_xmem_install_enabled; then
          continue
        fi
        target_path=$target_dir/$skill_name
        [ -d "$target_path" ] || continue
        marker_path=$target_path/.skillhub-source
        [ -f "$marker_path" ] || continue
        recorded_source=$(cat "$marker_path" 2>/dev/null || true)
        normalized_recorded_source=$(normalize_skill_source_path "$recorded_source") || continue
        [ "$normalized_recorded_source" = "$source_path" ] || continue
        printf '%s%s%s%s%s\n' "$target_dir" "$TAB" "$skill_name" "$TAB" "$recorded_source" >>"$EXTRA_MANAGED_SKILLS"
      done
    done
  done
}

restore_extra_managed_skills() {
  [ -f "$EXTRA_MANAGED_SKILLS" ] || return 0
  [ -s "$EXTRA_MANAGED_SKILLS" ] || return 0
  while IFS="$TAB" read -r target_dir skill_name source_path; do
    [ -n "$target_dir" ] || continue
    [ -n "$skill_name" ] || continue
    [ -n "$source_path" ] || continue
    target_path=$target_dir/$skill_name
    if [ ! -d "$target_path" ]; then
      [ -d "$source_path" ] || continue
      rm -rf "$target_path"
      cp -R "$source_path" "$target_path"
      printf 'restored extra-source managed skill: %s\n' "$target_path"
    fi
    marker_path=$target_path/.skillhub-source
    recorded_source=$(cat "$marker_path" 2>/dev/null || true)
    if [ "$recorded_source" != "$source_path" ]; then
      printf '%s\n' "$source_path" >"$marker_path"
    fi
  done <"$EXTRA_MANAGED_SKILLS"
}

verify_extra_managed_skills() {
  [ -f "$EXTRA_MANAGED_SKILLS" ] || return 0
  [ -s "$EXTRA_MANAGED_SKILLS" ] || return 0
  while IFS="$TAB" read -r target_dir skill_name source_path; do
    [ -n "$target_dir" ] || continue
    [ -n "$skill_name" ] || continue
    target_path=$target_dir/$skill_name
    if [ ! -d "$target_path" ]; then
      printf 'sync_installed_xflow_skill.sh: extra-source managed skill missing after sync: %s\n' "$target_path" >&2
      exit 1
    fi
    marker_path=$target_path/.skillhub-source
    recorded_source=$(cat "$marker_path" 2>/dev/null || true)
    if [ "$recorded_source" != "$source_path" ]; then
      printf 'sync_installed_xflow_skill.sh: extra-source managed skill marker lost after sync: %s\n' "$target_path" >&2
      exit 1
    fi
  done <"$EXTRA_MANAGED_SKILLS"
}

# Restore the extra-source managed skills even when the sync fails, so a
# failed sync never leaves the installed skills without their managed ones.
trap 'restore_extra_managed_skills || true; rm -f "$EXTRA_MANAGED_SKILLS"' 0

snapshot_extra_managed_skills

SKILLHUB="$PROJECT_ROOT" \
SKILL_SYNC_EXTRA_SOURCE_DIRS="$EXTRA_SOURCE_DIRS" \
SKILL_SYNC_TARGET_DIRS="$TARGET_DIRS" \
  sh "$SYNC_SCRIPT" xflow

restore_extra_managed_skills

install_xmem_skill_family() {
  [ -n "$XMEM_SOURCE_DIR" ] || return 0
  [ -d "$XMEM_SOURCE_DIR" ] || return 0
  [ -f "$XMEM_SOURCE_DIR/skill.md" ] || return 0

  printf '%s\n' "$TARGET_DIRS" | while IFS= read -r target_dir; do
    [ -n "$target_dir" ] || continue
    target_xmem=$target_dir/xmem

    rm -rf "$target_xmem"
    cp -RL "$XMEM_SOURCE_DIR" "$target_xmem"
    rm -f "$target_xmem/.xmem-root-skill.tmp"
    mv "$target_xmem/skill.md" "$target_xmem/.xmem-root-skill.tmp"
    mv "$target_xmem/.xmem-root-skill.tmp" "$target_xmem/SKILL.md"
    rm -f "$target_xmem/.skillhub-source"
    printf '%s\n' "$XMEM_SOURCE_DIR" >"$target_xmem/.xflow-source"

    find "$target_xmem" -type d -name '__pycache__' -prune -exec rm -rf {} +
    find "$target_xmem" -type f -name '*.pyc' -delete
    printf 'verified xmem skill sync target: %s\n' "$target_xmem"
  done
}

install_xmem_skill_family

verify_extra_managed_skills

rm -f "$EXTRA_MANAGED_SKILLS"
trap - 0

printf '%s\n' "$TARGET_DIRS" | while IFS= read -r target_dir; do
  [ -n "$target_dir" ] || continue
  target_xflow=$target_dir/xflow

  if [ ! -d "$target_xflow" ]; then
    printf 'sync_installed_xflow_skill.sh: target xflow dir missing after sync: %s\n' "$target_xflow" >&2
    exit 1
  fi

  find "$target_xflow" -type d -name '__pycache__' -prune -exec rm -rf {} +
  find "$target_xflow" -type f -name '*.pyc' -delete

  diff_file=$(mktemp)
  filtered_file=$(mktemp)
  diff -qr "$PROJECT_ROOT/xflow" "$target_xflow" >"$diff_file" 2>&1 || true
  grep -v -E 'Only in .*/xflow: \.skillhub-source$|__pycache__|\.pyc$' "$diff_file" >"$filtered_file" || true

  if [ -s "$filtered_file" ]; then
    cat "$filtered_file" >&2
    rm -f "$diff_file" "$filtered_file"
    exit 1
  fi

  rm -f "$diff_file" "$filtered_file"
  printf 'verified xflow skill sync target: %s\n' "$target_xflow"
done
