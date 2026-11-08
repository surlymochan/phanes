REPORT: ready — 修复包装命令风险绕过、lane 审批响应跳过和 ledger scope 丢失；新增回归通过，聚焦对比仅有两项基线既有错误。

# host scope 第二次退回修复验证

验证日期：2026-11-08。当前源码起点为 `9e0df323dca155afee08772b904d72f3fc29a8d7`，
比较基线固定为任务指定的 `a4ff878970ad3978058ee47cc62af6851dbae9e1`，未使用 `origin/main` 代替基线。
未提交、推送或切换工作分支。

## 评审项与回归证据

| 评审项 | 修复 | 测试证据（`tests/test_host_scope.py`） |
| --- | --- | --- |
| BLOCKER：`sudo` 包装的内层强制风险未检查 | 逐层识别 `sudo`（含用户选项）、`env`、`nohup`、`timeout`、`nice`、`command`、`exec` 等前缀及其选项，内层命令继续经过全部风险规则；保留外层 sudo 告警 | `test_wrapped_commands_keep_all_inner_enforcement`：15 种前缀 × push / secret print / publish，并验证仅开启内层风险规则时仍阻断；包含嵌套 shell、引号文本负例 |
| 同上：运行时阻断与告警 | 假执行器只输出命令事件，不执行命令文本 | `test_fake_executor_records_warnings_and_stops_wrapped_enforced_risks`：`sudo git push origin main`、`sudo cat .env`、`sudo npm publish` 返回 133 且保存对应 `risk_blocks`；sudo 运维和外部 rm 返回 0 并保存 `risk_warning` |
| 同上：rm 边界和默认强制行为 | 内层 rm 仍经过路径检查；host 降级告警，repo 保持强制；包装器的工作目录只作用于本条命令 | `test_wrapped_rm_and_sudo_remain_host_warnings`、`test_wrapper_cwd_does_not_change_later_shell_commands` |
| MAJOR：lane 告警后 `continue` 跳过审批响应 | 记录告警后继续正常协议处理；强制风险仍中止 | `test_host_warnings_still_answer_lane_permission_requests`：sudo / 外部 rm × ACP 权限请求 / Codex 两种审批请求；下一次读取前必须已回复，并能收到完成事件；ACP 保留原选项选择，Codex 保留原 decline / abort 响应 |
| MAJOR：实际 ledger 写入缺 scope | run 和 lane 完成记录写入 scope；相关失败记录同样补齐 | `test_run_and_lane_write_scope_through_ledger_to_daily_report`：实际调用 `cmd_run`、`lane_finalize_run` 和 `append_ledger`，读取落盘的 ledger / status / result，再调用 `daily_report` 验证 host 和省略参数时的 repo 默认值；没有手工构造带 scope 的 ledger |
| 验证记录无法独立核验 | 撤下旧记录中无法复核的全量对比结论；本次记录限定为实际执行的聚焦测试 | [机器可读结果](host-scope-review-results.json) 保存测试数量、错误 ID、traceback、跳过原因、源码摘要和明确的比较基线；测试用例已纳入仓库 |

原有默认 repo 提示、host 提示、分号组合风险、report 显示、每日 JSON/HTML、batch scope 透传、
fleet 参数透传和 CLI golden 测试均在本次聚焦测试中执行。README.md、SKILL.md 同步说明包装命令仍阻断、
lane 告警继续响应和 ledger scope。

## 本次实际结果

| 版本 / 测试范围 | 测试数 | 通过 | failures | errors | skips |
| --- | ---: | ---: | ---: | ---: | ---: |
| 本次新增回归（包含在下一行） | 6 | 6 | 0 | 0 | 0 |
| 当前修复，7 个聚焦模块 | 91 | 88 | 0 | 2 | 1 |
| a4ff878 + 相同隔离 helper，6 个聚焦模块 | 81 | 78 | 0 | 2 | 1 |

两侧共同错误均为 `TypeError: 'NoneType' object is not subscriptable`：

- `test_guardrails.GuardrailsTests.test_enforced_risks_all_protocols_and_policy_opt_out`
- `test_guardrails.GuardrailsTests.test_rm_block_records_only_path_class`

这些测试的临时目录未建立独立 Git 根；当 TMPDIR 位于当前 worktree 内时，路径分类会找到外层仓库。
本次没有扩大修改这些基线测试。错误集合新增 `[]`、消失 `[]`；这只是聚焦比较，不代表全量测试通过。
两侧均跳过 macOS 上不适用的非 macOS Keychain 用例。

## 复现与隔离

```sh
mkdir -p .tmp/test-tmp
TMPDIR="$PWD/.tmp/test-tmp" PYTHONPATH=tests python3 -m unittest test_host_scope -v
TMPDIR="$PWD/.tmp/test-tmp" PYTHONPATH=tests python3 -m unittest \
  test_host_scope test_guardrails test_workspace test_ledger test_cli_golden test_batch test_fleet -v
```

基线通过 `git archive a4ff878` 解包到 `.tmp/host-scope-baseline`，只复制当前 `tests/_load.py`，
以相同的独立 OPENSWARM_HOME、fleet guard 和 capacity 路径隔离运行；业务代码与断言未改。
使用同一个 worktree 内 TMPDIR，通过基线 tests 目录加载上述模块（不含尚未存在的 `test_host_scope`）。

本轮逐项日志和 runner 存在于 `.tmp/host-scope-{current,baseline}-suite.log`、
`.tmp/run-host-scope-suite.py`；修复前失败日志为 `.tmp/host-scope-red.log`。
这些是忽略的临时产物，持久结果以本页和仓库中的 JSON 为准，不依赖临时文件继续存在。
未运行全量 discovery；lane 使用进程内假 child 验证真实协议处理函数，未启动 supervisor 或真实控制面。
所有风险命令均只作为事件文本。`git diff --check` 和 Python 编译检查通过。
