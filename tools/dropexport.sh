#!/bin/bash
# Runs on gocc. Usage: dropexport.sh <worktree-dir-name> <file> <function-name>
set -uo pipefail
export PATH=$HOME/.bun/bin:$HOME/.local/node-v24.14.1/bin:/opt/homebrew/bin:/usr/bin:/bin:$PATH
cd ~/work/"$1" || exit 9
python3 - "$2" "$3" <<'PY'
import re,sys
p,name=sys.argv[1],sys.argv[2]; s=open(p).read()
n=len(re.findall(rf"^export function {name}\b",s,re.M)); assert n==1,(p,n)
open(p,"w").write(re.sub(rf"^export function {name}\b",f"function {name}",s,flags=re.M))
PY
git add -A "$2"
printf 'Remove an unused export\n\nKnip reports %s as exported but used only in its own file.\n' "$3" > /tmp/kfix.msg
git commit -q -F /tmp/kfix.msg > /tmp/kfix.log 2>&1; rc=$?
echo "$1 commit rc=$rc"; grep -E "error TS|Type checks failed" /tmp/kfix.log | head -3
git push -q origin HEAD 2>&1 | tail -1
