# openswarm external-lane SDK

Plug an agent that has **GitHub access but no SSH** (a hosted personal agent, a CI bot, a cloud IDE) into an
openswarm lane queue. The agent polls issues, claims one, works on a branch, and opens a PR; the dispatcher's own lanes
review the PR and merge it. Python 3.9+ standard library only. Apache-2.0, same license as OpenSwarm.

- Library: `external_lane` (`ExternalLane`, `GitHub`, `contract`)
- CLI: `python3 -m external_lane ...` (or `external-lane ...` after `pip install`)
- Contract: [`external_lane/contract.py`](external_lane/contract.py). The dispatcher (`openswarm external tick`,
  `openswarm/bin/openswarm_external.py`) imports the same module, so labels and branch names are defined once.

## The contract (v1)

| Step | What the agent does |
|------|---------------------|
| Find | Open issues labelled **both** `lane-ready` and `backend:<agent>`. Best first: `p0` < `p1` < `p2`, then lowest number. |
| Claim | Replace `lane-ready` with `lane-claimed`, then comment exactly `claimed by <agent>`. Hold at most `max_inflight` claims (default 1). Re-read the issue first: skip it if it already has `lane-claimed`, `in-progress`, `lane-pr` or `lane-failed`. |
| Work | Branch from the latest `main` as `fleet/<agent>-issue<N>-<YYYYmmdd-HHMMSS>` (UTC). Run the issue's `验收：`/`Verify:` command before pushing. |
| PR | The PR body starts with `Closes #<N>`. The diff has **≥ 30 effective lines** of code or tests (`.py .ts .tsx .js .mjs .go .rs .sh`, `tests/`, `tools/`); docs-only or mostly-docs diffs are rejected. Then relabel the issue `lane-claimed` → `lane-pr`. |
| Rework | If the issue body has a line `重基PR：#<P> <branch>` (or `Rework PR: #<P> <branch>`), it is a fix-up for an existing PR: check out `<branch>`, apply the requested changes from the issue body, push to **the same branch**, and comment on PR #P. Do not open a new PR. |
| Fail | Relabel `lane-claimed` → `lane-failed` and comment the reason. |
| Never | Merge, approve, or review PRs; push to `main`; touch issues without your `backend:<agent>` label. |

The dispatcher side, for reference: it assigns cards to `backend:<agent>` (up to its own `max_inflight`), posts a
commit status `external-lane/contract` on every PR head (failure for a missing `Closes #N` or a thin diff, which blocks
the merge), runs a lane review, merges once it is approved and the checks pass, and puts a claim back to `lane-ready`
when it has no PR and no activity for `claim_timeout_s` (6h by default). After 2 such requeues, it marks the card
`lane-failed` + `needs-human`.

## Quickstart

```bash
git clone https://github.com/surlymochan/phanes.git && cd phanes/openswarm/sdk/external-lane
export GITHUB_TOKEN=...            # fine-grained token: this repo only; Issues RW, Pull requests RW, Contents RW
export EXTERNAL_LANE_REPO=surlymochan/phanes
export EXTERNAL_LANE_AGENT=chatgpt-agent   # your label is backend:chatgpt-agent

python3 -m external_lane status    # what you hold and what you could claim
python3 -m external_lane claim     # claim the best card (prints its number), or exit 1 if none / already at max_inflight
python3 -m external_lane branch 123          # the branch name to use for #123 (rework cards return the existing PR branch)
# ... clone, git checkout -b <branch> origin/main, edit, run the 验收 command, commit, git push origin <branch> ...
python3 -m external_lane submit 123 --branch <branch> --title "feat: ..." --dir /path/to/clone   # PR + lane-pr
python3 -m external_lane fail 123 --reason "acceptance fails: ..."                               # give up cleanly
```

If you can run a shell command that does the work (edits files and commits), one call handles the whole turn:

```bash
python3 -m external_lane run --dir ./lane-work --work-cmd './my-agent-step.sh' --loop --interval 300
```

`--work-cmd` runs inside the clone on the new branch and gets `ISSUE_NUMBER`, `ISSUE_TITLE`, `ISSUE_BODY_FILE`,
`LANE_BRANCH`, `LANE_REPO`, `LANE_AGENT`. It must commit. If it exits non-zero, makes no commit, fails the acceptance
command, or produces a thin diff, the SDK labels the issue `lane-failed` with the reason. Otherwise it pushes the branch
and opens the PR.

### Without Python (REST only)

The same steps as GitHub REST calls, for an agent that only has a GitHub connector:

1. `GET /repos/{repo}/issues?state=open&labels=lane-ready,backend:{agent}`, then pick the best one and `GET /repos/{repo}/issues/{n}` to re-check its labels.
2. `PUT /repos/{repo}/issues/{n}/labels` with the labels minus `lane-ready` plus `lane-claimed`, then `POST /repos/{repo}/issues/{n}/comments` `{"body": "claimed by {agent}"}`.
3. Push the branch `fleet/{agent}-issue{n}-{YYYYmmdd-HHMMSS}`, then `POST /repos/{repo}/pulls` `{"head": branch, "base": "main", "title": ..., "body": "Closes #{n}\n\n..."}`.
4. `PUT .../labels`: replace `lane-claimed` with `lane-pr`. If you give up, replace it with `lane-failed` and comment the reason.

## Rate limits

The client waits out `Retry-After` and `X-RateLimit-Reset` (when `X-RateLimit-Remaining: 0`), backs off exponentially on
429 and 5xx responses, and spaces write calls at least 1 s apart, as GitHub recommends for content-creating requests.
Keep `--interval` at 300 s or more. One poll costs 2–3 requests.

## Auth

`GITHUB_TOKEN` or `GH_TOKEN`. Without either, it falls back to a logged-in `gh` CLI. For `git`, the token is passed through
`GIT_CONFIG_*` environment variables, so it never shows up in argv and is never written to `.git/config`.

## Tests

```bash
python3 -m unittest discover -s tests -v   # no network: fake GitHub + a local bare git repository
```
