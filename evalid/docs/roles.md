# evalid 角色协议

在被测项目目录运行：

```sh
xflow role run --request role-request.json --output role-result.json
```

入口只校验控制器授权的一个阶段、回放公开证据并封装结果；不调用模型、不调度、不执行 archive、发布或合并。角色任务的最终推进权仍在控制器。

## 请求与输入文件

请求严格按 `schemas/evalid-role.schema.json#/definitions/role_request` 校验。必填字段为 `schema_version`、`role`、`stage`、`change_id`、`attempt`、真实 `run_id`、完整 `base_commit`/`head_commit`、`inputs` 和 `policy_digest`。不接受额外的 `payload` 字段。

`inputs` 是非空的 `{name, sha256}` 数组；`name` 相对于 request 所在目录，`sha256` 是文件**原始字节**的内容地址。所有文件必须存在、hash 匹配且不能通过符号链接越出输入目录。逻辑名称不能重复。路径属于传输层，不进入公开 envelope。`inputs` 必须包含 `stage.json`、`parent.json` 和父证据的完整公开依赖闭包；只传 payload 或文件字节 hash 不能代替父 envelope。

其中 `stage.json` 是阶段数据文件，公共元数据为：

```json
{
  "issue_ref": "example/project#1",
  "producer": {
    "backend": "codex",
    "model": "example-model",
    "effort": "high",
    "policy_digest": "sha256:<64 lowercase hex>",
    "tool_commit": "<full Git OID>"
  }
}
```

`producer` 来自实际 run 回执，策略 digest 必须匹配 request。控制器负责核对模型回执的真实性。按阶段向该对象补充以下字段；未知顶层字段拒绝，隐藏测试、命令和日志不得进入输入。

| 阶段 | 阶段字段与门禁 |
| --- | --- |
| `spec` | `spec`；`parent.json` 为控制器冻结的 intake 证据：schema 与可执行性检查，生成候选 Spec；此时不要求未来的独立 review，也不产生 approved-spec |
| `spec-check` | `review`；Spec 从 `parent.json` 的候选 envelope 读取：要求 PASS、subject 等于 Spec payload 的规范 JSON digest，且没有 BLOCKER |
| `red` | `scenario_tests`；从已封存 Spec → spec-check 父链经 C2 `freezeApprovedSpec` 重建 approved 对象。从当前项目读取已封存 proof/snapshot，调用 C3 `runTddStage` 实际执行 I6b 回放；不采信作者自报的 replay receipt |
| `green` | 同 red；从父 red envelope 引用的 `tdd-handoff` artifact 读取控制器已接收的 red evidence bundle。实际回放 green，并执行 I6c quality；校验 red 祖先、冻结 manifest 与场景映射 |
| `delivery` | `review`、脱敏 `holdout`、完整有序 `commits`；从父链重建 `approvedSpec` 并读取 green 的 `tdd-handoff`：调用 C4，PASS 不能覆盖 holdout 失败、stale head、漏提交或 BLOCKER |

`spec` 的根 `parent.json` 使用最小公开 intake 记录，字段为 `{schema_version:1, kind:"intake", change_id, attempt, base_commit, policy_digest, payload, evidence_id}`。`payload` 只含公开的 `requirements` 和冻结的 `holdout_required`；私有 holdout 信息留在控制面。其 `evidence_id` 同样按移除自身后的规范 JSON 计算。后续阶段的 `parent.json` 必须为紧邻上一阶段的完整 envelope。

控制器将上一阶段输出 envelope 作为下一阶段的 `parent.json`，并将输出目录中的内容寻址依赖文件全部列入下一请求的 `inputs`（原始字节 hash）。不再需要额外调用 `runTddStage` 获取交接字段。兼容输入中的 `spec`/`red`/`approvedSpec`/`tdd`/`intake` 若与封存父链不一致则拒收。

TDD proof 的 `git_head` 必须匹配冻结 head。入口不生成新的测试或 proof；作者须先运行 I6a。每个逻辑 attempt 由控制器提供独立项目目录及唯一工具 change ID，内部 proof 固定使用 `red-0`/`green-0`，不能覆盖旧 attempt。`attempt` 保留在 envelope 中。

## 结果与返回码

| rc | 含义 |
| --- | --- |
| 0 | 本阶段门禁通过，输出符合 `evalid-artifact.schema.json` 的完整公共 envelope |
| 1 | 质量或证据不符，含 NEEDS-BOSS；不授予后续阶段权限 |
| 2 | 配置、版本、schema、输入 hash/边界错误，或输出已存在 |
| 3 | 输入/输出 I/O、缺 proof/snapshot、回放基础设施失败 |

拒收结果是 `{ok:false, code, issues}` 诊断对象，不伪造成功 envelope。原 atom 返回码不套用 wrapper 的返回码表。

成功 envelope 包含 `kind`、`issue_ref`、`producer`、非空 digest `parents`、`artifacts` 及该阶段原始 payload。`evidence_id` 是移除自身后规范 JSON 的 SHA-256；它与序列化文件的字节 hash 有不同用途，stdout 与存盘的 envelope 完全一致。

成功输出目录保存完整公开依赖闭包：

- `parents[0]` 为上一阶段 envelope 的 **evidence_id**（Spec 为 intake 的 evidence_id），逐级串联 intake → spec → spec-check → red → green → delivery。
- `parents[1]` 为本阶段输入登记记录的 evidence_id。该记录为 `{schema_version:1, kind:"input", stage, change_id, attempt, base_commit, policy_digest, parents:[上一阶段 evidence_id], artifacts:[stage.json 原始字节的内容寻址条目], evidence_id}`，连同原始输入一起保存。
- `artifacts[0]` 是原始 payload；TDD 的 `artifacts[1]` 是 `tdd-handoff-<hash>.json`，包含 `{phase, status, spec_digest, digest, payload, evidence}`。`evidence` 为 C3 的完整 red/green bundle；`digest` 为 payload 的规范 JSON digest，供 C4 的 `tdd_digest` 绑定。
- 所有接收文件保存为 `input-<字节 hash>.json`，新输入登记保存为 `evidence-<字节 hash>.json`。artifact hash 覆盖原始字节；evidence_id 覆盖规范 JSON，两者不可互换。

入口在运行门禁前递归校验父证据 seal、artifact 字节、同 change/attempt/base/policy、阶段顺序和依赖完整性；篡改、缺依赖或跳阶段均拒收。存储前再次验证完整闭包，先保存依赖再以独占写发布结果；已有结果拒绝覆盖，相同内容寻址文件可复用。

控制器仍负责确认父证据确已接收、独立回放、接收顺序、run 独立性和授权；本地 hash seal 不等于控制器接收或签名，也不证明 holdout 隔离。
