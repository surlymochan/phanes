# RSI 路由权重门控说明与对比规范

## 1. 设计原则与背景

依据主架构设计原则（见 [`README.md:45`](file:///Users/surlymo/.claude/openswarm-executor-candidate/lanes/A1/wt/README.md#L45)）：
> **Self-improvement is evidence-driven and gated.** RSI proposes experience and policy changes; weighted routing stays record-only until controlled comparisons on the same task type and environment justify enabling it, and every change can roll back.

RSI（Recursive Self-Improvement，递归自完善层，对应 `knowledge.repo`）负责从实际运行历史中提炼经验教训（lessons）、复用模式（patterns）以及路由优化建议（routing advice）。然而，执行控制平面（execution plane）默认严守确定性与稳定性，**权重路由在未完成受控对照验证之前必须保持只读记录（record-only）状态**，不得自动替代线上生产分发。

---

## 2. 现有 RSI 核心入口清单与代码路径

### 2.1 路由（Routing）入口
- **实时路由决策**：[`openswarm/bin/openswarm:3197-3243`](file:///Users/surlymo/.claude/openswarm-executor-candidate/lanes/A1/wt/openswarm/bin/openswarm#L3197-L3243) (`route`)。基于 `policy.json` 中的 `default_order`、`review_types`、`ocgo_first_types` 与额度/窗口状态给出执行分发。
- **权重计算与只读建议**：
  - 样本指标聚合：[`openswarm/bin/openswarm:2125-2182`](file:///Users/surlymo/.claude/openswarm-executor-candidate/lanes/A1/wt/openswarm/bin/openswarm#L2125-L2182) (`compute_routing_weights`)。
  - 只读建议生成：[`openswarm/bin/openswarm:2204-2241`](file:///Users/surlymo/.claude/openswarm-executor-candidate/lanes/A1/wt/openswarm/bin/openswarm#L2204-L2241) (`routing_advice`)。明确声明 `"actual routing unchanged"`。
  - 运行时记录：[`openswarm/bin/openswarm:5173-5178`](file:///Users/surlymo/.claude/openswarm-executor-candidate/lanes/A1/wt/openswarm/bin/openswarm#L5173-L5178) 及 `:5250, :5409` (`cmd_run`)。仅将 `routing_advice` 写入 `run.json` 与账本 `ledger.jsonl`，执行器仍由 `route()` 真实派发。
- **CLI 审查与对比入口**：
  - `openswarm routing weights [--out FILE] [--fleet]`：[`openswarm/bin/openswarm:2243-2248, 11601`](file:///Users/surlymo/.claude/openswarm-executor-candidate/lanes/A1/wt/openswarm/bin/openswarm#L2243-L2248)。
  - `openswarm routing compare [--days N]`：[`openswarm/bin/openswarm:2259-2273, 11604`](file:///Users/surlymo/.claude/openswarm-executor-candidate/lanes/A1/wt/openswarm/bin/openswarm#L2259-L2273)。对偶未执行路线严格标为 `not run`。
- **策略门控与受控回归选择器**：
  - 默认策略开关：[`openswarm/policy.json:78-80`](file:///Users/surlymo/.claude/openswarm-executor-candidate/lanes/A1/wt/openswarm/policy.json#L78-L80)（`"min_samples": 8`, `"weighted": false`）。
  - 回归测试选择器：[`openswarm/bin/openswarm:11224-11235`](file:///Users/surlymo/.claude/openswarm-executor-candidate/lanes/A1/wt/openswarm/bin/openswarm#L11224-L11235) (`regress_select_backend`) 与 CLI 参数 `--routing {static,weighted}`（[`openswarm/bin/openswarm:11585`](file:///Users/surlymo/.claude/openswarm-executor-candidate/lanes/A1/wt/openswarm/bin/openswarm#L11585)）。

### 2.2 经验教训（Lessons）入口
- **提议与升级**：
  - `openswarm lessons propose`：[`openswarm/bin/openswarm:1122-1306, 11460`](file:///Users/surlymo/.claude/openswarm-executor-candidate/lanes/A1/wt/openswarm/bin/openswarm#L1122-L1306)（从已关联 P0/P1 的 PR 审查结果中提炼新规则草案）。
  - `openswarm lessons escalate`：[`openswarm/bin/openswarm:1414-1478, 11465`](file:///Users/surlymo/.claude/openswarm-executor-candidate/lanes/A1/wt/openswarm/bin/openswarm#L1414-L1478)（对反复复发的 lesson 提议 L3 防御护栏）。
- **匹配与提示词注入**：
  - [`openswarm/bin/openswarm:378-422`](file:///Users/surlymo/.claude/openswarm-executor-candidate/lanes/A1/wt/openswarm/bin/openswarm#L378-L422) (`select_lessons`)，[`openswarm/bin/openswarm:425-434`](file:///Users/surlymo/.claude/openswarm-executor-candidate/lanes/A1/wt/openswarm/bin/openswarm#L425-L434) (`add_lessons_to_prompt`)，`openswarm lessons preview`（[`openswarm/bin/openswarm:11456`](file:///Users/surlymo/.claude/openswarm-executor-candidate/lanes/A1/wt/openswarm/bin/openswarm#L11456)）。

### 2.3 复用模式（Patterns）入口
- **提议与淘汰检查**：
  - `openswarm patterns propose`：[`openswarm/bin/openswarm:768-825, 11471`](file:///Users/surlymo/.claude/openswarm-executor-candidate/lanes/A1/wt/openswarm/bin/openswarm#L768-L825)（从 `openswarm compare` 获胜方和重复审查项提炼模式）。
  - `openswarm patterns retire-check`：[`openswarm/bin/openswarm:832-868, 11473`](file:///Users/surlymo/.claude/openswarm-executor-candidate/lanes/A1/wt/openswarm/bin/openswarm#L832-L868)（检查模式对成功率无提升时提议下线）。
- **匹配与提示词注入**：
  - [`openswarm/bin/openswarm:504-557`](file:///Users/surlymo/.claude/openswarm-executor-candidate/lanes/A1/wt/openswarm/bin/openswarm#L504-L557) (`select_patterns`)，[`openswarm/bin/openswarm:560-569`](file:///file:///Users/surlymo/.claude/openswarm-executor-candidate/lanes/A1/wt/openswarm/bin/openswarm#L560-L569) (`add_patterns_to_prompt`)。

---

## 3. 何谓「同任务类型对比」（Same Task Type Comparison）

1. **正交分类与细分层级**：
   - 任务对比必须严格以任务卡元数据中的 `card_type`（如 `fix`、`feature`、`test`、`refactor`、`mechanical`、`review` 等）和 `difficulty`（1 至 5 级）为基准划分。
   - **禁止跨类推论**：严禁用低难度机械工作（例如 `mechanical`, `difficulty: 1`）的高通过率或低耗时，推断该执行器在复杂故障排除（例如 `debug`, `difficulty: 5`）中具备同等甚至更优表现。
2. **固定回归任务卡套件（Regression Card Suite）**：
   - 对比必须在固定的标准化任务集（`knowledge.repo/regression/cards/`）上开展。
   - 每张任务卡应明确指定 `meta.yaml` 中的 `card_type`、`difficulty`、`task` 目标要求以及确定的校验命令 `verify`。

---

## 4. 何谓「同环境对比」（Same Environment Comparison）

1. **统一基线代码状态（Base Commit Alignment）**：
   - 对照测试的所有分支必须检出相同的 `base_commit`，禁止一方在旧 commit 测试、另一方在已包含相关修复的新 commit 上测试。
2. **统一运行与隔离沙箱**：
   - 各执行器均在独立、干净的 git worktree 中运行，拥有完全相同的本地依赖版本（Node/Python/编译器）、构建产物缓存和运行权限。
3. **统一预算与约束条件**：
   - 任务给定的超时时限（`est_minutes` / `budget_minutes`）、最大上下文 Token 限制等外部配额完全一致。
4. **杜绝反事实臆测（Unrun Counterfactuals）**：
   - 依据 [`openswarm/bin/openswarm:2265-2272`](file:///Users/surlymo/.claude/openswarm-executor-candidate/lanes/A1/wt/openswarm/bin/openswarm#L2265-L2272) 与 `test_compare_shows_actual_outcome_and_unrun_counterfactual`：若线上运行真实调度了 Backend A，而历史建议为 Backend B，因 Backend B 并未在当时执行，其结果必须如实记为 `not run`。受控对比必须通过实际并行对撞（`openswarm compare`）或批量回归套件（`openswarm regress run`）真实运行两套体系，严禁将静态推论当作对比证据。

---

## 5. 权重路由启用门控标准（Gating Criteria）

在考虑将 `policy.json` 或生产配置中的 `routing.weighted` 调整为 `true` 之前，必须满足以下门控条件：

1. **样本充足度（Sample Adequacy）**：
   - 目标 `(backend, model, card_type)` 样本组的有效完成次数必须满足 `runs >= min_samples`（默认 8 笔，见 `compute_routing_weights` 中的 `enough` 标识）。
2. **核心质量指标不劣于基线**：
   - 执行通过率：`exec_pass_rate` 与人工或合流裁定 `accepted_rate` 不低于默认静态路由。
   - 首次通过率：`first_try_rate` 保持稳定或更优。
   - 返工与缺陷控制：`rework_rate`（重做率）与 `review_p1_rate` / `peer_review_p1_rate`（审查发现 P0/P1 率）低于或等于既有基线。
3. **成本效益显著**：
   - 在质量指标不退步的前提下，中位 Token 消耗（`median_tokens`）或中位耗时（`median_seconds`）呈现统计显著性降低。
4. **回归对比判定确认**：
   - 执行 `openswarm regress run --routing static --label before` 与 `openswarm regress run --routing weighted --label after`。
   - 运行 `openswarm regress compare --before ... --after ...`，其裁定 `verdict` 必须为 `"B not worse"` 且通过数无下降。

---

## 6. 如何回滚（Rollback Plan & Runbook）

一旦启用权重路由后观察到指标异常（通过率下降、Token 陡增、P1 审查增多），按以下顺序立即执行回滚：

1. **配置级即时回滚（< 1 分钟）**：
   - 修改 `~/.claude/openswarm/policy.local.json` 或部署策略，将 `routing.weighted` 设置为 `false`（或直接删除该覆盖项）。
   - 系统将立即退回确定性的静态规则选路（`policy.json` 中的 `default_order`、`thresholds` 与启发式规则），停止 advice 对分发决策的影响。
2. **知识库数据回退（< 5 分钟）**：
   - 若 `knowledge.repo/routing/weights.json` 被污染或存在异常偏置，使用 git 回退该文件：
     ```bash
     cd "$OPENSWARM_HOME/agent-knowledge"
     git checkout HEAD~1 -- routing/weights.json
     ```
   - 若由于脏运行导致账本数据异常，可针对异常 run_id 执行 `openswarm ledger accept <run_id> --reject --reason "poisoned sample"`，使 `routing_outcome` 剔除该样本。
3. **执行端紧急隔离（秒级）**：
   - 若某特定执行器 backend 出现服务异常或大面积失败，可直接使用 `--backend <known_good>` 显式调度，或利用断路器机制（breaker）在策略中触发冷却时间（`thresholds.breaker_minutes`），系统将自动绕过故障后端。

---

## 7. 测试钩子与单测保障

在 `openswarm/tests/test_routing_weights.py` 中维护以下单测：
- `test_routing_weighted_is_disabled_by_default_and_stays_record_only`：
  - 断言 `load_policy()["routing"]["weighted"] is False`。若有人修改配置默认开启，单测失败即红。
  - 断言 `regress_select_backend(card, {"kind": "routing", "routing": None}, ...)` 默认返回 `(None, None, None)`，证明不传 `--routing weighted` 时 advice 绝不生效；只有显式传参 `--routing weighted` 才会生效。
- `test_run_records_advice_without_changing_live_route`：
  - 断言在真实 `cmd_run` 分发中，即使账本样本推荐了更优的 `ocgo`，实际执行的 `run["backend"]` 与账本 `recorded["backend"]` 依然严格维持静态策略决定的 `codex`，只将 advice 作为观察字段记录。
