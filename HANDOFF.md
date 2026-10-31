# Phanes 交接

最后更新：2026-10-31。已合入代码、部署和验收分别记录；未完成事项以关联 Issue 为准。

## 当前拓扑

| 角色 / 执行器 | 模型配置 | 主机 |
|---|---|---|
| OpenBot Hub | 本机 Hub | local |
| Codex | gpt-6-luna，high | gocc |
| Antigravity | gemini-3.8-flash-medium | gocc |
| GitHub Copilot 常规 / 较难 | claude-haiku-5.5 / claude-sonnet-5.5，medium | gocc |
| Pi + MiniMax | MiniMax-M3.1-Flash | gores1 |
| Cursor | composer-2.5，非 fast | gores1 |

以上是现有主机配置；统一部署后的配置和模型仍待逐机验证。

OpenBot 来源 main：`2f02a65b1bebebae8640ddf971f1719f33139f53`；OpenSwarm 来源 main：`6d5c3c555c2b2275924dacf9dd35c5e3c770b58a`。两者和连接入口正在 [Phanes #14](https://github.com/surlymochan/phanes/pull/14) 整合，尚未部署为统一版本。

## 验收状态

| 已通过 | 未验证 | 失败 / 待修 |
|---|---|---|
| Antigravity 正确账号额度及真实任务；服务重启后的任务 | 五类执行器经本机 Hub 自动路由的真实任务与结果回传 | 父进程异常退出后的名额自动回收尚未实现，[#3](https://github.com/surlymochan/phanes/issues/3) |
| Phanes 目录中的 OpenSwarm 53 项针对性测试、OpenBot 类型和语言检查、Pi/Codex 准入测试 | 三台主机运行同一个 Phanes 提交、全局并发观察及任务后预约归零；dev 脚本 LAN 守卫（`--lan` 与 `AUTH_EXPOSE_DEVELOPMENT_CODE` 互斥）在本工作树未运行验证，[#11](https://github.com/surlymochan/phanes/issues/11) | OpenBot 主线队列和备用模型 E2E 失败，[#7](https://github.com/surlymochan/phanes/issues/7) |
| 源码导入时，两组件目录树分别与来源提交一致 | 远端结果自动交回、三 Bot 场景及主机重启恢复 | Phanes 完整构建、CI 与正式部署尚未完成 |

## 未完成

- 完成 [#14](https://github.com/surlymochan/phanes/pull/14) 的 CI、审查和合入；完成 [统一项目入口](https://github.com/surlymochan/phanes/issues/1)。
- 实现带续约和进程确认的故障名额回收；实测 kill -9 和释放后的再调度，见 [#3](https://github.com/surlymochan/phanes/issues/3)。
- 从同一 Phanes 提交构建和部署，逐主机记录版本、模型、任务 ID、路由选择、预约和回收结果。
- 完成 [远端结果交回](https://github.com/surlymochan/phanes/issues/2)、[本机 Hub](https://github.com/surlymochan/phanes/issues/5)、[三 Bot 场景](https://github.com/surlymochan/phanes/issues/6)及服务、整机重启后的验收。
- Phanes Discussions 已启用；现有私有 Phanes Project 收纳本仓库事项。跨所有者 Issue 已复制描述与讨论并保留原链接；历史 PR 保留原记录。

## 回滚点

本轮尚未统一部署；部署时保留各主机原版本和配置，记录确切回滚点。

## 用户规则

- 全部功能跑通前，不新增安全类工作；已有保护不为迁移而擅自移除。
- 任务使用独立工作树；审查修改检查后丢弃，不自动提交或合入主线。此方案不是系统级只读沙箱。
- 正式任务经过全局并发限制；结束或确认停止后释放名额。
- PR 关联 Issue；明确的功能问题修复后复审。Codex 审查额度耗尽或等待五分钟无响应时，按用户确认的 CI 规则处理。
- 每五轮使用 ccargue 复盘；有分歧时，除非有充分证据，否则优先采纳 Claude Code 结论。
- FileVault 或系统登录要求人工登录时，记录实际边界，不为无人值守泄露凭据。
