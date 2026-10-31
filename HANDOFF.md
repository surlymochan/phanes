# OpenBot / OpenSwarm 交接

最后更新：2026-10-30。运行与验收状态以本文件和关联 Issue 的最新证据为准。

## 运行拓扑

| 执行器 | 模型 | 主机 |
|---|---|---|
| Codex | gpt-6-luna，high | gocc |
| Antigravity | gemini-3.8-flash-medium | gocc |
| GitHub Copilot 常规 | claude-haiku-5.5，medium | gocc |
| GitHub Copilot 较难 | claude-sonnet-5.5，medium | gocc |
| Pi + MiniMax | MiniMax-M3.1-Flash | gores1 |
| Cursor | composer-2.5，非 fast | gores1 |
| OpenBot Hub | 本机运行 | local |

OpenSwarm main 最新已合入执行器支持提交 `30589a980ded38e2607d6d1febbc1c5c2942e577`；三主机尚未统一部署到该提交。OpenBot main 已合入组织健康示例 `afcc320667907bdda4b57ac876cc90885b1ffc7a`；Hub 准入构建尚未合入并部署。

## 验收状态

| 已通过 | 未验证 | 失败 / 待修 |
|---|---|---|
| Antigravity 正确账号额度查询及真实任务；执行器服务重启后任务 | 统一部署后的五类执行器经本机 Hub 自动派单与回传 | 主机崩溃后名额自动回收，见 [OpenSwarm #5](https://github.com/surlymochan/openswarm/issues/5) |
| Cursor 标准版、Codex high、Copilot 两档和 Pi 的现有主机配置 | 三主机和本机 Hub 登录后的整机重启恢复 | 远端结果自动交回本机审查，见 [OpenSwarm #40](https://github.com/surlymochan/openswarm/issues/40) |
| OpenBot 组织健康示例已合入 | Boss、DA、Builder 完整协作及最终审查 | OpenBot main 的队列和备用模型 E2E，见 [OpenBot #66](https://github.com/nibiru-labs/openbot/issues/66) |

## 未完成

- 合入 [OpenSwarm #43](https://github.com/surlymochan/openswarm/pull/43)、[#44](https://github.com/surlymochan/openswarm/pull/44)，并把线上功能统一部署为同一个 main commit。
- 合入 [OpenBot #67](https://github.com/nibiru-labs/openbot/pull/67)，将本机 Hub 切到该构建。
- 运行五类执行器真实任务，记录任务 ID、执行主机、模型、运行时预约和释放结果。
- 实现并实测失联 runner 名额回收，以及远端结果自动交回。
- 完成组织健康场景、审查和重启后复验；将结果追加到本文件。

## 回滚点

部署时保留各主机旧版本与配置；在验证新版本后记录确切回滚版本和命令。

## 用户规则

- 功能全部跑通前，不新增安全类工作。
- 任务使用独立工作树；审查产生的修改检查后丢弃，不自动提交或合入主线。
- 正式任务经全局并发限制；结束后释放名额。
- PR 必须关联 Issue；已知功能问题修复后复审。Codex 审查额度耗尽或等待五分钟无响应时，可按用户确认的 CI 规则合并。
- 每五轮使用 ccargue 复盘；有分歧时，除非有充分证据，否则优先采纳 Claude Code 结论。
- FileVault 或系统登录要求人工登录时，以登录后自动恢复为验收边界。
