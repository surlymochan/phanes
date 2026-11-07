# 回放数据来源（2026-11-02 夜班，Refs #1696）

这三个文件是 2026-11-02 夜班合并积压的**离线快照**，供
`openswarm/tests/test_merge_backlog.py` 与 `tools/tests/test_merge_backlog.py`
回放。取数时刻：`2026-11-02T21:05:00+08:00`（fleet 本地时区）。

## 三个文件各是什么

| 文件 | 真实来源 | 采集命令 |
| --- | --- | --- |
| `gh-lane-prs.json` | 仓库里当时还 open 的 PR | `gh pr list --state open --json number,title,url,labels,body,headRefName,headRefOid,isDraft,mergeable,reviewDecision,statusCheckRollup,createdAt,updatedAt` |
| `gh-lane-issues.json` | 带 `lane-pr` 标签的卡（issue） | `gh issue list --label lane-pr --state open --json number,title,state,labels,body,updatedAt` |
| `reviews.json` | 调度器的审查账本 | `~/.claude/openswarm-dispatch/reviews.json`（`openswarm/lib/dispatch/engine.py` 写） |

## PR 与卡的关联：编号不能相同

同一个 GitHub 仓库里 **issue 和 PR 共用一个编号空间**，所以 PR #1632 的关联卡
**不是** #1632，而是 #1626。快照里每个 PR 都用真实的两条信号指向同一张卡：

- PR 正文 `Closes #N`（车道 PR 见 `tools/lane-autodispatch/autodispatch.py:625`，
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

## head SHA 是本仓库里真实存在的提交

`headRefOid` 不是编出来的十六进制串，而是本仓库（`surlymochan/phanes`）里真实
的提交对象——车道分支 `refs/heads/openswarm/<run>` 就推在同一个仓库里
（`tools/lane-autodispatch/autodispatch.py:618`）。可以用下面两条命令逐条核对：

```bash
git cat-file -e 2c4543ecaac779fb29499807c89c8a704a846e25   # PR #1632 的头
git show -s --format='%cI %s' 2c4543ecaac779fb29499807c89c8a704a846e25
```

| PR | head SHA | 提交时间 | 提交标题 |
| --- | --- | --- | --- |
| #1632 | `2c4543ecaac779fb29499807c89c8a704a846e25` | 2026-11-01T18:13:50+08:00 | openswarm(agy): #1110 SSH fleet：未知 host 名不得计入 hosts_ok |
| #1633 | `a3a196e576cb482a3dec163b779d1bbc3423ac33` | 2026-11-02T14:50:18+08:00 | openswarm(pi): #1592 dispatch 引擎：paused_lane 同时读 PauseStore |
| #1634 | `19bae1bccd13bf14c2a4e08e9d7a042dfad2e5ce` | 2026-11-01T14:52:38+08:00 | openswarm(agy): #907 AD git_ssh_command：ProxyCommand 空格必须 shlex 引用 |
| #1635 | `67deb417b8b051a4bbd4588e577681187f9a8b5c` | 2026-11-01T10:45:43+08:00 | openswarm(agy): #244 双模式：一份运维检查清单 checklist.md |
| #1636 | `53d07a90a475cb961c9999810054de51d91782f3` | 2026-11-01T12:22:13+08:00 | openswarm(agy): #774 Boss make_brief：期次锚点与 diagnosis periods 对齐 |
| #1637 | `c47d3b1c325177b2dc57744af8b252a4fc1ca703` | 2026-11-02T15:33:20+08:00 | openswarm(agy): #1373 upstream(openbot)：org-health-local 目录搬迁 |
| #1638 | `15e28d7e9dcbf35f129fe817a933894275a1daaf` | 2026-11-02T15:57:51+08:00 | openswarm(pi): #1392 通用 inode 守卫 |
| #1639 | `14194fd421702060fe5a2b17fe3359043d16b339` | 2026-11-02T12:17:09+08:00 | openswarm(pi): #1627 Review PR #1627: Add lane budget timeout report |

## 账本里的时间

`reviews.json` 的时间是调度器写的**本地墙钟**（`%Y-%m-%d %H:%M:%S`，无时区），
所以回放时必须给时区（`--tz +08:00`）。

- `at`：判决时间（批准 / CHANGES）。
- `needs_human_at`：**`needs-human` 标签贴上去的时刻**，由调度器在贴标签时写进
  账本（`openswarm/lib/dispatch/engine.py`）。#1632 的判决在 18:34:11，标签在
  19:02:47 才贴上——等待时间从贴标签那一刻起算，不是从判决那一刻起算。

## 局限

快照是**离线重建**的：GitHub 侧那一夜的 `gh` 输出没有留在本机，所以这里按
上表的契约（正文 `Closes #N`、分支 `fleet/<lane>-issue<N>-<run>`、`lane-pr` 打在
卡上、检查项按 `Phanes components` 的 CheckRun 形状）重建，head SHA 用本仓库真实
的提交代替。回放能证明的是**判定与计时逻辑**对真实形状的输入成立；它不是
GitHub 上那一夜的字节级快照。
