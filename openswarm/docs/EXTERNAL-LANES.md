# External lanes (GitHub-only executors)

> **Onboarding an agent?** Send it one sentence: *"Read openswarm's external agent onboarding guide at
> https://github.com/surlymochan/phanes/blob/main/openswarm/sdk/external-lane/README.md and follow it (your agent name is `<name>`)."*
> The [For agents](../sdk/external-lane/README.md#for-agents-read-this-section-and-follow-it) section is self-contained.
> Nothing needs to change on the dispatcher side.

An external lane is an executor that can read and write the repository on GitHub but has no SSH. It pulls work itself,
through a zero-cost wake gate (`openswarm external check --agent <name>`). The contract and the agent SDK live in
[`sdk/external-lane`](../sdk/external-lane/README.md). This page covers the dispatcher side.

## Self-registration

- Any `backend:<name>` label whose name is not internal (`_internal`, default `pi codex agy copilot`) is an external lane.
  So is any name on a `lane-claimed` card; its label is created on the next tick if missing.
- New agents start with **starter cards** labelled `backend:external`. Any external agent may claim one, and the claim
  re-pins it to `backend:<name>`. The tick keeps `pool_size` (default 1) starter cards available.
- **Refills** (relabelling `lane-ready` cards to `backend:<name>`, up to `max_inflight`, default 1) go only to names listed
  in policy (the allowlist) or names that have claimed before. A typo'd name never claims, so it never gets cards.
- Fleet dispatchers must skip `lane-claimed`, `backend:external` and every external `backend:<name>`
  (`contract.fleet_must_skip`). `openswarm external reserved` prints the rule.

## Configure (all optional except `_defaults.repo`)

```json
{"external_backends": {
  "_defaults": {"repo": "owner/name", "max_inflight": 1, "pool_size": 1, "claim_timeout_s": 21600, "max_requeues": 2,
                "assign_from_backends": ["codex", "pi"], "assign_exclude_hot": ["openswarm/bin/openswarm"]},
  "_internal": ["pi", "codex", "agy", "copilot"],
  "chatgpt-agent": {"max_inflight": 2, "branch_prefixes": ["fleet/chatgpt-"]}}}
```

| Key | Meaning |
|-----|---------|
| `_defaults` | Applies to every external name; a named entry overrides it. A named entry also allowlists that name for refills. |
| `max_inflight` | Upper bound on `backend:<name>` cards that are `lane-ready` or `lane-claimed`. Cards at the PR stage do not count. |
| `pool_size` | Number of `backend:external` starter cards to keep. 0 turns the pool off. |
| `auto_assign` | Default true. Refills come from `lane-ready` cards that are unpinned (or pinned to one of `assign_from_backends`), have an acceptance line, have no `依赖`/`Depends on` and no rework marker, and touch no file in `assign_exclude_hot`. |
| `claim_timeout_s` | A `lane-claimed` card with no linked PR activity and no issue activity for this long goes back to `lane-ready`. After `max_requeues` requeues it becomes `lane-failed` + `needs-human` instead. |
| `branch_prefixes` | Extra head-ref prefixes recognised as this agent's PRs. `fleet/<name>-` is always included. |

## Run

```bash
openswarm external tick [--name N] [--dry-run]   # dispatcher pass; run every few minutes
openswarm external init | status | reserved
openswarm external check --agent X               # agent side (wake gate): rc 0 card / 1 nothing / 2 error
openswarm external tick --agent X                # agent side: one round, claims ≤ 1 card
```

On each pass, the dispatcher tracks each agent's PRs by number. A PR is linked to its card by `Closes #N`, or by
`fleet/<name>-issue<N>-…` when the body has no such line. The pass relabels the card `lane-claimed` → `lane-pr` and posts the
commit status `external-lane/contract` on the PR head. The status is `failure` for a missing link or a thin diff (fewer
than 30 effective lines, or mostly docs), and `success` otherwise. Review and merge stay with the normal lane review and
merge gate. A failing status keeps the PR out of review and merge until a new head fixes it.

## Event wake-ups (inbox PR)

Some agent platforms deliver only pull-request events. For those, `openswarm external init` creates (idempotently) one
**inbox PR** per allowlisted name: a draft on branch `external-inbox/<name>` with a single empty commit, labelled
`external-inbox`, never merged. `.github/workflows/external-lane-notify.yml` runs on `issues` opened, reopened and
labeled events. When the card has `lane-ready` plus exactly one external `backend:<name>` and that name has an open
inbox PR, the workflow comments there with the issue number and link. A hidden marker dedupes it to once per card
per 24 h. Permissions: `contents: read` (sparse checkout of the SDK), `issues: read`, `pull-requests: write`.

The allowlist the workflow sees is the set of open inbox PRs. `init` creates them only for names listed in
`external_backends`. Self-registered names keep polling until someone adds them to the list and runs `init`.
Lane review, merge-green and the external tick never touch inbox PRs: the branch is outside `fleet/`/`brain/`, the
PR is a draft, and it carries the `external-inbox` label.
