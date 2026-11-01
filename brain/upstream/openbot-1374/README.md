# openbot 回流：3 个上游 PR 的补丁（Refs #1374）

用户 2026-11-02 00:41（UTC+8）同意签 nightly-labs CLA，把 phanes/openbot 里的通用改动拆成 3 个小 PR 提给
nightly-labs/openbot。这里存的是准备推到 surlymochan/openbot、再对 nightly-labs/openbot main 开 PR 的补丁，
基线都是 nightly-labs/openbot main `7ef25fbc`（2026-11-01 23:35 UTC+8，v0.58.0）。本 PR 只是为了走 lane 审查并留档，
不改 phanes 代码。

| 补丁 | 上游分支 | 内容 | 来源 |
|---|---|---|---|
| 1-fix-dev-lan-opt-in.patch | fix/dev-lan-opt-in | `bun run dev` 默认只绑 127.0.0.1，`--lan` 才绑 0.0.0.0；`--lan` 与 `AUTH_EXPOSE_DEVELOPMENT_CODE=true` 互斥（命令行 + 绑定处两道）；LAN 栈跳过一次性开发账号；smoke `scripts/dev-lan-guard-smoke.ts` | fork d5e1c402 + 17e97f22 + phanes #11/#48 增量。上游 main 现在 app/test-client 栈默认绑 0.0.0.0 且开发 Auth API 会在响应里返回登录码 |
| 2-fix-dev-checks-in-subdirectory.patch | fix/dev-checks-in-subdirectory | OpenBot 作为大仓库子目录（如 phanes/openbot）或 linked worktree 时，pre-commit / staged-typecheck / dev-verify / prepare-dev-environment / check-release-notes 用项目相对路径；hook 按自身位置找项目根（不再写死 `openbot/`）；e2e `scripts/pre-commit-subdirectory-e2e.sh` 4 种布局，旧 hook 会失败 | phanes #70 等 |
| 3-fix-auth-migration-number-check.patch | fix/auth-migration-number-check | `apps/auth-api/scripts/migration-numbers.ts` 拒绝重复迁移号和非 `NNNN_name.sql` 命名，已上线的 0034 两个文件按名字放行；smoke `scripts/auth-migration-numbers-smoke.ts` | phanes 的 Python 版 check_migration_numbers.py 改写成 TS（上游无 Python，测试规范要求 scripts/*-smoke.ts 写 .openbot-build 报告） |

去重：上游 open PR #3786（usage reading reset）、#3686（Gemini 验证提示）不碰这些文件；已合入的 #2576–#2578 不涉及；
上游 main 里没有 `--lan`、迁移号检查或子目录支持。刻意不带：CI 改动（上游要求 approved issue）、remote/api 生产默认 host、
org-health 等 phanes 专用内容、docs 漂移修正（留到后续）。

本地验证（box，最小依赖，未跑全量 typecheck，留给上游 CI）：
- `bun scripts/dev-lan-guard-smoke.ts` 全过；`bun scripts/dev-services.ts app --lan --dry-run` 在开发码开启时拒绝。
- `bash scripts/pre-commit-subdirectory-e2e.sh` PASS（4 种布局）；换回上游旧 hook 时 FAIL。
- `bun scripts/auth-migration-numbers-smoke.ts` 全过；临时加一个 0046 重复文件时 CLI 退出 1。
- 各补丁改动文件 `biome check` 0 问题，hook 与 e2e `shellcheck` 0 问题。

审查要点：补丁是否正确、是否只含通用改动（无 phanes 路径/密钥/主机名）、是否会让上游现有行为回归、
每个补丁是否自洽可单独合入。
