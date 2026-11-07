# evalid 三角色贯通设计稿

> 状态：待 Boss 审核，未实现、未部署。Refs [phanes#1887](https://github.com/surlymochan/phanes/issues/1887)。
> 本文是供审核的变更提案；[ARCHITECTURE.md](ARCHITECTURE.md) 仍是唯一架构 spec。获准后将定案并入其 §1.2、§2.4–§2.6 并追加演进记录，本文保留为提案记录。本次不改架构正文。

**结论：可扩展。** evalid 负责可验证的阶段契约与证据；swrm 负责角色解析、模型选择和执行；Phanes 控制面负责独立核对、holdout 与最终放行。声明 `evalid-spec`、`evalid-tdd`、`evalid-review` 后，由控制器展开完整链路，无须作者手拼门禁。角色名不等于成功，也不授予发布或合并权限。

## 1. 已有能力与真实缺口

核对基线为本机已存在的 `origin/main`，离线读取：evalid `10bd2d96d3396ae7a78bbc011ebddbdd9d1a09bf`；swrm `d3dd3156568f3292a5c248904926b58b07e039cb`。下列源码链接固定到这些版本，不代表运行时已验收。

| 层 | 已有实现与具体文件 | 本提案补齐的缺口 |
|---|---|---|
| evalid 定位 | [README.md][e-readme]：证据固化、快照回放；[DESIGN.md][e-design]：YAML/schema、可执行门禁；[AHA.md][e-aha] 是公开模板 | 不应描述成从零新建 Spec/TDD/Review，也不能把 README 的产品声明当成语义正确性证明 |
| Spec | [J4d 澄清][e-clarify] 检测含糊、缺 WHEN/THEN、占位符，写 `spec_clarify.json/.md`；`J4a` 结构一致性和 `J4b` delta 见 [atom registry][e-registry] | 尚无版本化的可执行验收清单、独立 Spec 核对回执与跨 run 冻结引用；文本扫描不能判断业务期望是否正确 |
| TDD | [I6a][e-run] 记录命令、退出码、输出 hash、`git_head`、快照和 binding；[I6b][e-replay] 回放并核对；[I6c][e-quality] 检查红绿命令覆盖关系、测试质量及 docs/refactor 模式 | 尚无外部控制器证明“先收红证据、后授权实现”，也没有角色间不可覆盖的测试清单交接 |
| Review | [J6][e-cr] 做 token 级 spec 对齐、测试跟随、断言数量与 diff 预算检查，写 `cr/review-0.json` | 确定性预检不能替代 terra 对业务语义、全提交链和 BLOCKER/FOLLOW-UP 的独立裁定 |
| 工作流与证据链 | [workflow schema][e-schema] 定义 phase/atom/gate；[workflow executor][e-executor] 编排；[execution log][e-log] 与 [integrity][e-integrity] 用 `prev_hash/entry_hash` 串联日志；[specs/workflow.md][e-workflow] 已列 Spec/TDD/Review 阶段 | 没有 `evalid-*` lane 契约；现有进程内日志排队不等于跨主机原子追加，hash 链也不是签名或可信时钟 |
| swrm | [bin/openswarm][s-cli] 的 `lane_config`、`cmd_lane_assign`、`run_verify`、`perform_peer_review`；[README][s-readme] 的 task-file、lane、verify、rework 接口 | lane 仍要求具体 backend/model 等，卡正文不会自动变成角色配置；`--verify` 返回码不足以表达多阶段交付 |
| Phanes | [tools/evidence.py](../tools/evidence.py) 已冻结测试 manifest，区分 red/green/posthoc；[tools/review_run.py](../tools/review_run.py) 保存可见测试、holdout 与 replay | 复用其严格语义；新增角色状态机和脱敏收据，不把控制面含隐藏测试的 evidence 包发给 lane |

evalid 的 CLI 当前名为 **`xflow`**，不是 `evalid spec/tdd/review`。下文明确区分“现有 atom 命令”和“拟新增角色命令”。不直接运行完整 yolo/corps 流程：其 archive 包含提交、推送等副作用，不属于角色门禁的权限。

## 2. 声明与 artifact 契约

### 2.1 最小声明、解析与权限

以下均为**拟新增格式**。项目、基线、Issue、预算、文件范围来自正常任务卡；角色本身不要求用户填模型。

任务卡头部（控制器生成下游 `input_evidence`，作者不能改）：

```yaml
---
evalid_version: 1
role: evalid-tdd
change_id: feature-example
input_evidence: ["sha256:<approved-spec-evidence>"]
---
```

lane 配置示意（省略既有 repo 等公共配置；不是当前可运行配置）：

```json
{
  "lanes": [
    {"name": "SPEC1", "role": "evalid-spec"},
    {"name": "BUILD1", "role": "evalid-tdd"},
    {"name": "REVIEW1", "role": "evalid-review"}
  ]
}
```

拟新增 `schemas/evalid-role.schema.json`：`evalid_version` 只能为 `1`，`role` 为上述三值，`change_id` 匹配 `^[A-Za-z0-9][A-Za-z0-9_-]*$`，`input_evidence` 为去重的内容地址数组；以上四项为角色卡必填。Spec 的根输入是控制器冻结的 intake evidence，不能凭空无父记录开始。lane 仅声明角色，公共 repo/预算由既有配置补齐。

解析规则：卡有声明则必须合法；lane 有声明则必须与卡一致；仅有 lane 声明时继承角色并由控制器补齐其余头部；两处都没有时保留旧路径。未知版本、拼错角色、相冲突的模型覆盖、缺父证据均拒绝，不静默按普通 impl 执行。`role` 是受控元数据，正文里的“忽略门禁”不改变它。角色路径禁用未批准的自动 peer review，避免重复评审和额外成本。

| 角色 / 控制器阶段 | 输入 | 允许产出 | 完成门禁 |
|---|---|---|---|
| `evalid-spec` / `spec` | 冻结 Issue/intake、基线、范围、既有架构和公开行为 | BDD 设计文档、`spec.json` 验收清单、现有澄清/一致性报告；不写实现或测试代码 | 需求可执行、常量/退出码/格式/边界明确；结构门禁过后由独立 `evalid-review` 的 `spec-check` 阶段核对，通过才产生 approved-spec |
| `evalid-tdd` / `red` → `green` | approved-spec、公开场景、固定基线、控制器阶段授权 | 测试、实现，`tdd.json` + 现有红绿 proof/snapshot、测试清单、quality/verify 报告 | red 为预期断言失败且可回放，控制器收妥后才授权 green；green 同测试转绿、质量门禁过、固定版本验证通过 |
| `evalid-review` / `spec-check` 或 `delivery` | 前者：Spec 候选；后者：approved-spec、固定 base/head 全提交链、公开证据、控制面脱敏结果收据 | 独立 `review.json`，含 PASS/FAIL/NEEDS-BOSS 与分级意见；不得改被审代码、测试、Spec 或旧证据 | spec-check 核对需求与可执行性；delivery 核对证据、语义、范围和全链路结果。控制器复验后发布门禁收据，模型意见不能覆盖实测失败 |

`stage` 由控制器写入授权请求，不是额外角色；作者不能把 red 改成 green，或把 delivery 改成 spec-check 来跳过步骤。Spec 独立核对复用第三角色，不新增第四种 lane。Review 写报告在独立输出区，`--read-only` 只表示不改被审树，不能作为宿主隔离证明。

### 2.2 公共 envelope 与 schema 约束（拟新增）

拟新增 `schemas/evalid-artifact.schema.json`，用 Draft-07 `oneOf` 区分 Spec、TDD、Review payload；复用 `src/core/workflow-integrity.js` 的规范化排序语义。字段如下均必填，明确标注可空者除外；各层对象关闭额外字段。

| 公共字段 | 类型与约束 |
|---|---|
| `schema_version`, `kind`, `role`, `stage` | `1`；`spec/tdd/review`；三角色 enum；与角色允许的阶段匹配 |
| `change_id`, `attempt`, `run_id` | 合法 change ID；非负整数；实际 swrm run ID，不能用排队 ID 替代 |
| `issue_ref`, `base_commit`, `head_commit` | 公开需求引用；完整 Git OID；完整 Git OID（无代码改动时可等于 base） |
| `parents` | 非空 `sha256:<64 lowercase hex>` 数组；必须能解析为同一 change 的已接收证据，不能循环 |
| `producer` | `{backend, model, effort, policy_digest, tool_commit}`；effort 仅非 Codex 后端可为 null；实际回执与冻结策略一致 |
| `artifacts` | 非空数组 `{name, sha256, media_type}`；逻辑名称不含路径穿越；hash 校验原始字节，解析不得跟随外部符号链接 |
| `payload` | 下表对应对象；缺输入不得填 PASS |
| `evidence_id` | `sha256:` 加 envelope 移除本字段后规范 JSON 的 SHA-256；自引用排除，文件 artifact 用字节 hash |

控制面按内容地址存储原始包；lane 只收到获准的公开子集。运行目录路径由传输层解析，不写入公开 envelope。Spec 文档独立于实现基线保存，不要求先把 Spec 合入代码分支。

| payload / artifact | 结构与强制约束 |
|---|---|
| `spec.json` | `{spec_version, scope, scenarios, commands, tdd_mode}`。version 为正整数；scope 为 `{allowed_files, excluded_files}`；mode 默认 `red-green`；scenarios 非空，每项 `{id, requirement_ref, given, when, then, command_id}`，ID 唯一，given/when 为非空文本，then 为非空可观测断言数组；commands 非空，每项 `{id, argv, cwd, expected_exit, timeout_s, environment_digest}`，argv 非空字符串数组、cwd 为仓库相对路径、timeout 为正整数、exit 为整数。每个场景可解析到命令和公开需求，不接受只有“应正常工作”的结论 |
| `spec-check` 的 `review.json` | `{subject, verdict, findings}`；subject 为 Spec 内容地址；verdict 为 `PASS/FAIL/NEEDS-BOSS`；finding 为 `{severity, requirement_ref, artifact_ref, reason}`，severity 为 `BLOCKER/FOLLOW-UP`；PASS 不得含 BLOCKER。控制器形成独立 approved-spec receipt，绑定 Spec digest、核对 run、策略 digest |
| `tdd.json` | `{spec_digest, phase, source_commit, test_manifest, proof_refs, scenario_tests, gate_results}`；phase 为 `red/green`；manifest 为所有可见测试及 fixture/config 的路径→hash（含测试 runner 配置/依赖锁）；proof refs 指向现有 I6a/I6b 结果和快照；scenario_tests 为每个公开场景到测试标识的非空映射；gate_results 记录逐项命令、真实 rc、报告 digest，不接受只有“tests passed” |
| `delivery` 的 `review.json` | `{spec_digest, tdd_digest, reviewed_base, reviewed_head, commits, verdict, findings, holdout_receipt}`；commits 为 base..head 的完整有序 OID 列表；finding 另含 `{path, line}`（非代码意见可空）；holdout_receipt 为控制面收据内容地址。verdict 同上；reviewed head 改动则本裁决失效 |

`then` 每项为 `{channel, operator, expected}`：channel 为 `exit/stdout/stderr/json/file`；operator 为 `eq/contains/absent/schema`；expected 是明确的 JSON 值或 schema 内容地址。JSON/file 另必填 `selector`（JSON pointer 或仓库相对文件名）。例如退出码 `eq 2` 与 stderr `contains "invalid argument"`，而非“错误友好”。由 Spec 定义命令入口与期望，TDD 将场景转成真实失败测试；Spec 不代写测试实现。新增命令入口在基线不存在是允许的，red/green 测试是否真正执行由后续阶段证明，不能在 Spec 阶段伪造通过记录。

**控制面私有收据**采用同版 schema 的独立定义：`{change_id, attempt, subject_head, spec_digest, suite_digest, required, status, isolation, l1_eligible, evidence_digest}`，status 为 `PASS/FAIL/INFRA/NOT_APPLICABLE`，isolation 为 `verified/unisolated/unknown`。原始 suite、失败样例、命令、日志、内部位置只在控制面；下游只拿脱敏收据及其 hash。NOT_APPLICABLE 只用于 intake 已冻结的 `holdout_required=false`，不能在测试失败后改成不适用；有效 L1 必须 holdout PASS 且隔离 verified。

### 2.3 串联、冻结和时间顺序

```text
intake 冻结（Issue、base、预算、公开范围、策略、控制面 holdout）
  → evalid-spec：可执行 BDD + spec.json
  → evalid-review/spec-check：独立核对 → approved-spec
  → evalid-tdd/red：只新增/改测试 → 断言失败 → 回放 → 控制器接收 red
  → 控制器授权 green → evalid-tdd/green：实现 → 同测试转绿 → quality + verify
  → 控制面：在固定 head 上执行隐藏测试，留私有证据，发脱敏收据
  → evalid-review/delivery：只读完整提交链与公开证据 → 裁决
  → 控制器重验 head、证据和放行公式 → VERIFIED / NEEDS-BOSS
```

- red 运行来自测试提交 R，生产文件须与 intake base B 相同，R 的 diff 只能含已批准测试范围。控制器在授权 green **之前**独立回放并保存 R、测试 manifest 和 red 接收序号；不采信作者时间戳。即使同一常驻 lane，red/green 也分成两个授权 turn/run。
- green 提交 G 必须包含 R，生产改动限于 scope；red 中已冻结测试、fixtures、runner 配置字节不变。允许新增回归测试，但必须重新走一次 red 授权；不能先改断言再解释。每次修复保留原 red，绑定当前实现基线；新增场景或测试变动须升 Spec/attempt 并重新核对。
- 每个逻辑 attempt 使用独立 change 工作目录，原生 proof 内部始终用 `red-0.json/green-0.json`，因为当前 I6c 固定读 `*-0`。外层 `change_id/attempt` 映射到唯一工具 change ID；不得用覆盖旧文件来“续跑”。
- proof 的 `git_head` 与工作树 snapshot 一起表示被测状态，不能只凭 HEAD。传输须带 snapshot、相关 Git 对象和基线，hash 对得上还须在控制器管理的验证环境回放；缺对象/依赖为 INFRA。复用 `tools/evidence.py` 的测试清单、固定版本、posthoc 区分语义，不把事后全绿称作 TDD。
- `parents` 串联 intake → spec → spec-check → red → green → delivery；控制器另以串行、原子写入的接收日志记录授权、收据和消费状态。事件去重键为 `(change_id, attempt, stage, input_digest)`，重投返回原记录，不重复派发；恢复从原始 runs 和已收证据重建，不靠内存队列。

## 3. swrm 集成、模型与失败回流

### 3.1 模型策略与已有接口的适配

| 步骤 | 冻结的模型与执行策略 | 核验点 |
|---|---|---|
| Spec | `codex / gpt-6.1-sol / high` | 实际 run 回执，不以卡上的声明代替 |
| Spec 独立核对、最终 Review | `codex / gpt-6.1-terra / high`，独立 run/会话 | Reviewer 不复用作者会话，不改被审版本 |
| 首轮 Build | 50% `pi / MiniMax-M3.1-Flash`，50% `codex / gpt-6-luna / high` | 单写者按批次序号交替分配，差值最多 1；同一任务 red/green 保持同一分桶，不各抽一次 |
| MiniMax 不可用 | 仅 MiniMax 分桶改由 `agy`（antigravity）承担；模型 slug 从已批准策略解析并冻结 | 不把 antigravity 当模型名，不沿用 Codex effort；记录计划模型、实用模型与 fallback 原因，agy 也不可用则 INFRA，不擅自改比例 |
| 质量失败修复 | `gpt-6.1-sol medium` 一轮 → `gpt-6.1-sol high` 一轮 → `NEEDS-BOSS` | 每轮新 run、`rework_of`、新 head，重新验证和独立评审；不无限自动修复 |

50/50 以**首轮任务分配数**为口径，非 token、耗时或通过数；fallback 和修复单列，不事后改分母。MiniMax/agy 不设置 Codex `high` 参数。基础设施重试沿用已选分桶，单任务默认最多重试 2 次（可由 Boss 在 intake 降低），失败即 INFRA；不消耗质量修复轮次，也不无限等服务。

该策略是对 [ARCHITECTURE.md §1.2](ARCHITECTURE.md) 的**待审增量**：50/50、agy fallback 与 sol 修复例外尚未进入现有作者白名单。Boss 审核前不激活；批准后统一更新权威表和三层容量策略。总并发仍受 policy/topology/host 交集限制，不因增加逻辑角色而增加 20 的目标容量。

拟增加 swrm `bin/evalid_roles.py` 适配层，`bin/openswarm` 只做入口接线：

1. 从受控头部/lane 得到角色，用冻结策略补齐模型、effort、独立性和读写范围，先做冲突验证。一次性 run 可解析为显式 `--backend/--model/--effort`；resident lane 的模型固定，须选择对应角色的具体 lane 池，不能在 `lane assign` 上虚构模型参数。red/green 固定到同分桶可用 lane。
2. 先生成不可变 request，再调用原有 `run --task-file --base --verify` 或 `lane assign --task-file --base --verify`。接收回执只存 run ID，等待 `result.json` 与实际 artifact 收齐后推进。实际模型偏离策略则拒绝阶段放行。
3. Spec 只允许设计文档与 manifest；Review 使用 `--read-only` 和独立报告输出。现有 `--read-only` 会清理工作树，且 lane 下一轮会重建：必须在清理/终态之前将报告、proof、snapshot 收至控制器拥有的输出区。输出区通过显式 artifact sink 接线，不能只靠作者 `files_changed`；不修改 swrm 通用最终 JSON 契约。
4. 现有 `openswarm review` 的 `review_family_compatible` 禁止 codex 作者配 codex Reviewer，`peer_review_prompt` 又要求只看 diff、不运行工具；其 P0/P1/P2 也不同于 Phanes 分级。因此角色路径采用独立 `run/lane assign --read-only` + `evalid-review` 卡，不修改通用 peer-review 跨后端规则，不把它的 approve 当最终放行。
5. `--verify` 只运行公开阶段检查或角色包装器，不含 holdout 参数。原 `run_verify` 只保存 rc/tail，完整日志由 evalid artifact 保存。角色证据默认不用 `--verify-baseline` 将失败归零：已有基线失败单列，公开验收命令必须真实通过；缺 verify 或 `rc=null` 是未验证。

### 3.2 复用的命令与拟新增的命令

以下为**当前存在的 evalid 命令模板**。`$CHANGE` 表示该 attempt 的工具 change ID，`$BASE` 是冻结的完整 base OID；变量由控制器设置，命令执行目录为被测项目。公开命令从 Spec 的 argv 经安全 shell quoting 构造，不能让卡正文替换控制器命令。

```bash
# Spec：结构检查 + 澄清；结果不能代替独立 spec-check
xflow atom run J4a.spec_consistency.structural --project-root . --change-id "$CHANGE"
xflow atom run J4d.spec_clarify.gate --project-root . --change-id "$CHANGE"

# TDD red：测试须先实际断言失败；I6a 自身成功表示符合“预期失败”
xflow atom run I6a.tdd.run --project-root . --change-id "$CHANGE" --phase red --test-command "$RED_COMMAND"
xflow atom run I6b.tdd.proof_validate --project-root . --change-id "$CHANGE" --phase red --tdd-mode red-green

# 控制器收妥 red 并授权后，才开始实现并执行 green
xflow atom run I6a.tdd.run --project-root . --change-id "$CHANGE" --phase green --test-command "$GREEN_COMMAND"
xflow atom run I6b.tdd.proof_validate --project-root . --change-id "$CHANGE" --phase green --tdd-mode red-green
xflow atom run I6c.tdd.quality_review --project-root . --change-id "$CHANGE" --diff-ref "$BASE" --tdd-mode red-green

# Review：针对固定 G 的干净树，diff-ref 覆盖 B..G，保留原报告
xflow atom run J6.cr.review --project-root . --change-id "$CHANGE" --diff-ref "$BASE"
```

I6b 的成功不能证明 red 是业务预期断言；外层 TDD 门禁额外要求 `failure_kind=assertion`、场景映射与测试计数，不接纳 import/syntax、timeout、空测试或 skip/focus。Review 先重验封存的 I6b 结果，再检查 I6c/J6 和语义；green 测试至少覆盖 red 命令选择的测试。

为免让 lane 自行拼接口，**拟新增**统一机器入口（不是现有能力）：

```bash
xflow role run --request role-request.json --output role-result.json
```

request schema 亦纳入 `evalid-role.schema.json`：`{schema_version, role, stage, change_id, attempt, run_id, base_commit, head_commit, inputs, policy_digest}` 全部必填，inputs 为受控文件逻辑名及 digest 数组。adapter 只向 evalid 传已完成阶段的包与公开命令；此命令负责验证/封装/回放，不调用模型、不调度 swrm、不替作者写实现。输出为公共 envelope；rc `0` 表示本阶段门禁通过，`1` 为质量/证据不符，`2` 为配置/版本/schema 错误，`3` 为基础设施失败。这是 wrapper 的新约定，原 atom rc/JSON 原样留存，不能按同一 rc 表硬套旧工具。

Spec 生产、TDD 编码、Review 语义裁决仍在模型 run 内；wrapper 校验这些输出和独立回执。角色状态机只使用白名单 atoms，不进入 archive/openissue/publish。Phanes holdout runner 只在控制面调用 `tools/review_run.py`，其 `--holdout-dir`、`--cmd-holdout` 和私有 evidence 参数不出控制面；该 runner 的 green 要求既有 red 记录，故后置隐藏验收须用独立验收 change ID 和 evidence 目录走其明确的 `--phase posthoc` 路径并保持 classification，不能覆盖既有 TDD 包或冒称完整 TDD。它校验的是回放与文件完整性，不自动证明执行环境隔离。

### 3.3 失败分类与放行公式

| 结果 | 回流目的地与动作 |
|---|---|
| Spec 含糊、验收缺口或独立核对失败 | 回 Spec；最多补订 2 轮，仍不清晰转 NEEDS-BOSS。新 spec_version 重跑核对，不让 Build 猜需求 |
| red 没有失败、回放不符、测试被改或不覆盖场景 | 不授权 green；作为质量失败走有界修复；保留拒收证据，不能改成 posthoc PASS |
| green/公开验收失败或 Review BLOCKER | 先 sol medium，再 sol high；每轮回到适用 red/green 和 Review，超限 NEEDS-BOSS |
| holdout 失败且对应公开要求 | 实测结果保留 FAIL；控制器生成只引用公开 requirement 的修复卡，走同一有界修复链，不带隐藏输入、断言或日志 |
| holdout 暴露未公开要求 | 遵守 [PROTOCOL.md V4](PROTOCOL.md)：不能构造作者 BLOCKER。保持 `holdout=FAIL`、交付未放行并转 NEEDS-BOSS；Boss 判断补公开 Spec 或将该样本排除，禁止静默改真值 |
| 超时、依赖缺失、模型/传输故障、证据丢失 | INFRA；按冻结上限重试，耗尽转 NEEDS-BOSS；不计作者失败，也不能记 PASS |
| 版本/schema/角色/模型冲突 | 派发前拒绝，交 Router 修卡；无 run ID 时无“已启动” |
| 仅 FOLLOW-UP | 记录后续，不阻断已满足公开验收且无其他失败的放行 |

交付 VERIFIED 必须同时满足：approved-spec 有效、按授权顺序 red 已接收、固定 G 的 green 回放与公开验收通过、必需 holdout 收据 PASS、独立 Review PASS 且无 BLOCKER、全提交链/文件范围合规、当前 head 与被审 G 相同。NOT_APPLICABLE 只能按 intake 的非 holdout 交付策略使用，不能计入有效 L1。任何必需结果 unknown/null 均不放行。

保持两套状态：swrm 的 `queued/accepted/running/done/partial` 原样留存；控制器的 `SPEC_PENDING/RED_ACCEPTED/GREEN_ACCEPTED/REVIEW_PENDING/VERIFIED/INFRA/NEEDS-BOSS` 由证据推导。`done`、`exec_pass=true`、进程健康或 reviewer approve 都不是上述公式的替代品。

## 4. holdout 边界与作弊反例

**holdout 只在控制面**：三个 lane 都不接收隐藏测试源码、fixture、参考实现、命令、私有挂载位置或原始日志。Spec 只看到完整的公开行为要求；隐藏测试只是这些要求的保留实例，控制面另行编写与冻结。Review lane 看到脱敏判定和公开 requirement，需私有诊断时交 Boss，不给 Reviewer 特权读取隐藏包。

控制面普通任务调度仍遵守架构的执行迁移原则；此处只保留专门的 holdout 验收器，不让控制面变成 Build 执行器。依据 [PROTOCOL.md §3](PROTOCOL.md)，Mac lane 不跑隔离探针，也不能凭“路径没下发”宣称不可读取。同环境可能读取隐藏材料的样本标 `unisolated`，不计有效 L1；有效实验作者须置于看不到控制面文件的 Linux 环境。隐藏包不能放进作者可读的同机目录、共享卷、Git 历史、prompt、缓存或公开 artifact store。

| 退化或作弊反例 | 防护与能力边界 |
|---|---|
| 实现完成后删除实现、补拍一份 red，再恢复实现 | 控制器在 green 授权前收 red、校验生产树与 base 相同并回放；可信边界是独立控制器，而非作者时间戳。不能证明模型脑中未先构思答案 |
| TDD 把 `expected=正确值` 改成当前输出；保持断言数量不变 | 冻结 red 的测试/fixture/config hash；跨 red/green 字节变动拒收。I6c/J6 数量检查只能辅助；错误 expected 仍须 Reviewer 对照 Spec 与 holdout 发现 |
| 用 `exit 1`、缺依赖、空测试、skip、只跑无关子集制造红绿 | assertion failure、实际测试计数、场景→测试映射、同选择器或 green 超集、独立回放并行约束；未知输出格式先补 runner 支持，不猜 PASS |
| 把功能改动声明成 docs/refactor 跳过 red | 默认强制 red-green；例外由 intake 冻结并经 Spec 核对。docs 必须无代码/测试 diff；refactor 须前后已有测试通过且行为不变，仍需独立语义评审 |
| Spec 与作者共谋，将错误行为写入 Spec | 独立 terra spec-check 对照 Issue/架构，需求不明转 Boss；holdout 不得凭隐藏要求惩罚作者。错误需求本身不是 hash 可解决的问题 |
| 用可写 artifact sink 伪造整条自洽证据 | 工具版本固定、验证器由控制器持有、重算字节 hash 并回放、单写者 append-only 接收记录；hash seal 不是签名，控制器被攻陷超出本方案保证 |
| 被测实现执行时读取同用户的隐藏测试或反射测试进程 | 隐藏 harness 留控制面，只通过公开 CLI/API 调用无隐藏挂载的 Linux 候选执行环境，返回公开输出后由控制面判定。现有 review_run 同环境白盒执行不能单独证明此边界；必须同进程导入被测代码且无法阻断读取的样本单列，不宣称严格隔离或有效 L1 |
| 并发 lane 改同一 status.json、覆盖 proof；下一任务清掉证据 | 每 attempt 独立目录/唯一工具 ID；阶段接收 CAS 与去重；先收集再清理，保留 Git 对象及依赖 manifest。现有进程内日志队列不足以替代它 |
| Reviewer 只看最后一条修复提交，或通过后 head 移动 | 审查冻结 B..G 的整个提交链及文件清单；受审 head 变更立刻使 verdict 失效，即使旧证据仍可回放 |
| 隐藏失败细节在修复循环中被逐步泄露 | 只回流公开要求和脱敏结果，私有日志/计数细节不进卡；首轮 L1 与后续修复分开统计，避免拿反复探测后的成功冒称首轮通过 |
| 为小任务执行完整流程导致成本高于收益 | 原子门禁复用缓存须绑定版本/内容 hash；不调用重复 LLM judge。首版不自动跳过 Spec，快速路径按架构仍待单独定案；记录阶段时长/token，评估后再简化 |

## 5. 可派发实现卡（8 张，文件 ownership 不重叠）

以下**仅为后续实现卡**；文件和验收测试中的新增入口尚不存在，所列命令是完成卡后的验收命令，本设计任务未执行它们。路径均以相应仓库根为起点，跨仓库同名文件不是同一 ownership。每卡可先用冻结 fixture/mock 独立开发；真实联调遵守依赖顺序，不能把 mock 通过当成上线成功。

| 卡 / 仓库 | 目标与独占文件范围 | 完成后的验收命令与关键判据 |
|---|---|---|
| C1 / evalid | 固定本稿 schema。仅 `schemas/evalid-role.schema.json`、`schemas/evalid-artifact.schema.json`、`test/roles-schema.test.js`、`test/fixtures/roles-schema/**` | `node --test test/roles-schema.test.js`：三角色有效包通过；缺父/未知字段/角色阶段冲突/错误 hash 形状/公开包混隐藏字段拒绝 |
| C2 / evalid | Spec manifest、公开场景覆盖和独立核对冻结器。仅 `src/roles/spec.js`、`test/roles-spec.test.js`、`test/fixtures/roles-spec/**` | `node --test test/roles-spec.test.js`：空场景、悬空 command、缺 requirement、未核对 Spec 阻断；合法 approved-spec 可追溯 |
| C3 / evalid | TDD 阶段 reducer 与 proof 适配，复用现有 I6 atoms，不改原 atom。仅 `src/roles/tdd.js`、`test/roles-tdd.test.js`、`test/fixtures/roles-tdd/**` | `node --test test/roles-tdd.test.js test/tdd-proof.test.js test/tdd-output-counts.test.js`：真实 red→green；事后 red、改测试/配置、空测试、快照缺失拒收；既有语义不退化 |
| C4 / evalid | 独立 Review 及放行公式校验器。仅 `src/roles/review.js`、`test/roles-review.test.js`、`test/fixtures/roles-review/**` | `node --test test/roles-review.test.js`：PASS 不覆盖 holdout FAIL、未知收据或 BLOCKER；FOLLOW-UP 不阻断；stale head/漏提交拒收；未公开要求 NEEDS-BOSS |
| C5 / evalid | `xflow role run` 接线、公共 envelope/CAS 存储与机器 rc；唯一 CLI 修改者。仅 `bin/xflow.js`、`src/roles/index.js`、`src/roles/protocol.js`、`test/roles-cli.test.js`、`docs/roles.md` | `node --test test/roles-cli.test.js test/cli-usability.test.js`：0/1/2/3 rc、内容地址、重复调用不覆盖、无 archive/发布副作用；依赖 C1–C4 后联调 |
| C6 / swrm | 角色解析、50/50 分配、模型 profile、evalid subprocess adapter。仅 `bin/evalid_roles.py`、`tests/test_evalid_roles.py`、`docs/evalid-roles.md` | `python3 -m unittest discover -s tests -p test_evalid_roles.py`：20 个首轮任务 10/10；MiniMax unavailable 只替换对应桶为 agy；修复 medium→high→NEEDS-BOSS，冲突拒绝；mock CLI 验证参数 |
| C7 / swrm | run/lane 生命周期接线、artifact sink 收集、禁重复 auto-review、result 元数据。仅 `bin/openswarm`、`tests/test_evalid_roles_wiring.py` | `python3 -m unittest discover -s tests -p test_evalid_roles_wiring.py`，并跑 `python3 -m unittest discover -s tests -p test_lane.py` 与 `python3 -m unittest discover -s tests -p test_review_oneshot.py`：一次性和常驻路径一致、清理前收包、遗留无 role 行为不变；依赖 C6 |
| C8 / Phanes | 控制面状态机、intake 冻结、私有 holdout 收据/脱敏、事件幂等与恢复验收；复用 evidence/review_run，不改其实现。仅 `tools/evalid_roles_control.py`、`tools/tests/test_evalid_roles_control.py`、`tools/tests/fixtures/evalid-roles/**` | `python3 -m unittest discover -s tools/tests -p test_evalid_roles_control.py`：离线真实小仓库红绿回放＋mock 模型全链路；含重复事件、崩溃恢复、漏证据、head 漂移、隔离未证、隐藏泄漏 canary；依赖 C1–C7 联调 |

C1–C4、C6 可并行，C5/C7/C8 先依据本稿 fixture 合同开发，再按依赖联调。共享的旧 atoms/registry/workflows 不在这些卡的修改范围；真有必要改动则另开明确 ownership 卡，不在并行卡里顺手改。

落地验收分两步：先由 C8 的离线小仓库验证完整用户路径和所有拒收分支；再在已批准模型/预算和满足隔离条件的执行环境跑一次真实 Issue→Spec→独立核对→red→green→holdout→Review，并读回实际模型、证据与终态。之后才考虑启用角色派发；配置开关默认关闭，回滚为停止新增角色任务并保留全部在途 run/证据，不能将未验证任务降级放行。

## 6. Boss 审核落点

- 批准三角色和 `spec-check/red/green/delivery` 阶段合同，以及控制器唯一推进权。
- 批准 50/50、agy fallback、sol medium→high 两轮修复对现有模型表的增量；批准后才并入权威架构和运行策略。
- 确认按公开需求冻结 holdout、仅控制面保存，以及未公开要求不构成 BLOCKER 的处理。
- 按 C1–C8 分派；本稿 ready 仅表示设计可供审核，不表示实现、部署或真实端到端验收完成。

[e-readme]: https://github.com/surlymochan/evalid/blob/10bd2d96d3396ae7a78bbc011ebddbdd9d1a09bf/README.md
[e-design]: https://github.com/surlymochan/evalid/blob/10bd2d96d3396ae7a78bbc011ebddbdd9d1a09bf/DESIGN.md
[e-aha]: https://github.com/surlymochan/evalid/blob/10bd2d96d3396ae7a78bbc011ebddbdd9d1a09bf/AHA.md
[e-clarify]: https://github.com/surlymochan/evalid/blob/10bd2d96d3396ae7a78bbc011ebddbdd9d1a09bf/xflow/atoms/j4d_spec_clarify_gate.py
[e-registry]: https://github.com/surlymochan/evalid/blob/10bd2d96d3396ae7a78bbc011ebddbdd9d1a09bf/atoms/registry.json
[e-run]: https://github.com/surlymochan/evalid/blob/10bd2d96d3396ae7a78bbc011ebddbdd9d1a09bf/xflow/atoms/i6a_tdd_run.py
[e-replay]: https://github.com/surlymochan/evalid/blob/10bd2d96d3396ae7a78bbc011ebddbdd9d1a09bf/xflow/atoms/i6b_tdd_proof_validate.py
[e-quality]: https://github.com/surlymochan/evalid/blob/10bd2d96d3396ae7a78bbc011ebddbdd9d1a09bf/xflow/atoms/i6c_tdd_quality_review.py
[e-cr]: https://github.com/surlymochan/evalid/blob/10bd2d96d3396ae7a78bbc011ebddbdd9d1a09bf/xflow/atoms/j6_cr_review.py
[e-schema]: https://github.com/surlymochan/evalid/blob/10bd2d96d3396ae7a78bbc011ebddbdd9d1a09bf/schemas/workflow.schema.json
[e-executor]: https://github.com/surlymochan/evalid/blob/10bd2d96d3396ae7a78bbc011ebddbdd9d1a09bf/src/core/workflow-executor.js
[e-log]: https://github.com/surlymochan/evalid/blob/10bd2d96d3396ae7a78bbc011ebddbdd9d1a09bf/src/core/execution-log.js
[e-integrity]: https://github.com/surlymochan/evalid/blob/10bd2d96d3396ae7a78bbc011ebddbdd9d1a09bf/src/core/workflow-integrity.js
[e-workflow]: https://github.com/surlymochan/evalid/blob/10bd2d96d3396ae7a78bbc011ebddbdd9d1a09bf/specs/workflow.md
[s-cli]: https://github.com/surlymochan/swrm/blob/d3dd3156568f3292a5c248904926b58b07e039cb/bin/openswarm
[s-readme]: https://github.com/surlymochan/swrm/blob/d3dd3156568f3292a5c248904926b58b07e039cb/README.md
