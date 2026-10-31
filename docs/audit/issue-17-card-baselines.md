# 回归任务卡基线验证核实报告 (Phanes Issue #17)

## 1. 概述与定位

本报告针对 Phanes Issue #17，对以下四张回归任务卡进行基线验证核实：
1. `cursor-acp-lane`
2. `cursor-steer-report`
3. `dispatch-approval-gate`
4. `process-search-results`

### 1.1 任务卡文件定位
- **任务卡存储位置**：`/Users/surlymo/work/agent-knowledge/regression/`
  - `cursor-acp-lane`：[`/Users/surlymo/work/agent-knowledge/regression/cursor-acp-lane/`](file:///Users/surlymo/work/agent-knowledge/regression/cursor-acp-lane/)
  - `cursor-steer-report`：[`/Users/surlymo/work/agent-knowledge/regression/cursor-steer-report/`](file:///Users/surlymo/work/agent-knowledge/regression/cursor-steer-report/)
  - `dispatch-approval-gate`：[`/Users/surlymo/work/agent-knowledge/regression/dispatch-approval-gate/`](file:///Users/surlymo/work/agent-knowledge/regression/dispatch-approval-gate/)
  - `process-search-results`：[`/Users/surlymo/work/agent-knowledge/regression/process-search-results/`](file:///Users/surlymo/work/agent-knowledge/regression/process-search-results/)
- **本工作树关联记录与引用**：
  - [`tools/cards/c1-58.md`](file:///Users/surlymo/.claude/openswarm-executor-candidate/lanes/A2/wt/tools/cards/c1-58.md#L10-L12)：明确指出 `dispatch-approval-gate` 和 `process-search-results` 使用类型检查（`typecheck:node` / `typecheck:renderer`）在 `base_commit` 上早已通过，无法证明 executor 做出了有效变更。
  - [`tools/cards/c1-58b.md`](file:///Users/surlymo/.claude/openswarm-executor-candidate/lanes/A2/wt/tools/cards/c1-58b.md#L10-L16)：记录了将相关测试向 E2E 脚本改造、确保“基线必败、修复后必过”的过程要求。
  - `agent-knowledge` 仓库提交 [`ff1c4e336e29995831986aaeda66e353ff66abec`](file:///Users/surlymo/work/agent-knowledge)（分支 `openswarm/20261027-224617-066104`）：记录了针对这 4 张卡片修正基线失败证据和细化验证命令的历史改动。

### 1.2 核心结论
**这 4 张回归任务卡当前在 `meta.yaml` 中配置的验证命令在基线提交（baseline commit）上全部会通过（exit code 0），即无法证明变更。**

---

## 2. 逐卡核实与基线分析

---

### 2.1 `cursor-acp-lane`

- **任务卡路径**：
  - [`regression/cursor-acp-lane/card.md`](file:///Users/surlymo/work/agent-knowledge/regression/cursor-acp-lane/card.md#L1-L4)
  - [`regression/cursor-acp-lane/meta.yaml`](file:///Users/surlymo/work/agent-knowledge/regression/cursor-acp-lane/meta.yaml#L1-L10)
- **所属仓库**：`openswarm`
- **基线提交（base_commit）**：`9cbcb404043e8bcc5e55e6dfac8f83ec87425d02` ("openswarm(codex): Fix resident lanes on a real host")
- **修复/合入提交（source_commit）**：`355f284870b1a6dfc3222b0d9d99f8f03b8a346d` ("openswarm(codex): Add Cursor Agent CLI lane over ACP"，对应 source_run `20261026-012756-c4a390`)
- **当前配置验证命令**：
  ```bash
  python3 -m unittest discover -s tests
  ```

#### 基线核实与无法证明变更的原因
1. **代码检查证据**：
   - 在基线提交 `9cbcb404043e8bcc5e55e6dfac8f83ec87425d02` 中，`tests/test_lane.py`（[`tests/test_lane.py`](file:///Users/surlymo/.claude/openswarm-executor-candidate/lanes/A2/wt/openswarm/tests/test_lane.py)）仅包含 Codex 和 Pi 的 lane 测试，尚未引入任何针对 Cursor ACP 的测试方法。
   - `tests/fixtures/fake_cursor_acp.py` 在基线提交中完全不存在。
2. **测试结果**：
   - 基线提交时既有的测试套件本身是完整的且完全绿灯。
   - 运行通用的 `python3 -m unittest discover -s tests` 时，由于 Cursor 的测试用例尚未被发现或定义，既有用例全部通过，退出码为 `0`。
   - **结论**：验证命令在基线提交上直接通过，**无法证明** Cursor Agent CLI lane over ACP 功能是否被实现。

#### 建议验证命令（基线失败、修复后通过）
使用针对该卡片所要求的具名用例进行靶向验证：
```bash
PYTHONPATH=tests python3 -m unittest test_lane.LaneTests.test_cursor_assign_selects_model_and_commits_without_verify
```
（或完整测试集）：
```bash
PYTHONPATH=tests python3 -m unittest \
  test_lane.LaneTests.test_cursor_assign_selects_model_and_commits_without_verify \
  test_lane.LaneTests.test_cursor_systemd_unit_is_printed_not_installed \
  test_lane.LaneTests.test_cursor_model_missing_fails_with_available_values \
  test_lane.LaneTests.test_cursor_permission_is_allowed_and_logged \
  test_lane.LaneTests.test_cursor_steer_cancels_then_continues_same_session \
  test_lane.LaneTests.test_cursor_abort_marks_run_stopped \
  test_lane.LaneTests.test_cursor_budget_interrupt_is_recorded \
  test_lane.LaneTests.test_cursor_crash_fails_run_and_restarts_child
```
- **基线表现**：基线提交缺少上述方法，unittest 抛出 `AttributeError: type object 'LaneTests' has no attribute 'test_cursor_assign_selects_model_and_commits_without_verify'`，退出码为 `1`（失败）。
- **修复后表现**：在修复提交 `355f284870` 及之后通过，退出码为 `0`（成功）。

---

### 2.2 `cursor-steer-report`

- **任务卡路径**：
  - [`regression/cursor-steer-report/card.md`](file:///Users/surlymo/work/agent-knowledge/regression/cursor-steer-report/card.md#L1-L4)
  - [`regression/cursor-steer-report/meta.yaml`](file:///Users/surlymo/work/agent-knowledge/regression/cursor-steer-report/meta.yaml#L1-L10)
- **所属仓库**：`openswarm`
- **基线提交（base_commit）**：`355f284870b1a6dfc3222b0d9d99f8f03b8a346d` ("openswarm(codex): Add Cursor Agent CLI lane over ACP")
- **修复/合入提交（source_commit）**：`3715f5bd016f5becd41cba2dea042deacc6f860f` ("openswarm(pi): Cursor lane steer keeps the final report"，对应 source_run `20261026-014338-2491f8`)
- **当前配置验证命令**：
  ```bash
  python3 -m unittest discover -s tests
  ```

#### 基线核实与无法证明变更的原因
1. **代码检查证据**：
   - 本卡片的修复目标是解决 Cursor 交互转向（steer）中断重续时后续 prompt 漏掉 JSON report 指令，以及 lane 正常结束无 report 时应标记为 `partial` 而非 `failed` 的缺陷。
   - 检查提交 `3715f5bd01` 的 diff（[`bin/openswarm`](file:///Users/surlymo/.claude/openswarm-executor-candidate/lanes/A2/wt/bin/openswarm#L855-L875) 与 [`tests/test_lane.py`](file:///Users/surlymo/.claude/openswarm-executor-candidate/lanes/A2/wt/tests/test_lane.py#L229-L276)）：修复代码在 `tests/test_lane.py` 中新增加了 3 个针对性测试：
     - `test_cursor_turn_without_report_is_partial`
     - `test_cursor_turn_without_report_honours_verify`
     - `test_cursor_turn_without_report_or_change_is_failed`
     并在 `test_cursor_steer_cancels_then_continues_same_session` 中增加了对 `REPORT_MARKER` 的断言。
2. **测试结果**：
   - 在基线提交 `355f284870` 上，未覆盖上述缺失 report 的边界行为，基线代码上的 `test_lane.py` 自身是全部通过的。
   - 运行 `python3 -m unittest discover -s tests` 直接返回 `0`。
   - **结论**：验证命令在基线提交上直接通过，**无法证明**转向重续是否保留了 report 指令以及是否正确分类 `no_report` 状态。

#### 建议验证命令（基线失败、修复后通过）
指定新增与补全断言的具体测试方法：
```bash
PYTHONPATH=tests python3 -m unittest \
  test_lane.LaneTests.test_cursor_steer_cancels_then_continues_same_session \
  test_lane.LaneTests.test_cursor_turn_without_report_is_partial \
  test_lane.LaneTests.test_cursor_turn_without_report_honours_verify \
  test_lane.LaneTests.test_cursor_turn_without_report_or_change_is_failed
```
（或核心精简用例）：
```bash
PYTHONPATH=tests python3 -m unittest test_lane.LaneTests.test_cursor_turn_without_report_is_partial
```
- **基线表现**：基线提交不存在该测试方法，unittest 退出码为 `1`（失败）。
- **修复后表现**：在提交 `3715f5bd01` 上通过，退出码为 `0`（成功）。

---

### 2.3 `dispatch-approval-gate`

- **任务卡路径**：
  - [`regression/dispatch-approval-gate/card.md`](file:///Users/surlymo/work/agent-knowledge/regression/dispatch-approval-gate/card.md#L1-L4)
  - [`regression/dispatch-approval-gate/meta.yaml`](file:///Users/surlymo/work/agent-knowledge/regression/dispatch-approval-gate/meta.yaml#L1-L10)
- **所属仓库**：`openbot`
- **基线提交（base_commit）**：`5b37965fa1249ad6a41bbaf81bc7807f055d3da8` ("Read the real openswarm output and explain dispatch failures")
- **修复/合入提交（source_commit）**：`317be65636c1c8ba6d7aec337d9119b5ab8fe2dd` ("Ask the person before a risky dispatch runs"，对应 source_run `20261026-194344-34dd4b`)
- **当前配置验证命令**：
  ```bash
  bun run typecheck:node
  ```

#### 基线核实与无法证明变更的原因
1. **代码检查证据**：
   - `bun run typecheck:node` 执行的是静态 TypeScript 编译器检查（`tsc --noEmit -p tsconfig.node.json`）。
   - 在基线提交 `5b37965fa1` 上，Node 端的 TypeScript 代码本身即无类型报错，`bun run typecheck:node` 退出码为 `0`。
   - 本卡片核心功能为：针对风险任务（migration、release、credentials、data deletion 等）必须经过 approval gate 审批机制，若未获授权或拒绝执行则不可发起运行。这属于纯运行时控制流逻辑与权限门禁，静态类型检查根本不涉及任何运行时逻辑检验。
   - [`tools/cards/c1-58.md`](file:///Users/surlymo/.claude/openswarm-executor-candidate/lanes/A2/wt/tools/cards/c1-58.md#L10-L12) 明确核实并记录：“`dispatch-approval-gate` uses a verify (`bun run typecheck:node`) that already passes at `base_commit`, so the card cannot show that an executor made the change”。
2. **测试结果**：
   - 在基线提交上执行 `bun run typecheck:node` 直接成功（exit code 0）。
   - **结论**：验证命令在基线提交上直接通过，**完全无法证明**风险调度审批网关是否存在或生效。

#### 建议验证命令（基线失败、修复后通过）
在提交 `317be65636` 中引入了端到端行为测试脚本 [`openbot/scripts/dispatch-approval-e2e.ts`](file:///Users/surlymo/.claude/openswarm-executor-candidate/lanes/A2/wt/openbot/scripts/dispatch-approval-e2e.ts#L1-L25)：
```bash
bun scripts/dispatch-approval-e2e.ts
```
- **基线表现**：基线提交不存在该脚本（退出码 `1`）；即便将该脚本置于基线运行，由于基线中的 `handleDispatchTool` 尚未实现风险模式识别和审批挂起门禁，会直接未授权启动任务导致断言失败（退出码 `1`）。
- **修复后表现**：在修复提交 `317be65636` 及之后通过，输出报告并退出码为 `0`（成功）。

---

### 2.4 `process-search-results`

- **任务卡路径**：
  - [`regression/process-search-results/card.md`](file:///Users/surlymo/work/agent-knowledge/regression/process-search-results/card.md#L1-L4)
  - [`regression/process-search-results/meta.yaml`](file:///Users/surlymo/work/agent-knowledge/regression/process-search-results/meta.yaml#L1-L10)
- **所属仓库**：`openbot`
- **基线提交（base_commit）**：`ef3993b73b0c155133da4a0b8197e0d3965389db` ("Show each turn's response process and animate the working avatar")
- **修复/合入提交（source_commit）**：`00daa495dab431a9071af18885affc70789d9ce2` / `feeaba4ae0dc73afb043d34702b13dc8668881f2` ("Open the response process when a search hit selects a process note" / "Show the response process with a working avatar (fuses #18 and #23)"，对应 source_run `20261027-034107-457701`)
- **当前配置验证命令**：
  ```bash
  bun run typecheck:renderer
  ```

#### 基线核实与无法证明变更的原因
1. **代码检查证据**：
   - `bun run typecheck:renderer` 执行 `bun scripts/type-checker.ts --noEmit -p tsconfig.web.json`。
   - 在基线提交 `ef3993b73b` 上，前端 renderer 代码类型检查本身即为通过状态（退出码 `0`）。
   - 该卡片目标是：修复 `AgentTasks.tsx` 的属性透传，并在搜索到思考过程（process note）结果时，将其映射到轮次分组并展开折叠（disclosure）。
   - 静态类型检查既无法测试搜索结果选中时的展开行为（[`packages/ui/src/features/conversation/ResponseProcess.tsx`](file:///Users/surlymo/.claude/openswarm-executor-candidate/lanes/A2/wt/openbot/packages/ui/src/features/conversation/ResponseProcess.tsx#L8-L38)、[`src/renderer/src/features/conversation/stores/search-store.ts`](file:///Users/surlymo/.claude/openswarm-executor-candidate/lanes/A2/wt/openbot/src/renderer/src/features/conversation/stores/search-store.ts#L92-L134)），也无法作为证明该功能实现的有效手段。
   - [`tools/cards/c1-58.md`](file:///Users/surlymo/.claude/openswarm-executor-candidate/lanes/A2/wt/tools/cards/c1-58.md#L10-L12) 同样记录了：`process-search-results` 的 `verify`（`bun run typecheck:renderer`）在基线提交上早已通过。
2. **测试结果**：
   - 在基线提交上执行 `bun run typecheck:renderer` 直接成功（exit code 0）。
   - **结论**：验证命令在基线提交上直接通过，**无法证明**搜索命中思考项后展开和高亮 disclosure 的交互逻辑已被实现。

#### 建议验证命令（基线失败、修复后通过）
在提交 `feeaba4ae0` 中引入了端到端行为验证脚本 [`openbot/scripts/response-process-e2e.ts`](file:///Users/surlymo/.claude/openswarm-executor-candidate/lanes/A2/wt/openbot/scripts/response-process-e2e.ts#L1-L35)：
```bash
bun scripts/response-process-e2e.ts
```
- **基线表现**：基线提交不存在该脚本（退出码 `1`）；在基线提交的代码中由于缺乏 process-note 到 search match 的映射及折叠展开联动，断言将失败退出。
- **修复后表现**：在修复提交 `feeaba4ae0` 及之后通过，退出码为 `0`（成功）。

---

## 3. 验证核实汇总表

| 任务卡 ID | 目标仓库 | 基线提交 (`base_commit`) | 修复提交 (`source_commit`) | 当前 verify 命令 | 基线是否通过 (原) | 拟议改进 verify 命令 | 改进后基线结果 | 改进后修复结果 |
| :--- | :--- | :--- | :--- | :--- | :---: | :--- | :---: | :---: |
| **`cursor-acp-lane`** | `openswarm` | `9cbcb40404` | `355f284870` | `python3 -m unittest discover -s tests` | **通过 (0)** | `PYTHONPATH=tests python3 -m unittest test_lane.LaneTests.test_cursor_assign_selects_model_and_commits_without_verify` | 失败 (1) | 通过 (0) |
| **`cursor-steer-report`** | `openswarm` | `355f284870` | `3715f5bd01` | `python3 -m unittest discover -s tests` | **通过 (0)** | `PYTHONPATH=tests python3 -m unittest test_lane.LaneTests.test_cursor_turn_without_report_is_partial` | 失败 (1) | 通过 (0) |
| **`dispatch-approval-gate`** | `openbot` | `5b37965fa1` | `317be65636` | `bun run typecheck:node` | **通过 (0)** | `bun scripts/dispatch-approval-e2e.ts` | 失败 (1) | 通过 (0) |
| **`process-search-results`** | `openbot` | `ef3993b73b` | `feeaba4ae0` | `bun run typecheck:renderer` | **通过 (0)** | `bun scripts/response-process-e2e.ts` | 失败 (1) | 通过 (0) |

---

## 4. 改进建议

1. **元数据更新**：
   在知识库卡片元数据（`meta.yaml`）中，将上述 4 张卡片的 `verify_fails_on_base` 从 `unknown` 更新为 `true`，并替换 `verify` 命令为上述具备反证能力的靶向命令或 E2E 脚本。
2. **负向对照原则（Negative Control Check）**：
   任务卡入库标准必须包含强制性要求：**验证命令在基线提交（base_commit）必须返回非零退出码，而在修复提交（source_commit）上必须返回零退出码**。严禁将大范围的静态类型检查或空泛的测试发现直接作为单点功能变更的验证命令。
