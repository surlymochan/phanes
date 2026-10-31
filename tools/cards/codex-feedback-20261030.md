# 给 Codex 的修改意见（2026-10-30，来自 Claude 复盘）

> 针对线程 `01a188be-cebf-7c91-ab82-d41ee4c5df68` 的工作成果。只改下面列出的事项，不扩展新方向。

## 0. 总判断

- 功能链路基本跑通：5 种执行器、Hub 自动路由、跨主机并发、三 Bot 场景都有真实任务证据。
- **交付几乎为零**：openswarm #6–#39（31 个）和 openbot #58–#65（8 个）全是未合并的堆叠 PR，10/29 以来两个仓库的合并数都是 0。线上运行的是草稿分支代码。
- 用户两次要求"不要过度防御、不谈安全"，之后仍新增了不少防御性 PR（只读隔离、释放重试、ack 去重、uncertain 状态等）。**从现在起以"合入主线、正向流程可用"为唯一目标。**

---

## P0-1 PR 审查规则变更（用户已决定）

GitHub Codex code review 额度耗尽或迟迟没有响应时，**不再阻塞合并**：

1. PR 推送后请求 Codex review。
2. **等待 5 分钟**。如果仍没有 review 结果，或者返回"usage limits for code reviews"，就视为审查跳过。
3. 审查跳过时，只要**必需 CI 通过就直接合并**，并在 PR 评论里写一行：`Codex review skipped (quota/timeout), merged on green CI`。
   - "CI 也得绿"是 Claude 补充的条件；如果用户要求连 CI 也不等，就去掉这一条。
4. 如果 review 在合并后才回来，并且有 P0/P1 发现，就开一个后续 PR 修复，不回滚。
5. Codex 给出明确否决（有具体问题）时，仍按原规则修改后再合。

## P0-2 收敛并合并 PR 堆叠

- **以线上主机实际部署的代码为准**（gocc、gores1、本机），先核对部署版本和各分支 head 的差异，确认没有"已部署但没进任何分支"的改动。
- openswarm：把 #6–#39 按主题重组为 **3–4 个 PR**，直接基于 `main`，不要再链式堆叠：
  1. 执行器修复与模型配置（Pi/MiniMax、Codex gpt-6-luna high、Cursor composer-2.5 标准版、Copilot Haiku/Sonnet 5.5 medium、Antigravity）
  2. fleet 全局并发 + 自动路由 + Hub 桥接
  3. consult / review 工作树（独立工作树、检查后丢弃改动）
  4. Antigravity 处理（错误分类、重试、额度状态），见 P0-3
- 被替代的旧 PR 一律关闭，并注明"superseded by #xx"。
- openbot：把 #58–#63、#65 合成 1–2 个 PR。#64（mock 示例）单独修好 2 项 CI 失败后合并。较早的 #38、#43–#55 不在本轮范围，**不要动**。
- 按 P0-1 的规则合入后，**把所有主机重新部署为 main 的版本**，再记录 commit 哈希。
- 完成标准：openswarm 和 openbot 的相关改动都在 main 上；三台主机的运行版本 = main 的某个 commit。

## P0-3 Antigravity "额度耗尽"需要重新诊断

**事实：**

- 真实任务 `20261030-151154-12ecd4`（gocc A1，`gemini-3.8-flash-medium`）的原始报错是 `Individual quota reached. Please upgrade your subscription to increase your limits. Resets in 131h25m18s.`
- 用户在 Antigravity 设置页看到的是：Gemini 和 Claude/GPT 的周额度都剩 **98%**，5 小时额度剩 **100%**，**3 天 18 小时**后全部刷新。
- 两者的重置时间对不上（131h 约 5.5 天，设置页约 90h），用量也对不上。所以 gocc 上 agy 碰到的额度，**很可能不是用户看到的那个账号或那档订阅**。

**假设（待验证）：** gocc 上 agy 登录的是另一个 Google 账号；或者账号虽然相同，但 `loadCodeAssist` 返回 403，导致 CLI 没识别到付费档位，回落成了免费的 "Individual" 额度。这也能解释日志里 `loadCodeAssist` / `retrieveUserQuotaSummary` / `fetchAvailableModels` 一起 403，以及报错文案里的 "upgrade your subscription"。也就是说，**403 和"额度耗尽"可能是同一个根因。**

**要做的：**

1. 在 gocc 上确认 agy 当前登录的账号邮箱和订阅档位（只输出邮箱和档位，不输出 token），和用户截图对应的账号比对。
2. 如果账号不一致或者档位没被识别：重新登录到正确账号，或者修复档位识别，然后跑一次真实任务看是否恢复。
3. **去掉 lane 上硬写到 11-05 的 `quota_exhausted` 锁**。改成简单方案：被标记为额度耗尽后，每 1 小时做一次低成本探测，探测成功就恢复 idle。不要按上游倒计时一锁 5 天，也不要人工初始化状态。
4. 403 排查限时（time-box）：如果第 1–2 步解决了，就关闭 [openswarm#5](https://github.com/surlymochan/openswarm/issues/5) 里的 403 部分；如果没解决，保留"重试一次 + consult 跳过 Antigravity"，记一行结论后停止深挖。

## P1-1 名额崩溃后的回收：改用 lease TTL

- 当前做法是"父进程崩溃、子进程状态不明时保留名额"，这会造成名额泄漏。
- 改为：预约带 lease（例如 TTL 10 分钟），运行中的 runner 定期续约，超时没续约就自动释放。不再增加 uncertain 之类的状态机分支。

## P1-2 远端提交自动交回本机审查（[openswarm#40](https://github.com/surlymochan/openswarm/issues/40)）

- 任务完成后，结果分支自动 push 到本机可 fetch 的远端（或者由 Hub 自动 fetch），去掉手动同步 bundle 这一步。
- 用一个真实 Hub 派单验收：远端执行完成后，本机 Hub 无需人工介入就能看到 diff 并审查。

## P1-3 重写 HANDOFF.md

- 现在有 1213 行，其中 883 行未提交，本地还落后 origin 一个提交。先 `git pull --rebase`。
- 新结构控制在 **≤150 行**：
  1. 当前运行拓扑（主机、执行器、模型、版本 commit）
  2. 已通过 / 未验证 / 失败三栏表
  3. 未完成项（关联 issue）
  4. 回滚点
  5. 用户规则
- 旧的流水账整体移到 `docs/handoff-archive-20261030.md`。
- 文风：先写结论，不要每条都附"不能证明……"。不确定的地方统一放在"未验证"一栏。
- 提交并推送 phanes。

## P2

- **本机整机重启**：写成运行约定——FileVault 开启时，重启后需要人工登录一次；只验收"登录后 Hub 和执行器自动恢复，并能完成一次 Hub 界面派单"。
- **清理** `~/workspace/openswarm-*`、`~/workspace/openbot-*` 下约 40 个临时工作树。清理前确认改动已经进入 P0-2 的 PR。
- b1–b5 不在本轮范围，在 issue 里标注"未接入"即可。

---

## 工作约束（重申）

- 不新增防御性门禁、只读沙箱或安全加固类 PR；保留已上线并且必要的保护（独立工作树、凭据不进日志、失败释放名额）。
- PR 要小，并且**必须合入才算完成**；同一时间开着的本轮 PR 不超过 4 个。
- 每完成一个 P0/P1 项：先合并、再部署，然后在关联 issue 里记录一行证据（任务 ID 和 commit）。
- 汇报只写三件事：已合并的 PR、部署版本、还剩什么。

## 本轮验收清单

- [ ] P0-1 规则已生效：至少 1 个 PR 走了"5 分钟无审查 → CI 绿合并"的流程
- [ ] P0-2 openswarm/openbot 相关改动全部进入 main，旧堆叠 PR 已关闭，三台主机版本 = main
- [ ] P0-3 Antigravity 账号和档位已核对，lane 不再硬锁到 11-05，有一次真实任务结果（成功，或明确的失败原因）
- [ ] P1-1 lease TTL 已合并，并用"杀掉 runner 父进程 → 名额自动回收"实测
- [ ] P1-2 #40 已合并，有一次真实 Hub 派单自动交回审查的记录
- [ ] P1-3 HANDOFF ≤150 行，已推送
