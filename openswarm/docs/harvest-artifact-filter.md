# Harvest artifact filter (Refs #1835)

A lane branch can carry artifacts that must never reach a PR: the hidden build
leftovers of the test suite (`openswarm/tests/.dde4*-00000000.so`, ~4.4 MB each) and
the review products (`.lane-review/PR-N.md`). The dispatcher rebuilds the branch
without those paths **before** it pushes it, so a PR only holds the real change.

## What runs where

| Where | What |
|---|---|
| `lib/dispatch/harvest_filter.py` | the filter (the rules, the rebuild, the emptiness check) |
| `lib/dispatch/engine.py` (`Dispatcher.finish`) | the harvest of a fix/rebase run and of a new PR: the filter runs before the push |
| `tools/lane-autodispatch/autodispatch.py` | the repo mirror of the box runtime (`~/phanes-ctl/bin/autodispatch.py`): the same filter, deployed separately |

The two copies of the rules are the same list; the deployed runtime is synced from
the mirror (`tools/autodispatch-backend-fallback.md`), so a rule change must land in
both.

## The rules

`HARVEST_EXCLUDE_PATTERNS` is a list constant: one more artifact kind is one more
pattern.

```python
HARVEST_EXCLUDE_PATTERNS = [
    HIDDEN_SHARED_OBJECT,                   # hidden shared objects (build leftovers)
    re.compile(r"(^|/)\.lane-review/"),      # the review products of the lane
]
```

The dispatcher config may replace the list with `harvest_exclude` (a list of regular
expressions); the default (`null`) keeps the constant.

## The behaviour

- The filter rebuilds the fetched lane branch (`refs/fleet/<lane>`) commit by commit
  with the excluded paths restored to the base, and the dispatcher pushes the rebuilt
  head instead of the raw branch.
- **The emptiness is a tree comparison**, not a head comparison: a commit that adds a
  normal file and an artifact, followed by a commit that deletes the normal file
  again, leaves the tree of the base although the head differs. Such a branch holds
  no effective change and is not pushed; the card ends as `lane-failed`.
- **The filter fails closed.** When a git command fails (or the branch holds a merge
  commit, which cannot be rebuilt), nothing is pushed, the cause is recorded in the
  run record (`harvest_failure_reason`) and the harvest retries on the next tick.
- The excluded paths are written as one line of the brain log:
  `<lane> #<n> run <run> harvest excluded: [...]`.

## The artifacts in the repository (Refs #1773)

The follow-up cards of the lane reviews (#1745, #1753, #1763, #1773) named the
hidden shared objects of `openswarm/tests/`. The verification of the four files
that `main` still tracked:

- They were **the same file four times**: one blob (`35a1bdb`, 4,446,904 bytes) under
  four hashed names (`.dde4fddfefb6d431-00000000.so` and its siblings).
- They are **not a library and not a build product of the repository**: an ELF x86-64
  static-pie executable (the `.so` suffix is a lie) that Zig 0.15.2 built for the
  `uucode-0.1.0` package of the `opentui` project on a GitHub Actions runner
  (`/Users/runner/work/opentui/opentui/...`). It is the native TUI library the agent
  runtime unpacks into the working directory, named after a temporary hash.
- **Nothing in the repository uses them**: no source file, test, fixture or tool
  reads, loads or builds them. The mentions of the name pattern are the rules of this
  filter, the docs and the tests of the filter.

So they were build leftovers of the lane tooling, not part of any feature: they are
removed from the tracking, and three guards keep them out.

| Guard | What it does |
|---|---|
| `openswarm/.gitignore` (`*.so`) and the root `.gitignore` (`openswarm/tests/.*.so`) | a rebuilt artifact stays untracked, so `git add -A` cannot sweep it in |
| the filter of this document | an artifact that a lane branch carries is dropped before the push |
| `test_the_repository_tracks_no_hidden_shared_object` (`openswarm/tests/test_dispatch_harvest_filter.py`) | the repository itself must not track one: the test fails when it does |

`lib/dispatch/harvest_filter.py` exposes `tracked_artifacts(repo)`: the artifacts
among the files the repository tracks, with the same rules the harvest uses.

## The proof

### 上轮审查逐项交付映射

| 问题 | 修复 | 对应测试 |
|---|---|---|
| B1：fix/rebase 实际收割路径未过滤 | `openswarm/lib/dispatch/engine.py` 的 `Dispatcher.finish` 推送前过滤，过滤失败时记录原因且不推送，保留租约保护。 | `test_the_fix_harvest_pushes_the_branch_without_the_artifacts`、`test_a_failing_filter_pushes_nothing_and_records_the_cause`、`test_push_always_uses_lease`（`openswarm/tests/test_dispatch_engine.py`） |
| B2：多提交抵消后仍推送空净改动 | `openswarm/lib/dispatch/harvest_filter.py` 比较过滤后的最终树与基线树；仓内 AD 镜像采用相同判断。 | `test_commits_that_cancel_each_other_leave_nothing_to_push`（`openswarm/tests/test_dispatch_engine.py`）、`test_commits_that_cancel_each_other`（`tools/lane-autodispatch/test_harvest_filter.py`） |
| B3：缺少收割终态和 fix/rebase 推送集成证据 | 增加 `Dispatcher.finish` 及 AD 收割测试，覆盖仅越界产物时不推送和空结果终态，以及过滤后推送。 | `test_a_branch_with_only_artifacts_is_not_pushed`、`test_the_fix_harvest_pushes_the_branch_without_the_artifacts`（`openswarm/tests/test_dispatch_engine.py`）、`test_finish_with_only_artifacts_pushes_nothing`（`tools/lane-autodispatch/test_harvest_filter.py`） |

本工作树执行回放的实际输出如下；本地缺少对应 ref，因此尚不能核验命中，需在具备这两个历史 ref 的仓库重新运行：

```text
1768: not a local ref (unknown ref)
1677: not a local ref (unknown ref)
2 excluded path(s) in total
```

末行是脚本汇总计数，不代表上述缺失的 ref 已命中过滤规则。

```sh
python3 -m unittest tests.test_dispatch_harvest_filter -v   # the filter itself
python3 -m unittest tests.test_dispatch_engine -v            # the harvest integration
python3 tools/check-harvest-filter.py 1768 1677              # replay the local PR heads
```

`tools/check-harvest-filter.py` reports what the filter would drop on a local head
(`rebase-pr-<n>`) over the merge base with `main`; it only reads, so a head can be
replayed offline.
