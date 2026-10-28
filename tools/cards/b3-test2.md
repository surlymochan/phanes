# M10 #66: E2E for media attachments and size limits (tests 10)

Repository: OpenBot desktop (Electron, Bun, TypeScript, SolidJS). Base commit: b50d5ce925 (head of PR nibiru-labs/openbot#42,
the upstream sync). Read AGENTS.md and the AGENTS.md of every directory you change, and follow them.

## Continue

Your base (commit eeab7f945d) already has `scripts/media-attachment-e2e.ts` (386 lines) from your earlier run, which the risk guard stopped before you ran it. Run it with `bun scripts/media-attachment-e2e.ts`, fix the script until it passes and writes its JSON report under `.openbot-build/`, then add the release note.

## Scope

Test-coverage finding 10 below. Check the existing scripts first (scripts/mp4-attachment-e2e.ts, scripts/zip-attachment-e2e.ts, scripts/composer-attachments-e2e.ts). Add one new E2E script for the parts not yet covered (MP3 and MOV attachments, the per-file, per-message and file-count limits). No product code change unless a test exposes a real bug; then report it instead of fixing it.

## Findings (from an audit written before the upstream sync; file and line references may have moved)

## 10. The media attachments and their size limits have no test

**Evidence**

- `README.md:493` sets the limits: 100 MB per file, 250 MB per message, and 10 files per message.
- `packages/contracts/src/attachment-files.ts:3` names the media kinds `mp3` and `mov`.
- `src/backend/attachment-files.ts:32` holds the file count limit, `src/backend/attachment-files.ts:33`
  the file byte limit, and `src/backend/attachment-files.ts:34` the total byte limit.
- `packages/contracts/src/team-protocol/current.ts:78` names the capability `media-attachments` that a
  remote host must have.
- No script names `mp3` or `mov`.

**Why it matters**

The limits are a promise to the user and to a remote host. A defect either refuses a valid recording
or accepts a file that the host cannot store. The README also says that OpenBot does not play or
validate a recording, so the store is the only check.

**Fix**

Add `scripts/media-attachment-e2e.ts`. It runs the real attachment file service on a temporary
profile. It attaches an MP3 and a MOV, and checks the stored file and its MIME type. It checks the
refusal of a file over 100 MB, of a message over 250 MB, and of an eleventh file. It checks that a
host without the `media-attachments` capability refuses the file. The byte limits are large, so the
script writes sparse files instead of real data.

**Size: S**

---

## Rules

- First confirm each finding against the current code. If upstream already fixed one, do not change it: report
  "already fixed" with the file and line that show it. That is a valid result.
- Smallest change that fixes the behavior. No new dependency. User-visible text comes from `@openbot/i18n` catalog
  keys (read docs/i18n.md before adding a key; add it to every locale). Errors that main/backend send use `sourceText(key)`.
- Tests: E2E scripts under `scripts/` only (repository rule); vitest only for the protected areas named in AGENTS.md.
  Do not add a package.json script; run a new script with `bun scripts/<name>.ts`.
- Do not change released Team API wire contracts, IPC contract names, or migrations.
- Never read or print `.env*`, `.env.keys`, `.dev.vars`, auth or credential files; the risk guard stops the run. Read one file per command; do not chain `cat` and `echo`.
- Stay inside this worktree. Do not use GUI apps or Computer Use. Do not commit (the runner commits) and do not push.
- Run only narrow checks, one at a time: `bun run typecheck:node` and `bun scripts/<your new script>.ts`, and `bunx biome check <changed files>`.
- Add a release note `changelog.d/<short-name>.md` (format: see existing files in changelog.d/).

## Report

For each finding: fixed / already fixed / not fixed (why), the files changed, and the exact checks you ran with results.
