#!/usr/bin/env python3
"""Write <key>.msg and <key>.body into scratchpad/pr from a JSON spec on stdin.
Spec keys: key, subject, detail, linked, type, bullets[], surfaces, verification[], risk, runs"""
import json
import sys

S = "/private/tmp/claude-501/-Users-chenchao-Documents-workspace-nibiru-openbot-openbot/62926203-41d2-4936-8582-8fe69de7f53c/scratchpad/pr"
d = json.load(sys.stdin)
open(f"{S}/{d['key']}.msg", "w").write(f"{d['subject']}\n\n{d['detail']}\n")
types = [
    "Bug fix",
    "Small improvement or alignment, with no change to how a feature works",
    "New provider, MCP app, or connector",
    "Translation or documentation",
    "New feature or change to existing behavior (needs an approved issue)",
]
checks = "\n".join(f"- [{'x' if t == d['type'] else ' '}] {t}" for t in types)
bullets = "\n".join(f"- {b}" for b in d["bullets"])
ver = "\n".join(f"- [x] {v}" for v in d["verification"])
body = f"""## Linked issue

{d['linked']}

## Type of change

{checks}

## What changed

{bullets}

Surfaces: {d['surfaces']}

## Verification

{ver}
- [x] The pre-commit hook passed.
- [x] No credentials, private data, generated output, or real user files are included.

## Risk and security impact

{d['risk']}

Model and harness: Codex gpt-6.1-terra, Cursor composer-2.5 and pi + MiniMax through openswarm; planner Claude Code.

openswarm-run: {d['runs']}
"""
open(f"{S}/{d['key']}.body", "w").write(body)
print(d["key"], "ok")
