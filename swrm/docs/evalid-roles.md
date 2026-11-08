# evalid role adapter

`bin/evalid_roles.py` provides the C6 role profile, one-shot `swrm run` adapter, and `xflow role run` subprocess adapter. It is an opt-in library/CLI module; this card does not wire it into `bin/swrm` or resident lanes.

## Frozen profiles

| Role and stage | Backend | Model | Effort |
| --- | --- | --- | --- |
| `evalid-spec / spec` | `codex` | `gpt-6.1-sol` | `high` |
| `evalid-tdd / red` or `green`, even assignment index | `pi` | `MiniMax-M3.1-Flash` | unset |
| `evalid-tdd / red` or `green`, odd assignment index | `codex` | `gpt-6-luna` | `high` |
| `evalid-review / spec-check` or `delivery` | `codex` | `gpt-6.1-terra` | `high` |

The controller's single writer supplies the monotonically increasing first-round assignment index. Alternating even/odd indices keep cumulative bucket counts within one; red and green reuse the same index. If MiniMax is unavailable, only an even-index task can use `agy`, with its model supplied from the approved frozen policy. The profile records the planned MiniMax model and fallback reason; no Codex effort is passed to agy.

Quality repair uses `roles.repair_profile(0)` for one `gpt-6.1-sol` medium turn and `roles.repair_profile(1)` for one high turn. The next request raises `NeedsBoss` with `NEEDS-BOSS`. A conflicting backend, model or effort is rejected rather than silently treated as an ordinary task.

修复阶段由控制器通过 `repair_profile(round, stage="red")` 或 `stage="green"` 指定；为兼容现有调用，默认仍为 `green`。profile 只选择模型，不授予 green 权限；red 失败后的修复仍需控制器接收 red 并独立授权后续 green run。

## Adapter API

`resolve_role` accepts the controlled task-card front matter and optional lane role. It returns `None` when neither declares a role, preserving legacy dispatch, and rejects malformed/unknown roles, unsupported versions, conflicting declarations and malformed parent content addresses.

只要卡头出现任一受控字段（`evalid_version`、`role`、`change_id`、`input_evidence`），就必须包含合法 role 和完整声明；缺 role 时即使 lane 声明了角色也拒绝。完全没有受控声明的旧卡才可返回 `None`，或继承 lane 角色并由控制器补齐元数据。

`resolve_role` 先检查卡头是否出现受控字段；完全没有受控声明时跳过卡头解析，因此旧卡的 `tags: [1, 2]` 等元数据可直接保留旧路径，或继承 lane 角色。

有受控声明的卡头中，单行字符串列表按 YAML 语义解析，支持单引号（`''` 表示一个单引号）、双引号及其转义、未加引号的字符串、尾逗号和列表后的注释。例如 `input_evidence: ['sha256:…']` 与双引号形式等价。解析仍只用标准库，不支持嵌套集合、tag 或 alias；列表中的隐式数字、布尔值、null 仍不满足字符串列表合同。

`resolve_profile` returns the immutable backend/model/effort choice. `build_swrm_command` returns argv (never a shell string) for the existing `swrm run --task-file --base --verify` path, disables duplicate automatic peer review, and marks review runs `--read-only`. `run_swrm` requires a real run ID, terminal result, and actual backend/model/effort matching the profile; a queued/accepted response is not completion. Tests inject a mock runner so no model service is contacted.

`run_swrm` 接受执行终态 `done/partial/failed/stopped/budget_exceeded`，在解析并核对 JSON 后处理进程退出状态：零或非零退出的合法失败终态原样返回，保留实际 run ID、终态、verify、attempts 和失败证据；这不表示阶段通过。路由失败的 `unroutable` 回执也原样保留，其中没有实际模型字段时不声称已执行或已核验模型。非零退出却声称 `done`、缺 run ID、非终态、错误模型或无效 JSON 均拒绝。

### evalid 阶段门禁适配

```python
command = roles.build_evalid_command(
    "xflow", request_file="role-request.json", output_file="role-result.json")
result = roles.run_evalid(command, timeout=120)
```

该 API 以独立 argv 调用 `xflow role run --request ... --output ...`，不拼 shell、不启动模型、不自动推进阶段。request 由控制器冻结，按设计 §3.2 包含版本、角色、阶段、change/attempt/run、base/head、公开 inputs 和 policy digest；调用方负责提供正确执行目录、公开文件和实际 run ID，不传 holdout 材料。wrapper 负责 schema、证据和阶段校验。

返回 `{rc, artifact, stdout, stderr}`：`rc=0` 为阶段通过，`1` 为质量/证据不符，`2` 为配置/schema 错误，`3` 为基础设施失败。artifact 来自指定输出文件，不从 stdout 猜测；失败时允许无 artifact，并保留返回码和诊断。成功缺输出、非 JSON 对象、未知返回码、启动/超时异常均抛 `RoleError`，不得放行。输出必须是新文件且不能是符号链接，已有证据不能覆盖；request 和 output 不能指向同一文件。此处不替代控制器对 envelope、内容地址和最终放行公式的独立复验。

隔离验收（从 `swrm/` 执行）：

```sh
mkdir -p ../.tmp/test-tmp
TMPDIR="$PWD/../.tmp/test-tmp" python3 -m unittest discover -s tests -p test_evalid_roles.py
```

测试包含真实本地 stub 子进程的 0/1/2/3 退出和非零 swrm 终态回执，不访问真实模型或控制面。

Example profile query:

```sh
python3 bin/evalid_roles.py profile evalid-tdd red --assignment-index 0
python3 bin/evalid_roles.py profile evalid-tdd red --assignment-index 0 --minimax-unavailable --agy-model approved-model
```

The role adapter does not grant publish/merge permissions or receive holdout test material. Holdout execution and its private evidence remain in the Phanes control plane.
