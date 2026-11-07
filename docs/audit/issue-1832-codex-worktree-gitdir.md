# Issue #1832 失败台账：CX10（govpn）连续无提交速败 — codex 沙箱写不了 worktree gitdir

- 卡片：`#1832 [ops] CX10（govpn）连续无提交速败根因：codex 沙箱写不了 worktree gitdir（wt1 只读）`
- 重基 PR：`#1856`（`fleet/M19-issue1832-20261103-222842-5ff1e2`）
- 本轮基线：`main` = `4dc2f2c930`（“Rename openbot to volto inside phanes (#1895)”）
- 上一 PR 头：`9a03be7b11`（上一轮已按 swrm 布局重基到 `2d5ec1ae60` 的版本）
- 本次记录日期：2026-11-08（执行器沙箱内；无外网、无 govpn 访问）
- 本轮动作：把 PR 头 `9a03be7b11` 重基到 `main` = `4dc2f2c930`（冲突只有行号偏移，无语义冲突），
  并定位、修复 PR 头 CI 变红的根因（见第 5.2 节）

## 1. 现场事实（来自 #1832 卡面，govpn 2026-11-03 16:2x 实读）

- boss 16:19 报 CX10 25 分钟内连续 5 次无提交速败；AD 日志 16:00–16:17：`#1668`、`#1716`、`#1790`、`#1794` 均为
  `CX10 ended … without a commit (partial/None)`。
- run `20261103-161536-bd638c`（#1794 / PR #1805 重基）`result.json`：`cherry-pick -n` 因无法写 index 失败，
  `Read-only file system`；index 位于 `/root/work/phanes-fleet/.git/worktrees/wt1`。
- run `20261103-161342-996121`（#1790 / PR #1800）：审查员/执行员未能在当前工作区拿到 PR 头分支，无提交
  （部分原因是任务本身，部分原因同上）。
- govpn 两条车道的 worktree 目录同名 `wt`（`lanes/CX5/wt`、`lanes/CX10/wt`），git 管理目录因重名被追加序号：
  CX5 → `.git/worktrees/wt`，CX10 → `.git/worktrees/wt1`。
- govpn host 层 `wt1` 目录可写（已核）→ 失败发生在**沙箱层**，不是 host 权限。

## 2. 假设的判定：部分证实、部分推翻

卡面假设：“codex 沙箱的可写白名单按 worktree 目录名推算 gitdir（`.git/worktrees/wt`），CX5 恰好匹配，CX10 的
`wt1` 落在白名单外”。

| 判定项 | 结论 | 依据 |
|---|---|---|
| Codex `workspace-write` 沙箱把 worktree 的 git 元数据只读挂载 | **证实** | 现场 `Read-only file system`（index 写入）；`swrm/bin/swrm:1709` `codex_gitdir_writable_roots` 的实现前提 |
| 只有该 worktree 的**确切** gitdir + common 的 `objects`/`refs`/`logs` 才是可写根 | **证实** | 同上函数；`swrm/tests/test_codex_sandbox_roots.py:59` 固化 |
| 运行时按 worktree **目录名**推算 gitdir（`wt` / `wt1`） | **推翻** | 可写根来自 `git rev-parse --git-dir --git-common-dir`，取的是**真实** gitdir；`swrm/tests/test_lane_worktree_gitdir.py:63` 用两条同名 `wt` 的车道断言根跟随真实 gitdir |
| `wt1` 本身是问题 | **推翻** | 真实 gitdir 即 `wt1`，运行时能取到；问题是 **govpn 当时部署的发行版不带这段实现**，且 `~/.codex/config.toml` 里钉死的 `writable_roots` 只覆盖了 `.git/worktrees/wt`（CX5 那条），CX10 的 `wt1` 落在白名单外 |
| CX5 不暴露问题是因为它只 review 不 commit | **成立** | review 车道不写 index，写失败不发生；写失败只在下一次 commit 时暴露 |

### 2.1 重基到 main 后发现的第二个根因（本次交付必须补回的部分）

`#1894`（`2d5ec1ae60`）把 vendored openswarm 换成 swrm 单一来源时，把 `#1346`（`bb8d5d9015`，upstream
`codex_gitdir_writable_roots`）整体丢掉了：main 的 `swrm/bin/swrm` 里 **没有任何** `writable_roots` 逻辑
（`git grep -n "writable" 2d5ec1ae60 -- swrm/bin/swrm` → 0 命中）。

即：若按 main 直接部署新的运行时，**所有** codex 车道都会退回“index 只读”，CX10 只是最先暴露的那条。
因此本次交付把 `#1346` 的实现按 swrm 布局补回，并叠加本卡的修复（唯一 admin dir + app-server 启动即带根）。

## 3. 交付内容（本次改动）

| 层 | 文件 | 内容 |
|---|---|---|
| 运行时 | `swrm/bin/swrm:1709-1812` | `codex_gitdir_writable_roots` / `codex_config_writable_roots` / `codex_writable_roots` / `codex_writable_roots_flags` / `codex_app_server_command`：可写根 = `~/.codex/config.toml` 已配根 ∪ 该 worktree 的真实 gitdir + common 的 `objects`/`refs`/`logs` + worktree 根 |
| 运行时 | `swrm/bin/swrm:7026-7098` | `lane_git_common_dir` / `lane_gitdir_admin_name` / `lane_align_worktree_admin_dir`：每条车道独占 `.git/worktrees/wt-<lane>`，`git worktree repair` 重新挂接；老车道从 `wt1` 迁到 `wt-CX10` |
| 运行时接线 | `swrm/bin/swrm:4466`（`codex exec`）、`:7175`（`codex app-server` 启动）、`:7655`（`thread/start`）、`:7157` 与 `:8486`（admin dir 对齐） | 四个入口都带可写根 |
| 策略 | `swrm/policy.json`（`codex.worktree_gitdir_writable: true`）、`swrm/backends/codex.json`、`swrm/README.md` | 开关与文档；`policy.local.json` 设 `false` 可关 |
| 自查工具 | `tools/check-codex-sandbox-gitdir.py` | 按车道读**真实** gitdir；用**已部署运行时**算可写根；报告 `~/.codex/config.toml` 钉死的根、admin dir 是否共享、host 层是否可写；缺 worktree 的车道报 `missing-worktree` 而不是抛 `AttributeError` |
| 回归测试 | `swrm/tests/test_codex_sandbox_commit.py`、`swrm/tests/test_lane_worktree_gitdir.py`、`swrm/tests/test_codex_sandbox_roots.py`、`swrm/tests/test_real_codex_sandbox.py`、`tools/tests/test_check_codex_sandbox_gitdir.py` | 见第 4 节映射 |

## 4. 复审 BLOCKER 逐条映射（B 编号 → 改动文件:行 → 测试 → 结果）

| B 编号 | 要求 | 改动文件:行 | 对应测试 | 结果 |
|---|---|---|---|---|
| **B1** | 真实 codex 沙箱内同机两车道各自提交 | `swrm/bin/swrm:1798`（`codex_app_server_command`，app-server 启动即带根）、`swrm/bin/swrm:7655`（`thread/start` 带根）、`swrm/bin/swrm:7048`（每车道独占 admin dir）、夹具 `swrm/tests/fixtures/fake_sandbox_app_server.py`（按 Codex 挂载规则裁决：根外的 git 写入 = `Read-only file system`） | `swrm/tests/test_codex_sandbox_commit.py:121` `test_two_lanes_each_commit_in_their_own_sandbox`（两条同名 `wt` 车道各自产出提交）、`:136` `test_lane_without_its_gitdir_root_cannot_commit`（对照：无根即 `Read-only file system`）、`:148` `test_app_server_is_launched_with_the_lane_admin_dir`、`:159` `test_thread_start_carries_the_lane_admin_dir`；真实 CLI 版 `swrm/tests/test_real_codex_sandbox.py:133`/`:143`（`OPENSWARM_REAL_CODEX=1`，本执行器沙箱无 codex，按跳过处理） | 离线版 4 项全通过；反向验证（临时去掉运行时接线）→ 车道提交失败并报 `Read-only file system` |
| **B2** | 自查工具要能检查实际车道，并给出 govpn 现场结论 | `tools/check-codex-sandbox-gitdir.py`（整文件重写：`lane_worktrees()` 按 `lanes.json` + `<home>/lanes/<name>/` + `state.json` 取车道工作树；`load_runtime()` 载入**已部署运行时**算根；`host_can_write()` 报告 host 层可写） | `tools/tests/test_check_codex_sandbox_gitdir.py:62`/`:74`/`:84`/`:94` | 4 项全通过；**govpn 现场核实仍未闭环**（见第 6 节：执行器沙箱无 DNS/网络，无法访问 govpn/govpn2/gores2） |
| **B3** | 恢复并更新失败台账，保留此前调查与未闭环信息 | 本文件 `docs/audit/issue-1832-codex-worktree-gitdir.md`（第 1 节保留原始事实，第 2 节给出假设判定，第 5/6 节列出未闭环项与现场待办） | 文档本身（人工核对第 1/2/6 节与 `git show bb8d5d9015`、`git show 2d5ec1ae60` 的可核对结论） | 已恢复；台账同时记录了本轮新发现的第二个根因（`#1894` 丢掉 `#1346` 实现） |
| **B4** | 交付说明逐条映射 B1–B3 → 改动 → 测试 | 本文件第 4 节（本表）+ `swrm/tests/test_codex_sandbox_commit.py` 文件头（说明夹具如何模拟 Codex 挂载规则） | 第 4 节表格 | 已给出 B1–B4 四条映射 |

## 5. 验收状态

- **运行时修复**：已交付（`swrm/bin/swrm`），并有离线集成回归覆盖（两条同名 `wt` 车道各自提交）。
- **双车道真实沙箱提交（tester / govpn 现场）**：**未闭环**。执行器沙箱内无外网（`getent hosts govpn` → 无 DNS）、
  无 `codex` 二进制，无法在本机跑真实沙箱；命令见第 6 节。
- **根因结论**：已写入第 2 节（假设部分推翻：`wt1` 不是问题本身；问题是部署发行版缺实现 + `~/.codex/config.toml`
  钉死的根只覆盖一条车道；重基到 main 后又发现 `#1894` 丢实现）。

### 5.1 门禁结果（本执行器沙箱，2026-11-08，重基到 `4dc2f2c930` 之后）

`BASE_SHA=4dc2f2c930503b5aedfeaa8f55ca8ab1c64a701b bash tools/ci-openswarm.sh`：**退出 1**，但失败集合与
**未改动的 main（`4dc2f2c930`）完全一致**：

| | main（`4dc2f2c930` 原样） | 本分支（main + 本 PR） |
|---|---|---|
| swrm 用例数 | 453 | 475（+22：本卡新增用例） |
| 失败 / 错误 | 4 / 0 | 4 / 0（同一批用例名） |
| 跳过 | 4 | 6（+2：`test_real_codex_sandbox`，需真 codex） |

失败的 4 项全部是本沙箱的环境限制，main 原样跑同样失败，与本改动无关：

1. `test_consult_outside_git_uses_temporary_worktrees`——临时目录落在仓内时的 git 解析；
2. `test_cursor_systemd_unit_is_printed_not_installed`——`getpwuid(): uid not found: 0` / `No username set in the environment`（容器 passwd 里没有 root 条目）；
3. `test_pi_isolation` 两项——`bwrap: No permissions to create a new namespace`（内核不允许非特权用户命名空间）。

另外，门禁脚本把 `TMPDIR` 放在很长的仓内路径（`$PWD/.tmp/test-tmp`），会让 48 项 lane 用例撞上
`lane control socket path exceeds the 108-byte AF_UNIX limit`；把 `TMPDIR` 指到仓内短路径后这 48 项全部通过。
本地复现命令（与 CI 门禁同环境变量，只改 `TMPDIR` 长度）：

```bash
T="$PWD/.t/ci"; mkdir -p "$T"/{home,state,config,cache,data,gh,capacity,tmp}
(cd swrm && env -u OPENSWARM_HOME TMPDIR="$T/tmp" HOME="$T/home" OPENSWARM_HOME="$T/state" \
  OPENSWARM_FLEET_GUARD_CONFIG="$T/fleet-guard.disabled.json" OPENSWARM_FLEET_CAPACITY_DIR="$T/capacity" \
  XDG_CONFIG_HOME="$T/config" XDG_CACHE_HOME="$T/cache" XDG_DATA_HOME="$T/data" GH_CONFIG_DIR="$T/gh" \
  python3 -m unittest discover -s tests -v)
```

新增用例全部通过（`swrm/tests/test_codex_sandbox_*.py`、`swrm/tests/test_lane_worktree_gitdir.py`、
`swrm/tests/test_real_codex_sandbox.py` 跳过、`tools/tests/test_check_codex_sandbox_gitdir.py`）；
`tools/tests` 全量 155 项通过。

### 5.2 PR 头 CI 变红的根因与修复（自动修 CI，本轮定位）

PR 头 `9a03be7b11` 的 CI 红在三个 ubuntu 作业（Python 3.9/3.11/3.13）。本地用同一套环境变量复现，
四棵树对比如下（swrm 单测全量）：

| 树 | 用例数 | 失败 / 错误 | 错误用例 |
|---|---|---|---|
| 基线 `2d5ec1ae60` 原样（**不含本 PR**） | 415 | 4 / **2** | `test_guardrails.GuardrailsTests.test_enforced_risks_all_protocols_and_policy_opt_out`、`test_guardrails.GuardrailsTests.test_rm_block_records_only_path_class` |
| PR 头 `9a03be7b11`（基线 `2d5ec1ae60` + 本 PR） | 437 | 4 / **2** | 同上两例 |
| main `4dc2f2c930` 原样 | 453 | 4 / 0 | 无 |
| main `4dc2f2c930` + 本 PR（本分支） | 475 | 4 / 0 | 无 |

结论：**CI 变红不是本 PR 引入的，而是 PR 头的基线太旧**。基线 `2d5ec1ae60` 的 `swrm/bin/swrm` 里
`enforced_risk` / `shell_invocations` 认不出 `test_guardrails` 用的两类事件，于是
`xs.enforced_risk(...)` 返回 `None`，测试报 `TypeError: 'NoneType' object is not subscriptable`；
这 2 个错误在**基线原样（不含本 PR）**的树上同样出现，可直接对照。

main（`4dc2f2c930`）已经重写了 `shell_invocations` / `enforced_risk`，修好了这两项。因此
**按卡面要求把 PR 头重基到 main 即修复 CI 变红**：本轮没有改本 PR 的实现，也没有删/跳过/放宽任何断言，
只做了重基（`git diff 2d5ec1ae60 9a03be7b11` 套到 `4dc2f2c930` 上只产生行号偏移，无语义冲突），
外加本文件与测试 docstring 的台账/映射更新。

复现方式（本地 clone 内对比，不依赖外网）：

```bash
git clone --shared <本机 base 仓> .t/c
git -C .t/c checkout 9a03be7b11   # PR 头：2 个 guardrails 错误
git -C .t/c checkout 2d5ec1ae60   # 基线原样：同样 2 个 guardrails 错误（证明不是本 PR 的锅）
git -C .t/c checkout 4dc2f2c930   # main 原样：0 个错误
```

## 6. 现场待办（govpn / govpn2 / gores2；CX10 恢复前执行）

```bash
# 1) 现场自查：每条 codex 车道的真实 gitdir 与可写根（verdict 必须全是 writable）
tools/check-codex-sandbox-gitdir.py --home "$OPENSWARM_HOME" --probe
# govpn 期望：CX5 -> .../.git/worktrees/wt-CX5，CX10 -> .../.git/worktrees/wt-CX10，两者不同

# 2) 部署含本修复的发行版后，重启车道并跑一次真实任务，确认 lane 产出提交
swrm lane down CX10 && swrm lane up CX10

# 3) 真实 Codex 沙箱内的双车道提交回归（需要 codex 与额度）
OPENSWARM_REAL_CODEX=1 OPENSWARM_REAL_CODEX_MODEL=gpt-6-luna \
  python3 -m unittest swrm.tests.test_real_codex_sandbox

# 4) 同类检查（多 codex 车道主机）
#    govpn2: CX2 / CX4 ；gores2: CX9 —— 逐条跑第 1 步
```

CX10 的 `PAUSE-CX10` 在第 1 步与第 2 步都通过、且第 3 步或连续 3 次真实任务各自产出提交之后，才可恢复。

## 7. 变更基线说明

本轮按卡面要求把 PR 头 `9a03be7b11` 重基到 `main`（`4dc2f2c930`）。`#1894` 已把 vendored `openswarm/` 目录
整体换成 `swrm/`，旧布局的 `openswarm/bin/openswarm`、`openswarm/tests/test_*` 在 main 上已不存在，因此上一轮
已把运行时改动与测试**按新布局移植**到 `swrm/bin/swrm`、`swrm/tests/`；本轮只做重基与台账更新：

- 第一个 PR 头（`7f41693c7e`）的 `openswarm/bin/openswarm:8462`（车道工作树固定为 `wt`）与 `:8525`
  （`cmd = [binary, *codex_executor_flags(), "app-server"]`，启动不带可写根）对应的新位置是
  `swrm/bin/swrm:7048`（admin dir 命名 `wt-<lane>`）与 `swrm/bin/swrm:7175`（app-server 启动即带可写根）。
- 重基方式：把 `git diff 2d5ec1ae60 9a03be7b11`（上一轮 PR 头相对其基线的全部改动）套到 `4dc2f2c930` 上，
  `git apply --check` 报告只有行号偏移（`swrm/README.md` +16、`swrm/bin/swrm` 7 个 hunk 偏移 96–114 行），
  **无语义冲突**，因此 main 上已合并的改动与本 PR 的意图都完整保留。

## 8. 本轮执行环境的一个硬限制（提交需由 host 侧完成）

本执行器沙箱把 base 仓的 git 管理目录**只读挂载**了：

```
/dev/vda3 /root/work/openbot-executor-base/.git ext4 ro,nosuid,nodev,relatime 0 0
```

本车道 worktree 的 admin dir 正在该只读挂载里（`/root/work/openbot-executor-base/.git/worktrees/wt13`），
因此 `git add` / `git commit` 在沙箱内必然失败：

```
fatal: Unable to create '/root/work/openbot-executor-base/.git/worktrees/wt13/index.lock': Read-only file system
```

这不是本卡要修的 codex 沙箱问题（那是 codex 的 bwrap 沙箱，不是执行器沙箱），但结论对本轮同样适用：
**改动以工作树内容交付，提交与 force-push 由 host 侧的 AD 完成**（`tools/ci-swrm.sh` 的
`detect_changed_paths` 同时统计 `git diff HEAD` 与 `git ls-files --others`，未提交的工作树改动也在门禁范围内）。
