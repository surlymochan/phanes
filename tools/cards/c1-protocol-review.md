# Read-only review: two proposed Team API additions from mobile parity work

Repository: OpenBot. Read AGENTS.md, packages/contracts/AGENTS.md, src/main/AGENTS.md and docs/glossary.md.
This is a REVIEW task. Do not change any file. Do not commit.

Two executor commits added Team API protocol pieces although their task forbade it. Both are on local branches in
this repository, based on b50d5ce925:

- `held-pause-continue` (6769e66993): `POST /v1/agents/:id/pause/continue` and capability `agent-pause-continue-v1`
  so a phone can continue an agent paused by an `agent-pause` marker.
- `held-context-usage` (cb6db55189): capability/route `context-usage-v1` so a phone can show the context usage meter.

Inspect each with `git diff b50d5ce925 <branch> -- packages/contracts packages/team-client src/main src/backend`.

For each, answer with evidence (file:line):
1. Wire design: is it additive and capability-gated; is it consistent with existing frozen v1 contracts (naming,
   request/response shape, error mapping, decoders on both trust sides per src/main/AGENTS.md)?
2. Authorization: which access level can call it (view / chat / manage / owner)? Is that the right level compared
   with the desktop's local rule and with similar existing routes (queue steer/update, agent pause)? Any way for a
   lower-level member to act?
3. Data: does the route leak anything (other agents, secrets, raw provider text)? Redaction on send paths?
4. Tests: is there a Team API E2E (scripts/team-api-routes-e2e.ts) for allowed and refused callers?
5. Verdict: ACCEPT as is / ACCEPT with listed fixes / REJECT, and the smallest fix list. Remember a released wire
   contract is permanent.

Write the review to `.openbot-build/protocol-review/report.md` and put the verdicts in your final summary.
