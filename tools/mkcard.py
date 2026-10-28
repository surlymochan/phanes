#!/usr/bin/env python3
"""Build a task card from an audit section range. Usage: mkcard.py <out> <title> <audit file> <start> <end> <scope> <checks>"""
import sys

out, title, audit, start, end, scope, checks = sys.argv[1:8]
lines = open(audit).read().splitlines()
body, on = [], False
for line in lines:
    if line.startswith(start):
        on = True
    elif on and line.startswith(end):
        break
    if on:
        body.append(line)
findings = "\n".join(body)

card = f"""# {title}

Repository: OpenBot desktop (Electron, Bun, TypeScript, SolidJS). Read AGENTS.md and the AGENTS.md of every
directory you change, and follow them.

## Scope

{scope}

## Findings (from an audit written before the upstream sync; file and line references may have moved)

{findings}

## Rules

- First confirm each finding against the current code. If upstream already fixed or covered it, do not change it:
  report "already fixed" with the file and line that show it. That is a valid result.
- Do every finding in scope before you finish; report each one as fixed / already fixed / not fixed (why).
- Smallest change that fixes the behavior. No new dependency. User-visible text comes from `@openbot/i18n` catalog
  keys (read docs/i18n.md before adding a key; add it to every locale). Errors that main/backend send use `sourceText(key)`.
- Tests: E2E scripts under `scripts/` only (repository rule); vitest only for the protected areas named in AGENTS.md.
  Do not add a package.json script; run a new script with `bun scripts/<name>.ts`. Running this repository's own
  E2E scripts, Electron ones included, is allowed.
- Do not change released Team API wire meaning, IPC contract names, or migrations. No database migration.
- Never read or print `.env*`, `.env.keys`, `.dev.vars`, auth or credential files. Read one file per command.
- Stay inside this worktree. Do not use Computer Use or drive other desktop apps. Do not commit (the runner
  commits) and do not push.
- Run only narrow checks, one at a time: {checks}, and `bunx biome check <changed files>`.
- Add a release note `changelog.d/<short-name>.md` (format: see existing files in changelog.d/).

## Report

For each finding: fixed / already fixed / not fixed (why), the files changed, and the exact checks you ran with results.
"""
open(out, "w").write(card)
print(out, len(card))
