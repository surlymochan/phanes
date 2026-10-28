# M10 #66: E2E for a signed webhook request that starts a routine (tests 1)

Repository: OpenBot desktop (Electron, Bun, TypeScript, SolidJS). Read AGENTS.md and the AGENTS.md of every
directory you change, and follow them.

## Scope

Test-coverage finding 1 below. First check the existing scripts (scripts/agent-webhook-routine-e2e.ts, scripts/webhook-test-event-e2e.ts, scripts/workflow-webhook-e2e.ts) and report what they already cover. Add one new E2E script only for the uncovered path (a correctly signed request starts the routine; a bad signature or replay is refused). No product code change unless a test exposes a real bug; then report it instead of fixing it.

## Findings (from an audit written before the upstream sync; file and line references may have moved)

## 1. A signed webhook request never starts a routine in a test

**Evidence**

- `docs/webhooks.md:1` describes the flow for the user. `docs/webhooks.md:99` lists nine answer codes.
- `docs/architecture/events.md:10` names the flow: signed request, route, routine, run.
- `src/main/host-events-service.ts:192` is `HostEventsService.receive`.
- `src/main/webhook-security.ts:54` is `verifyWebhookSignature`.
- `src/backend/webhook-trigger.ts:11` validates the trigger, `src/backend/webhook-trigger.ts:21` matches
  the filters, and `src/backend/webhook-trigger.ts:33` builds the run instruction.
- `src/main/host-events-api.ts:38` is the `HostEventsApi` surface.
- No file in `scripts/` imports any of these modules. `scripts/signal-security-e2e.ts:471` checks only
  the headers at the Signal relay. `scripts/team-api-routes-e2e.ts:708` checks only that the events
  route refuses HTTP.

**Why it matters**

A webhook routine is the only way to start work from an outside service. The sender reads the status
code and retries on 503. A wrong signature check, a wrong filter match, or a wrong deduplication key
drops production events without a message. The answer codes are a public contract, and a secret is
involved.

**Fix**

Add `scripts/webhook-routine-e2e.ts`. It starts the real `HostEventsService` on a temporary profile
with a fake account service and a fake routine scheduler. It sends signed requests for: a matching
event, a wrong event type, a wrong filter value, a repeated delivery ID, a bad signature, an old
timestamp, a body over 64 KiB, and a wrong content type. It asserts the status code, the receipt
row, and the run instruction. It writes `.openbot-build/webhook-routine-e2e/report.json`. Add the
same script to the `reverse-actions-e2e` job in `.github/workflows/ci.yml`.

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
