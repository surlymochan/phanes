# Fix the context-usage-v1 Team API addition before it can ship

Repository: OpenBot. Read AGENTS.md, packages/contracts/AGENTS.md, src/main/AGENTS.md and src/preload/AGENTS.md.
Base: held-context-usage (cb6db55189). A read-only protocol review found the problems below. A released wire contract is permanent, so the
contract must be right before it ships.

## Review findings to fix (file:line refer to the base branch)

## Context usage

1. **Wire.** The capability, POST route, and request/response are additive (`packages/contracts/src/team-protocol/context-usage-v1.ts:21`, `:59`). The host checks the capability (`src/main/team-api/route-context-usage.ts:21`); mobile checks before use (`apps/mobile/src/features/workspace/context/host-request-actions.ts:780`). An optional side codec is registered for both transport paths (`packages/contracts/src/team-protocol/optional-routes.ts:97`, `packages/contracts/src/team-protocol/side-routes.ts:51`), with an error envelope (`packages/contracts/src/team-protocol/admin-wire.ts:82`). The POST body `{agentId}` and 200 object follow `context-reset-v1` (`src/main/team-api/route-context-reset.ts:23`). However, the wire codec imports the live IPC category list (`packages/contracts/src/team-protocol/context-usage-v1.ts:7`, `:47`), contrary to a frozen independent wire contract. The mobile response decoder turns malformed known usage or compaction into null, filters bad category rows, and accepts numeric values that the strict codec rejects (`packages/contracts/src/team-protocol/context-usage-v1.ts:91`, `:101`, `:116`, `:128`; `packages/contracts/src/team-protocol/admin-wire.ts:31`). This conflicts with the fail-closed rule in `packages/contracts/AGENTS.md`. The branch adds no desktop IPC endpoint for this feature; separate `FromHost` and `FromMain` decoders are therefore not currently applicable. If desktop IPC support is added, keep those boundaries separate.
2. **Authorization.** The host checks hidden and missing agents, then requires **chat** (`src/main/team-api/route-context-usage.ts:25`). View is refused; chat, manage, and owner can read, subject to visibility. This matches context reset's chat threshold (`src/main/team-api/route-context-reset.ts:27`) and the branch's declared policy (`packages/contracts/src/team-protocol/context-usage-v1.ts:3`). Desktop reads its local context snapshot (`src/backend/agent-service.ts:1649`) without a remote member role. A view-level bypass was not found in this route; this conclusion is from static inspection, not a live test.
3. **Data.** The service selects the requested agent from the personal-session snapshot (`src/backend/agent-service.ts:3195`, `src/backend/agent/context-compaction.ts:108`); the route returns usage and compaction only (`src/main/team-api/route-context-usage.ts:29`). The declared fields contain counts, category labels, compaction ID, time and status, not provider text (`packages/contracts/src/team-protocol/context-usage-v1.ts:72`). The side codec projects known fields on send (`packages/contracts/src/team-protocol/admin-wire.ts:52`, `:57`, `src/main/team-api-server.ts:1767`); no text redactor is called in the route. An explicit allowlist projection at the route would also protect the boundary if transport behavior changes. No evidence of another agent's data or raw provider text was found in this path. Compaction IDs and metadata are intentionally disclosed to chat members; confirm that product decision before release.
4. **Tests.** The branch adds only a stub to `scripts/team-api-routes-e2e.ts:429`; no allowed/refused route checks were found there. `scripts/mobile-composer-parity-e2e.ts:81` says it does not drive a live host. Runtime access, transport and redaction remain unverified.
5. **Smallest fixes.** Freeze the category values in the wire module and map explicitly from IPC state; make the client reject malformed known responses with the same bounds as the wire codec; explicitly project response fields at the send boundary; add Team API E2E for chat success, view refusal, hidden/unknown agents, capability absence, malformed payloads, and no extra fields, through supported transports.

A shipped wire contract is permanent. Make and verify these changes before release; do not silently change a contract after it ships. No tests were run for this read-only review.

## Task

Apply the "Smallest fixes" item exactly. Keep the addition additive and capability-gated; do not change any
released adapter, existing route, or existing codec meaning. Keep FromHost and preload FromMain decoders separate.
Add the Team API E2E cases listed (allowed caller, refused caller with no mutation, hidden/unknown agent, capability
absent, malformed input, transports) to scripts/team-api-routes-e2e.ts and run it; scripts/team-api-legacy-clients-e2e.ts
must still pass unchanged.

## Checks

`bun run typecheck:node`, `bun run test:team-api-routes`, `bun run test:team-legacy-clients`,
`bun run --cwd apps/mobile typecheck`, `bunx biome check <changed files>`. Running local E2E scripts and binding
local ports is allowed. Never touch /tmp except single scratch files; prefer $TMPDIR. Do not use Computer Use.
Do not commit; do not push. Report each fix with file:line and each check result.
