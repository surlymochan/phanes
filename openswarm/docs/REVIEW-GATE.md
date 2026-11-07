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

## A new head requires a new review

**新 head = 未审。** Any operation that changes a PR head, including rebase, a CI
fix, or union-merge, produces a new commit that needs another review. A successful
review status on the previous SHA does not approve the new SHA. Keep the PR out
of merge until the review and required checks apply to its current head.

## The content check: a merge is not a delivery (Refs #1814)

PR #1799 was merged with green checks, but the merged head carried only the
review record `.lane-review/PR-1799.md`: the functional commits stayed on a side
branch, so the merge delivered nothing. The merge gate (`openswarm merge-gate`)
therefore judges the file list of the PR as well (`lib/merge/content.py`):

- a changed code, test or tool path is a delivery;
- a changed review or evidence artifact (`.lane-review/`, `docs/evidence/`) is
  not;
- a PR with neither is a suspected empty delivery: the merge is blocked with
  `blocked:empty_delivery`, the PR is labelled `needs-human`, and a comment
  explains it (`疑似空交付：PR 不含关卡功能变更`). The gate never closes a PR.

A card declares the paths it delivers in its issue, and a changed file that
matches a declared path counts as the delivery (the declaration wins over the
artifact classification):

```
产物路径：docs/architecture-review-brief.md
Expected-Paths: `tools/check_evidence.py`
```

The failure modes are explicit: when the file list is missing, null or cannot be
read the merge stays blocked (`blocked:content_unreadable`, or
`blocked:gh_error` when the PR itself cannot be read) and the gate warns on
stderr; it never merges on a guess and never passes silently. A content check
that cannot run at all (a missing or broken `lib/merge/content.py`) blocks the
same way. A label or a comment that failed is reported on stderr and tried
again in the next round.

The gate lifts the `needs-human` label again when the delivery arrives, but
only the label it added itself: an escalation of the dispatcher (a review that
keeps failing, a CHANGES round that needs a human) stays untouched.
`--no-content-check` turns the check off.
