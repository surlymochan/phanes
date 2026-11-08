# 回放数据来源（2026-11-02 夜班，Refs #1696）

这三个文件是 2026-11-02 夜班合并积压的**离线快照**，供
`tools/tests/test_merge_backlog.py` 与 `tools/tests/test_merge_backlog_cli.py`
回放。取数时刻：`2026-11-02T21:05:00+08:00`（fleet 本地时区）。

## 三个文件各是什么

| 文件 | 真实来源 | 采集命令 |
| --- | --- | --- |
| `gh-lane-prs.json` | 仓库里当时还 open 的 PR | `gh pr list --state open --json number,title,url,labels,body,headRefName,headRefOid,isDraft,mergeable,reviewDecision,statusCheckRollup,createdAt,updatedAt` |
| `gh-lane-issues.json` | 带 `lane-pr` 标签的卡（issue） | `gh issue list --label lane-pr --state open --json number,title,state,labels,body,updatedAt` |
| `reviews.json` | 调度器的审查账本 | `~/.claude/openswarm-dispatch/reviews.json`（审查门禁写） |

## PR 与卡的关联：编号不能相同

同一个 GitHub 仓库里 **issue 和 PR 共用一个编号空间**，所以 PR #1632 的关联卡
**不是** #1632，而是 #1626。快照里每个 PR 都用真实的两条信号指向同一张卡：

- PR 正文 `Closes #N`（车道 PR 见 `tools/lane-autodispatch/autodispatch.py`，
  external lane 见 `openswarm/sdk/external-lane/external_lane/contract.py:pr_body`）；
- 分支名 `fleet/<lane>-issue<N>-<run>`。

测试 `FixtureShapeTests` 会把这两条信号和「PR 编号 ≠ 卡编号」一起断言。

| PR | 关联卡 | 场景 |
| --- | --- | --- |
| #1632 | #1626 | external lane 薄卡，等人工裁决（薄卡豁免） |
| #1633 | #1627 | 已批准且 CI 绿，等合并循环 |
| #1634 | #1628 | 还没人审，等首审 |
| #1635 | #1629 | 当晚已合并，不在清单里 |
| #1636 | #1630 | 卡还没进 `lane-pr`，不在清单里 |
| #1637 | #1631 | 没有 CI 读数也没有判决记录 → unknown |
| #1638 | #1625 | 批准后 CI 转红，等 CI |
| #1639 | #1624 | 批准 3 小时后 CI 才转绿 → 刚进「已批待合并」，不该被点名 |

## head SHA 是本仓库**已发布历史**里真实存在的提交

`headRefOid` 不是编出来的十六进制串，而是本仓库（`surlymochan/phanes`）里真实
的提交对象，可以用下面这两条命令逐条核对：

```bash
git cat-file -e c15cacc51d9b5d908ba5e35888f51ad3bcc5939b^{commit}   # PR #1632 的头
git merge-base --is-ancestor c15cacc51d9b5d908ba5e35888f51ad3bcc5939b HEAD
git show -s --format='%cI %s' c15cacc51d9b5d908ba5e35888f51ad3bcc5939b
```

| PR | head SHA | 提交时间 | 提交标题 |
| --- | --- | --- | --- |
| #1632 | `c15cacc51d9b5d908ba5e35888f51ad3bcc5939b` | 2026-11-01T05:47:08-05:00 | openswarm(agy): #1110 SSH fleet：未知 host 名不得计入 hosts_ok（r1716） (Refs #1110) (#1154) |
| #1633 | `4dd9cb694f40b8e31684275e27c173d86b39a04f` | 2026-11-01T19:59:52-05:00 | openswarm(codex): #1591 upstream(openswarm): `openswarm pause set\|clear\|list` CLI 接 lib/dispatch/pause.py 的 PauseStore（拆自 #1366 第 1 项） (#1612) |
| #1634 | `23b374715d0cfbbd428ab864ce5098ab30ee2e7e` | 2026-11-01T11:32:30-05:00 | openswarm(pi): #907 AD git_ssh_command：ProxyCommand 空格必须 shlex 引用否则 harvest 失败 (#935) |
| #1635 | `1c9b8d963ab80ef7a2463b622b19117a592ad7ca` | 2026-10-31T23:29:24-04:00 | openswarm(agy): #244 双模式：一份运维检查清单 checklist.md (#724) |
| #1636 | `ff48b1cd740c0b6dc3a9a00b6e39dfc183438d35` | 2026-11-01T00:26:12-04:00 | openswarm(agy): #774 Boss make_brief：期次锚点与 diagnosis periods 对齐 (#818) |
| #1637 | `91e4925150b71b88ed408627a4d91500ae45dc3d` | 2026-11-01T12:21:25-05:00 | openswarm(pi): #1372 upstream(openbot): phanes CI 漂移检查：openbot/ 与 openswarm/ 相对上游基线的改动必须登记 (#1376) |
| #1638 | `cb958519919e86d2f02e190a1258a818cb39be2f` | 2026-11-01T19:26:02-05:00 | openswarm(codex): #1495 upstream(openswarm): `openswarm doctor` 接入资源守卫（inode/块空间/cgroup 压力，拆自 #1392 第 1/3 项） (#1528) |
| #1639 | `240526deac476cfa16ba509641d8c3ebc96ec719` | 2026-11-01T23:18:48-05:00 | openswarm(pi): #1605 运维工具：车道预算超时报告——按主机/后端/卡类型统计 budget_exceeded 比例与耗时分布（tools/lane-budget-report.py） (#1627) |

车道分支的头（`refs/heads/openswarm/<run>`）当晚推在同一个仓库里，但**不在已发布
历史**里：clone 里留下的是同一变更**合并后**的提交。快照里的 `headRefOid` 用的就是
这些**已发布**的真实提交，所以每个 SHA 都能用上面的命令逐条核对，
`reviews.json` 里的 `sha` 与 PR 的 `headRefOid` 也一一对应（`FixtureShapeTests`
会断言这一点）。

### 为什么必须是「已发布历史」里的提交

CI 的 job 用 `actions/checkout` 的 `fetch-depth: 0` 拉仓库，它能看到的是**已发布
分支**带过来的提交；只存在于某一台机器上的提交（调度器的车道分支、AD 的本地
`rebase-pr-*` 辅助分支）在 CI 的 clone 里**不存在**，回放数据就核不出来。

上一版快照里有三个 SHA 取自这样的本地车道分支（`rebase-pr-1611`、
`rebase-pr-1647` 与一条 `openswarm/<run>` 分支），CI 的 `Swrm tests` 因此变红
（`FixtureShapeTests::test_the_head_shas_are_real_commits_of_this_repository`：
`a3a196e576… is not a commit of this repository`）。现在这三条换成同一变更的
**已发布**提交，并加了一条更强的断言：SHA 必须是 `HEAD` 的祖先，也就是**任何
一份历史的 clone**（含 CI）都能核出来。

## 账本里的时间

`reviews.json` 的时间是审查门禁写的**本地墙钟**（`%Y-%m-%d %H:%M:%S`，无时区），
所以回放时必须给时区（`--tz +08:00`）。

- `at`：判决时间（批准 / CHANGES）。
- `needs_human_at`：**`needs-human` 标签贴上去的时刻**，由审查门禁在贴标签时写进
  账本。#1632 的判决在 18:34:11，标签在 19:02:47 才贴上——等待时间从贴标签那一刻
  起算，不是从判决那一刻起算。

## 局限

快照是**离线重建**的：GitHub 侧那一夜的 `gh` 输出没有留在本机，所以这里按
上表的契约（正文 `Closes #N`、分支 `fleet/<lane>-issue<N>-<run>`、`lane-pr` 打在
卡上、检查项按 `Phanes components` 的 CheckRun 形状）重建。回放能证明的是**判定
与计时逻辑**对真实形状的输入成立；它不是 GitHub 上那一夜的字节级快照。
