# Fix the pause/continue Team API addition before it can ship

Repository: OpenBot. Read AGENTS.md, packages/contracts/AGENTS.md, src/main/AGENTS.md and src/preload/AGENTS.md.
Base: held-pause-continue (6769e66993). A read-only protocol review found the problems below. A released wire contract is permanent, so the
contract must be right before it ships.

## Review findings to fix (file:line refer to the base branch)

## Pause continue

1. **Wire.** The route and capability are additive (`packages/contracts/src/team-api-routes.ts:160`, `packages/contracts/src/team-protocol/agent-pause-continue-v1.ts:8`). The mobile and desktop callers check the capability (`apps/mobile/src/features/workspace/context/mobile-workspace-context.tsx:1417`, `src/main/ipc/agent-handlers.ts:711`); the host checks it too (`src/main/team-api/route-agent-queue.ts:142`). The POST body `{messageId}` and empty 204 match the style of queue actions (`src/main/team-api/route-agent-queue.ts:87`), but the capability file contains no request or response codec. The route is absent from the side-route table and the frozen route selectors (`packages/contracts/src/team-protocol/side-routes.ts:51`, `packages/contracts/src/team-protocol/v1.ts:1973`, `packages/contracts/src/team-protocol/provider-aware-codec.ts:2134`). `readJson` falls through to the negotiated codec (`src/main/team-api/request-helpers.ts:242`), which rejects an unknown route (`packages/contracts/src/team-protocol/provider-aware-codec.ts:1426`); WebRTC also decodes before dispatch (`src/main/team-webrtc-host-peer.ts:616`). Thus the proposed route cannot be relied on through these transports. Host malformed JSON maps to 400; unexpected service failures reach the dispatcher, consistent with the queue pattern. Desktop already has separate `FromHost` and preload `FromMain` void decoders (`src/main/remote-host-decoding.ts:25`, `src/preload/index.ts:544`); mobile uses `ignoreResponse` (`packages/team-client/src/team-api-requests.ts:130`), which does not validate an unexpected response body. Add a dedicated frozen optional codec with dynamic-path matching and strict request/empty-response validation, including error envelopes, without changing released adapters. Check both HTTP and WebRTC and both trust sides.
2. **Authorization.** The dispatcher authenticates first and hides agents with no access (`src/main/team-api-server.ts:798`, `:811`). A visible agent with **view** access can reach this action: `agentWriteLevel` has no `pause/continue` entry (`src/main/team-api/route-agents.ts:230`, `:390`), and the handler only checks that the agent is listed (`src/main/team-api/route-agent-queue.ts:142`). This is lower than the **chat** level for queue steer, update, and interrupt (`src/main/team-api/route-agents.ts:409`). A view member who can name a marker can release holds and possibly enqueue a resume turn (`src/backend/agent/agent-pauses.ts:134`). The local desktop action uses the local service and has no remote member role (`src/main/ipc/agent-handlers.ts:711`); before this branch it is local-only. **Chat** is the consistent minimum for a remote action; manage and owner also pass. The codec defect currently prevents ordinary successful calls; it must not be treated as an authorization gate.
3. **Data.** The handler returns no body on success and accepts only an agent path and marker ID (`src/main/team-api/route-agent-queue.ts:142`). The dispatcher rejects hidden path agents (`src/main/team-api-server.ts:817`). No raw provider output or secret is intentionally sent by this route. The resume operation affects the named agent's stored markers and may queue a fixed prompt (`src/backend/agent/agent-pauses.ts:139`, `:150`); it does not return those contents. Error-path redaction is not independently verified; unexpected errors use the common dispatcher path.
4. **Tests.** No pause route checks are present in `scripts/team-api-routes-e2e.ts` on this branch. The mobile parity script explicitly excludes a live Team API route (`scripts/mobile-chat-parity-e2e.ts:108`). Allowed and refused callers are unverified.
5. **Smallest fixes.** Register `POST pause/continue` at chat level; implement a frozen optional route codec and strict mobile response decoding; add Team API E2E for chat success, view refusal with no mutation, hidden/unknown agents, capability absence, malformed input, and HTTP/WebRTC transport. Verify the separate desktop decoder boundaries and error behavior.

## Context usage

## Continue

Your base 98f259bdad already has: the optional codec and side-route registration, the chat authorization gate, client response validation, and HTTP access cases. Your earlier checks failed only because node_modules resolved @openbot/* to another checkout; this is fixed now (the worktree's @openbot/* point to its own sources). Now: add and run the WebRTC E2E cases (both trust boundaries, error behavior), then run every check below and fix what fails.

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
