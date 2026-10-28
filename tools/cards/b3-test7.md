# M10 #66: E2E for answering a Telegram chat (tests 7)

Repository: OpenBot desktop (Electron, Bun, TypeScript, SolidJS). Read AGENTS.md and the AGENTS.md of every
directory you change, and follow them.

## Scope

Test-coverage finding 7 below. First check existing messaging scripts under scripts/ (grep for telegram-transport and messaging) and report what they cover. Add one new E2E script that starts a local fake Telegram Bot API HTTP server inside the script (no internet, no real token), points the real Telegram transport at it, delivers one incoming message and asserts the reply that the transport sends. No product code change unless a test exposes a real bug; then report it. Temporary files only under $TMPDIR or .openbot-build/.

## Findings (from an audit written before the upstream sync; file and line references may have moved)

## 7. The Telegram connection has no test

**Evidence**

- `docs/messaging.md:115` describes the connection. `docs/messaging.md:162` says: "Not confirmed: this
  flow end to end against Telegram."
- `src/backend/messaging/telegram/telegram-transport.ts:30` is `TelegramTransport`, and
  `src/backend/messaging/telegram/telegram-driver.ts:373` is the driver.
- `scripts/messaging-pause-resume-e2e.ts` uses fake drivers for Slack and Discord only.

**Why it matters**

Telegram is a shipped connector. The reply chain, the forum topic ID, the privacy mode, and the 20 MB
file limit only work for a user who tries them. A defect is invisible until then.

**Fix**

Add `scripts/telegram-messaging-e2e.ts`. It starts a fake Bot API on loopback and runs the real
`TelegramTransport` and driver with the real messaging service on a temporary profile. It sends a
mention, a reply in a chain, a direct chat, a message in a forum topic, and a file over the limit. It
checks the answer of the orchestrator, the thread that receives the reply, and the refusal of the
large file.

**Size: M**

---


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
- Run only narrow checks, one at a time: `bun run typecheck:node` and `bun scripts/<your new script>.ts`, and `bunx biome check <changed files>`.
- Add a release note `changelog.d/<short-name>.md` (format: see existing files in changelog.d/).

## Report

For each finding: fixed / already fixed / not fixed (why), the files changed, and the exact checks you ran with results.
