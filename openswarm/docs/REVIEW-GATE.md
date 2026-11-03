# Review gate

## Offline branch-protection check

`lib/review/protection.py` checks a saved GitHub branch-protection JSON response.
It verifies that the configured review status (default `lane-review`) is required,
using both legacy `required_status_checks.contexts` and modern `checks[].context`,
and that `enforce_admins.enabled` is true. Missing protection, a missing context,
or administrators being exempt produces a warning and exit code 1. A compliant
snapshot exits 0; invalid input exits 2. This is an offline snapshot check, not a
live query or a check of rulesets and other GitHub policies.

```sh
# Fetch a snapshot separately, then inspect it offline.
gh api repos/OWNER/REPO/branches/main/protection > protection.json
python3 openswarm/lib/review/protection.py --json protection.json --repo OWNER/REPO --branch main

# If the branch has no protection, explicitly supply null.
echo null | python3 openswarm/lib/review/protection.py --json -
```

`--context NAME` selects a different required status. On warnings the CLI prints a
suggested `gh api -X PUT ... --input -` command, but never executes it. Supply the
actual `--repo` and `--branch` before using the suggestion; the default repository
is the placeholder `OWNER/REPO`. The JSON adds the context and enforces admins,
preserving existing check contexts/app bindings, review requirements, push actor
restrictions and supported boolean protection settings. Read-only API response
fields are omitted from the PUT payload. Review the command against a fresh
snapshot before applying it. The checker is independent of doctor wiring.

## The doctor checks the same gate

`openswarm doctor` queries the branch protection of the repository the lane-review status is
published on and reports whether the protection requires the review context and binds
administrators. A missing rule is a `warn` with a copyable repair command; the doctor never
changes the branch settings.

The gate has exactly one source: the dispatcher config. The publisher, the CI filter (which
excludes the review context from the check rollup) and the doctor all resolve it through
`status.resolve_gate(cfg)`:

| field | meaning | default |
| --- | --- | --- |
| `repo` | the repository the commit status is published on | none |
| `review.status_context` | the context the commit status is published under | `lane-review` |
| `base_branch` | the branch whose protection has to require that context | `main` |

The dispatcher config is read from `--dispatch-config`, from `OPENSWARM_DISPATCH_CONFIG`, or
from `~/.claude/openswarm-dispatch/config.json`. A dispatcher that publishes under another
context therefore makes the doctor demand that context, not the default one.

Without that config the gate is **unknown**, not default: the dispatcher's `--config` is a
required argument, so the repository, the status context and the branch that are published
and enforced cannot be read from the worktree. The doctor then reports a `warn` (never an
`ok`) and asks for `--dispatch-config <path to the config the dispatcher serves with>`.
Guessing the repository from the git remote would check a rule nobody enforces, so the
doctor does not do it.

## A new head requires a new review

**新 head = 未审。** Any operation that changes a PR head, including rebase, a CI
fix, or union-merge, produces a new commit that needs another review. A successful
review status on the previous SHA does not approve the new SHA. Keep the PR out
of merge until the review and required checks apply to its current head.
