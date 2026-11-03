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

## The fix task book carries the method block (Refs #1838)

A fix task book is the issue body a fix lane receives. Three places write into it —
the review CHANGES round (fix task), the red-CI fix (`cifix`) and the conflict
rebase — and all three append the same shared method block
(`## 修复方法（Refs #1838）`, `lib/dispatch/engine.py`):

- ① enumerate the same-defect occurrences of every previous BLOCKER first (grep
  the whole repo, list the hits, fix them all — fixing only the one the review
  named is not a fix);
- ② write the regression test that reproduces the failure first and run it on the
  current head to see it fail, then change the code. The failure is the one the
  task book is about: the previous round's BLOCKER (review CHANGES → fix task),
  the red CI check it lists (`cifix`) or the merge conflict itself (`rebase`); a
  task book without a previous BLOCKER never asks for a BLOCKER that does not
  exist;
- ③ verify with real commands and real samples, not only with mocks;
- ④ tick every previous BLOCKER off in the delivery notes
  (`BLOCKER → 同类点清单 → 测试 → 改动`);
- ⑤ continue on the PR's current head, never replace the change wholesale.

The block is written once per task book: every injection point strips the previous
copy before writing the new one, so stacked fixes (rebase → cifix → review
CHANGES) never repeat it. The items that need the previous round's BLOCKERs (①
and ④) are left out when the task book carries none — a CI fix or a rebase
without a previous review round keeps ② ③ ⑤ only.

The block constrains the *method* of the fix. The review model, the review
strictness and the acceptance checks are unchanged (Refs #1838).
