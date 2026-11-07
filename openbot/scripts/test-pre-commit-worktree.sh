#!/bin/bash
# Regression test for #70 and #1374: the pre-commit hook must not stage a file under a second
# path, and it must stage what the formatter changed.
#
# Git runs a hook with GIT_DIR set and without GIT_WORK_TREE. In a linked worktree GIT_DIR
# points at the worktree's own git directory, so git treats the current directory as the top of
# the working tree. The hook changes into the project directory and then runs
# `git add -- examples/x.json`, which wrote the file back to the index as `examples/x.json`
# instead of `openbot/examples/x.json`. A normal checkout never showed the bug.
#
# The test builds a throwaway repository with the real hooks, stubs `bun` so it needs no
# runtime, and commits the same file in a normal checkout and in a linked worktree. Both
# commits must contain exactly `openbot/examples/x.json`.
#
# The stub also stands in for Biome: it records the arguments of every `bun` call and, when the
# test asks for it, rewrites the working-tree copy of the staged file. A file the author staged
# completely must reach the commit in its rewritten form, and a file the author staged only
# partly must stop the commit, because the rewrite would swallow the unstaged edits.
#
# Usage: bash openbot/scripts/test-pre-commit-worktree.sh
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
openbot_root=$(cd -- "$script_dir/.." && pwd)
repository_root=$(cd -- "$openbot_root/.." && pwd)

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

# The stub replaces `bun run check:staged`: it records the staged files the hook sees and the
# arguments of every `bun` call. It rewrites the working-tree copy of the staged file only when
# the test sets BUN_REWRITE, because the formatter runs on the working tree, not on the index.
mkdir -p "$work/bin"
cat >"$work/bin/bun" <<'STUB'
#!/bin/sh
printf '%s\n' "$*" >>"$BUN_ARGV_LOG"
if [ "${1:-}" = "run" ] && [ "${2:-}" = "check:staged" ]; then
  git diff --cached --name-only >>"$BUN_STAGED_LOG"
  if [ -n "${BUN_REWRITE:-}" ]; then
    printf '{\n  "name": "rewritten"\n}\n' >"$BUN_REWRITE"
  fi
fi
exit 0
STUB
chmod +x "$work/bin/bun"
export PATH="$work/bin:$PATH"
export BUN_STAGED_LOG="$work/staged.log"
export BUN_ARGV_LOG="$work/argv.log"
: >"$BUN_STAGED_LOG"
: >"$BUN_ARGV_LOG"

# The throwaway repository has the same layout as this one: the hook lives in the repository
# root and hands over to the hook in the project directory.
main="$work/main"
mkdir -p "$main/.githooks" "$main/openbot/.githooks" "$main/openbot/examples"
cp "$repository_root/.githooks/pre-commit" "$main/.githooks/pre-commit"
cp "$repository_root/openbot/.githooks/pre-commit" "$main/openbot/.githooks/pre-commit"
chmod +x "$main/.githooks/pre-commit" "$main/openbot/.githooks/pre-commit"
echo '{}' >"$main/openbot/package.json"
printf '{\n  "name": "example"\n}\n' >"$main/openbot/examples/x.json"

git -C "$main" init -q -b main
git -C "$main" config user.email "hook-test@example.com"
git -C "$main" config user.name "Hook Test"
git -C "$main" add -A
git -C "$main" commit -qm "initial commit"
git -C "$main" config core.hooksPath .githooks

# Commits the file in the given checkout and prints the paths the commit contains.
commit_example() {
  local checkout=$1
  printf '{\n  "name": "changed"\n}\n' >"$checkout/openbot/examples/x.json"
  git -C "$checkout" add openbot/examples/x.json
  git -C "$checkout" commit -qm "stage an example" || fail "the commit in $checkout failed"
  git -C "$checkout" show --pretty=format: --name-only HEAD | sed '/^$/d' | sort
}

expected="openbot/examples/x.json"

# The linked worktree comes first, so both commits start from the same initial commit.
# Git hands a hook in a worktree a GIT_DIR and no GIT_WORK_TREE, so the hook has to find the
# repository root by itself.
worktree="$work/worktree"
git -C "$main" worktree add -q -b feature "$worktree"

normal=$(commit_example "$main")
[ "$normal" = "$expected" ] || fail "a normal checkout committed [$normal], expected [$expected]"
grep -Fxq "$expected" "$BUN_STAGED_LOG" || fail "the hook did not read the staged file in a normal checkout"

: >"$BUN_STAGED_LOG"
linked=$(commit_example "$worktree")
[ "$linked" = "$expected" ] || fail "a linked worktree committed [$linked], expected [$expected]"
grep -Fxq "$expected" "$BUN_STAGED_LOG" || fail "the hook did not read the staged file in a linked worktree"
[ ! -e "$worktree/examples" ] || fail "the hook created a second copy of the file at the repository root"
if git -C "$worktree" ls-files --cached | grep -Fxq "examples/x.json"; then
  fail "the hook staged the file a second time under the repository root"
fi

git -C "$main" worktree remove --force "$worktree" || true

# The hook runs the staged checks through bun, with the project as the working directory.
grep -Fxq "run check:staged" "$BUN_ARGV_LOG" || fail "the hook did not run the staged checks through bun"

# Biome rewrites the working-tree copy of a staged file. A file the author staged completely
# reaches the commit in its rewritten form, because the hook stages that copy again.
export BUN_REWRITE="examples/x.json"
printf '{\n  "name": "to rewrite"\n}\n' >"$main/openbot/examples/x.json"
git -C "$main" add openbot/examples/x.json
git -C "$main" commit -qm "stage an example" || fail "the commit with a rewritten file failed"
rewritten=$(git -C "$main" show HEAD:openbot/examples/x.json)
printf '%s\n' "$rewritten" | grep -Fxq '  "name": "rewritten"' ||
  fail "the commit does not carry the rewritten file, it carries [$rewritten]"

# A file the author staged only partly must stop the commit: the rewrite would swallow the
# unstaged edits.
printf '{\n  "name": "staged"\n}\n' >"$main/openbot/examples/x.json"
git -C "$main" add openbot/examples/x.json
printf '{\n  "name": "unstaged"\n}\n' >"$main/openbot/examples/x.json"
status=0
partly_output=$(git -C "$main" commit -qm "stage a partly staged example" 2>&1) || status=$?
[ "$status" -ne 0 ] || fail "the commit of a partly staged file was not stopped"
printf '%s\n' "$partly_output" | grep -Fq "Biome fixed files that also have unstaged changes" ||
  fail "the hook did not report the rewritten partly staged file, it said [$partly_output]"
unset BUN_REWRITE

echo "PASS: the pre-commit hook stages openbot/examples/x.json in a normal checkout and in a linked worktree, and it stages what the formatter changed."
