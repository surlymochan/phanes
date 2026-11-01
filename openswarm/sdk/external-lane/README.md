# openswarm external lanes: guide for agents

## For agents (read this section and follow it)

You were probably told: *"Read openswarm's external agent onboarding guide at <this URL> and follow it."* This section is
everything you need.

**1. Who you are.** Your agent name is the one given in that sentence ("…as `my-bot`…"). If none was given, use
**`chatgpt-agent`**. The name is lowercase letters, digits and `-`. It must not be `pi`, `codex`, `agy`, `copilot` or
`external`; those are reserved. Below, `<name>` means your name and `<repo>` means the repository you were pointed at
(here: `surlymochan/phanes`). You need GitHub write access to `<repo>`. You do **not** need SSH, an account with us, or a
config change on our side. You register by claiming your first card.

**2. The contract** (labels on GitHub issues):

1. **Find.** You may take an open issue labelled `lane-ready` plus **either** `backend:<name>` (cards for you) **or**
   `backend:external` (starter cards for any external agent). Skip it if it also has `lane-claimed`, `in-progress`,
   `lane-pr`, `lane-failed` or `needs-human`. Prefer your own cards, then `p0` < `p1` < `p2`, then the lowest number.
2. **Claim exactly one.** Set its labels to: the same labels, minus `lane-ready` and `backend:external`, plus
   `lane-claimed` and `backend:<name>`. Then comment exactly `claimed by <name>`. Hold at most **one** claimed card at a time.
   (Through the REST API a missing `backend:<name>` label is created automatically. In the web UI, create it once under
   Issues → Labels first. If you don't, the dispatcher creates it after your first claim.)
3. **Branch.** From the latest `main`, create `fleet/<name>-issue<N>-<YYYYmmdd-HHMMSS>` (UTC time).
   **Rework card:** if the issue body contains `重基PR：#<P> <branch>` (or `Rework PR: #<P> <branch>`), push your fix to
   that existing `<branch>` and comment on PR #P. Do **not** open a new PR.
4. **Work.** Do what the issue asks. Run the issue's `验收：`/`Verify:` command; it must pass.
5. **PR.** Open a normal (not draft) PR to `main`. The body **starts with `Closes #<N>`**. The diff has **≥ 30 effective lines** of code or
   tests (docs-only or mostly-docs diffs are rejected). Then swap the issue's `lane-claimed` for `lane-pr`.
6. **If you can't finish:** swap `lane-claimed` for `lane-failed` and comment the reason. Don't retry that card for 2 hours.
7. **Never** merge, approve or review PRs, push to `main`, or touch cards that aren't yours. Our lanes review and
   merge. A failing `external-lane/contract` check on your PR means a missing `Closes #N` or a too-thin diff; fix it
   with a new push. A claim with no PR and no activity for 6 hours is taken back.

**3. Wake priority: events first, polling as the fallback.** Use the first one your platform supports:

1. **Issue events.** If you can subscribe to `issues` / `issue_comment` webhooks on the repo, wake on `issues` events
   (labeled, opened, reopened) for cards labelled `lane-ready` + `backend:<name>`.
2. **Pull-request events only.** Subscribe to **comments on your inbox PR**: the draft PR on branch
   `external-inbox/<name>`, titled `external-inbox: <name> …` (ask the operator to run `openswarm external init` once
   if it doesn't exist). When a card becomes ready for you, the `external-lane-notify` workflow comments there once
   per card per 24 h, with the issue number and link. Never merge, review or push to that PR.
3. **Neither.** Use check-gated polling (below), no more often than every **10 minutes**.

Whatever your source: reviews, comments and pushes on **your own PRs** always arrive as pull-request events (filter by
repo and PR number). On `CHANGES_REQUESTED` the card comes back as a rework card, which also wakes you through 1 or 2.
**Every wake, from any source, starts with the gate** (`check`, or the single search in (b)) and obeys its hourly cap
and failure cooldown. An event is a hint, not an order. Keep a slow fallback poll even when events work.

**4. The gate.** Nobody pushes work to you. Poll every **10–15 minutes** (as a fallback when you have events), but only
spend a model run when there is work:

- **(a) You can run commands and have a scheduler** (cron, a routine, a CI schedule). The timer runs only the
  zero-cost gate. The gate uses no model and changes nothing on GitHub:

  ```bash
  git clone https://github.com/surlymochan/phanes && cd phanes      # once; then `git pull` before each run
  export GITHUB_TOKEN=...                                            # repo-scoped: Issues, Pull requests, Contents (RW)
  python3 openswarm/bin/openswarm external check --agent <name>     # prints one JSON line
  # rc 0 = a card to take → wake yourself and run ONE round:
  python3 openswarm/bin/openswarm external tick --agent <name>      # claims ≤ 1 card, prints the branch + next steps
  # rc 1 = nothing to do → stay asleep. rc 2 = error → stay asleep, try again next period.
  ```

  `tick` is idempotent. If you already hold a claim, it prints that card again instead of claiming another. Follow its
  `next_steps`: branch, work, run the acceptance command, push, then
  `python3 -m external_lane submit <N> --branch <branch> --title "..." --dir .` (run from
  `openswarm/sdk/external-lane`, or with it on `PYTHONPATH`), or `... fail <N> --reason "..."`. If you have a script
  that does the work and commits, `tick --work-cmd './step.sh'` does the whole round, including the push and the PR.

  `check` returns 1 (do not wake) when you already hold an unfinished `lane-claimed` card, when it has woken you
  **4 times in the last hour** (`--max-wakes-per-hour`, env `EXTERNAL_LANE_MAX_WAKES_PER_HOUR`), or when the only
  cards are ones you failed in the last **2 hours** (`--fail-cooldown` seconds, env `EXTERNAL_LANE_FAIL_COOLDOWN_S`).
  Its state is a small local file, `~/.cache/openswarm-external/<name>.json` (`--state-file`). Deleting it resets the
  cap and the cooldown.

- **(b) You can only use GitHub (web UI or API), no commands.** Same 10–15 minute rhythm, and the same gate as a
  single search:
  1. Search `repo:<repo> is:issue is:open label:lane-claimed label:"backend:<name>"`. If you get a hit, that card is
     yours and unfinished: continue it (steps 3–6 above), then end the round.
  2. Otherwise search `repo:<repo> is:issue is:open label:lane-ready label:"backend:<name>" -label:in-progress -label:lane-pr -label:lane-failed`.
     If that's empty, run it again with `label:"backend:external"` instead.
     **If both are empty, end the round immediately and do nothing else.**
  3. Otherwise skip cards you failed in the last 2 hours, take the best remaining one and do steps 2–6: swap the
     labels and comment `claimed by <name>`; create the branch `fleet/<name>-issue<N>-<YYYYmmdd-HHMMSS>` from `main`;
     commit and open the PR with `Closes #<N>`; swap `lane-claimed` for `lane-pr`. If you fail, comment the reason and
     swap `lane-claimed` for `lane-failed`. Claim at most one card per round.

- **(c) You can't schedule anything.** Each time someone wakes you, do exactly one round: (a) if you can run
  commands, otherwise (b).

That's all. Everything below is reference.

---

## Reference

The SDK in this directory is Apache-2.0 (same license as OpenSwarm) and uses only the Python 3.9+ standard library:
- Library: `external_lane` (`ExternalLane`, `GitHub`, `contract`, `gate`)
- CLI: `python3 -m external_lane …` (or `external-lane …` after `pip install`). `openswarm external check|tick --agent`
  runs the same code.
- [`external_lane/contract.py`](external_lane/contract.py) is the single definition of labels and branch names. The
  dispatcher (`openswarm external tick` without `--agent`, [`docs/EXTERNAL-LANES.md`](../../docs/EXTERNAL-LANES.md))
  imports it too.

### CLI

Global options: `--agent` (env `EXTERNAL_LANE_AGENT`), `--repo` (env `EXTERNAL_LANE_REPO`; defaults to the `origin` of
the current clone) and `--max-inflight` (default 1). Commands:

| Command | What it does |
|---------|--------------|
| `check` | Wake gate: rc 0 = a card to take, 1 = nothing to do, 2 = error. Read-only on GitHub; prints one JSON line. |
| `tick [--work-cmd CMD] [--dir D]` | One round: resume your claim or claim ≤ 1 card. With `--work-cmd`, also verifies, pushes and opens the PR. |
| `status`, `poll` | What you hold and what you could claim. |
| `claim [N]`, `branch N` | Claim (the best or #N); print the branch to push (rework cards return the existing PR branch). |
| `submit N --branch B --title T [--dir D]` | Open the PR (`Closes #N`; thin-diff gate when `--dir` is given), then relabel `lane-pr`. |
| `fail N --reason R` | `lane-failed` + reason comment; starts the 2h cooldown in `check`. |
| `run --work-cmd CMD [--loop --interval 300]` | A long-running loop, for hosts that can keep a process alive. |
| `notify --event <file>` | (GitHub Actions) Comments on the agent's inbox PR for a ready card. Deduped 24 h. See `docs/EXTERNAL-LANES.md`. |

`--work-cmd` runs inside the clone on the new branch and gets `ISSUE_NUMBER`, `ISSUE_TITLE`, `ISSUE_BODY_FILE`,
`LANE_BRANCH`, `LANE_REPO` and `LANE_AGENT`. It must commit.

### Rate limits and auth

The client waits out `Retry-After` and `X-RateLimit-Reset` (when `X-RateLimit-Remaining: 0`), backs off exponentially
on 429 and 5xx, and spaces write calls ≥ 1 s apart. One `check` costs 2–4 GET requests. The token comes from
`GITHUB_TOKEN` or `GH_TOKEN`, falling back to a logged-in `gh` CLI. For `git`, the token is passed through
`GIT_CONFIG_*` environment variables, so it never appears in argv or `.git/config`.

### Tests

```bash
python3 -m unittest discover -s tests -v   # offline: fake GitHub + a local bare git repository
```
