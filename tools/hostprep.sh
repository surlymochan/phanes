set -u
R=$(python3 -c "import json,os;print([l['repo'] for l in json.load(open(os.path.expanduser('~/.claude/openswarm/lanes.json')))['lanes']][0])")
cd "$R" || exit 9
export PATH=$HOME/.openbot-task-tools/bin:$HOME/.openbot-task-tools/node-v24.20.0-linux-x64/bin:$HOME/.bun/bin:$HOME/.local/bin:/usr/local/bin:$PATH
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then echo "DIRTY $R"; git status --short | head -5; exit 3; fi
git fetch -q https://github.com/nibiru-labs/openbot.git sync/upstream-2026-10-28-squash || { echo FETCH_FAIL; exit 4; }
git checkout -q --detach b50d5ce925 || exit 5
echo "head $(git rev-parse --short HEAD) bun $(bun --version 2>/dev/null) node $(node --version 2>/dev/null)"
OPENBOT_SKIP_SKIA=1 bun install --frozen-lockfile > /tmp/hostprep-install.log 2>&1; rc=$?
echo "install rc=$rc"; tail -2 /tmp/hostprep-install.log
df -h . | tail -1 | awk '{print "disk left",$4}'
