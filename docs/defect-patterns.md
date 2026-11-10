# 缺陷模式库

新铸卡与补池预审应对照本清单；存量卡不追溯重铸。更新本库须通过 PR 审查，不得直接修改模板源绕过审查。

首批模式（2026-11-02 实测）：

1. **测错对象**：测试通过，但针对的不是需求所指对象或路径。验收明确目标对象并验证其结果。
2. **局部覆盖却报全局**：只检查局部样本，却宣称全局完成。声明覆盖范围并检查全量或准确限定结论。
3. **状态读写结构不一致**：写入与读取使用不同字段/结构。验证写后读回及字段一致性。
4. **dry-run 真执行**：预演模式仍产生副作用。验证 dry-run 前后状态/文件/调用次数不变。
5. **配置优先级写反**：低优先级配置覆盖高优先级配置。用冲突配置验证最终取值。
6. **清理丢任务**：清理/重启流程意外移除待办。验证清理前后任务仍存在且可继续处理。

## #1732 预审移植与验收记录

沿用旧 PR #1767 的卡片解析、正文估算、理由码、PR diff 校验及测试，迁移到 `swrm/`；续做提交的模式库、模板和 prompt 改动保留。预审只检查新铸卡/新导入卡，现存 issue 队列不追溯重铸。模式库仍须经 PR 审查更新，模板引用本文件而非复制可漂移的模式源。

支持的验收格式：`验收：` / `acceptance:` 后的逐行条目，或 `## Acceptance (measurable)` / `## 验收` 段落。每条须声明实际 shell 命令（建议用反引号包裹），或「目标 + 等于/输出包含/equals 等比较符 + 数值/引号内预期值」。预审不执行命令，也不以测试工具关键字代替可测性。围栏内的模板示例不提供当前卡的必需字段；失败模式标题、引用及验收脚手架不计入有效正文。

BLOCKER → 文件:行 → 对应测试 → 结果：

- B1 → `swrm/bin/swrm:12865`（命令语法、明确比较与裸词散文拒绝）、`swrm/bin/swrm:12910`（逐条报错）→ `swrm/tests/test_card_lint.py:203` 的 `test_commands_and_explicit_conditions_are_measurable`、`:212` 的 `test_keywords_and_vague_comparisons_do_not_prove_acceptance`（含 `pytest functionality works`）、`:219` 的 `test_each_bad_item_is_reported_beside_a_valid_command` → PASS。拒绝 `pytest 功能正常`、`pytest functionality works` 和反引号包裹的模糊描述，允许 bun 脚本、ls/wc/grep 管道、`git diff --check`、`make test`。
- B2 → `tools/mkcard.py:37`（验收声明）、`:39`（Plan 数据形状/真实 fixture 门槛）、`:43`（Design 状态机矩阵和集成测试要求）→ `tools/tests/test_mkcard.py:50` 的 `test_three_generated_cards_pass_real_preflight` → PASS。真实调用 mkcard 生成三张卡，再逐张运行 `swrm/bin/swrm card-lint`，覆盖 bun 脚本、biome、生产管道命令及模式库引用。
- B3 → `tools/lane-autodispatch/autodispatch.py:3252`（预审入口；找不到 lint 脚本时拒绝建卡并告警，不放行）、`:3266`（所有新建 issue 先预审）、`:3277`（生产 prompt）、`:3355`（replenish 仅成功导入后记状态）→ `tools/tests/test_card_replenish.py:63` 的 `test_local_rejection_preserves_card_and_reports_item`、`:81` 的 `test_repo_generation_rejects_then_imports_only_after_success`、`:98` 的 `test_a_missing_lint_script_refuses_the_card`、`:105` 的 `test_production_planner_prompt_passes_and_carries_requirements` → PASS。另保留复现测试 `tools/tests/test_card_pre_review_1732.py` 与夹具 `tools/tests/fixtures/card_pre_review_good.md`（提交 5f032388）。
- B4 → `docs/defect-patterns.md:20`（本映射）→ 上述完整测试命令及 `git diff --check HEAD` → PASS。提交说明由调度方提交时沿用此映射；执行工作树按禁止提交规则保留改动。

Issue 验收：缺失/不可测验收的具体条目、缺失败模式段均有拒绝测试；三张真实生成卡均带模式库引用并通过预审；预审单元测试全绿。额外保留薄卡和薄 PR 回归，补入围栏示例不能伪造必需字段的测试。

验证命令：`python3 -m pytest -q tools/tests/test_mkcard.py tools/tests/test_card_replenish.py swrm/tests/test_card_lint.py tools/tests/test_autodispatch_state_home.py tools/tests/test_autodispatch_host_recovery.py` → **77 passed, 24 subtests passed**。

REPORT: dropped — 旧路径 `openswarm/lib/dispatch/*` 的退役引擎内部不恢复；所读旧 PR 的 diff 本身未修改此目录，无需移植引擎内部。`pi-isolated` 同样无旧 PR 改动，保持当前实现。有效的补池入口仍在 `tools/lane-autodispatch/autodispatch.py`，已集成而非 dropped。
