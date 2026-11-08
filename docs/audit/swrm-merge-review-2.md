REPORT: ready — 本轮三项 MAJOR 已修复并完成隔离回归；完整门禁的两项既有 guardrails 错误在未修改 HEAD 副本复现，不宣称全量通过。

# review-swrm-merge2 修复证据

| 评审项 | 修复 | 实际证据 |
| --- | --- | --- |
| MAJOR 1：旧目录内四个被跟踪 `.so` | 删除全部四个二进制及空目录；去掉仅针对旧测试目录的忽略规则，新测试目录忽略规则保留。 | `git diff --name-only --diff-filter=D -- openswarm/` 列出四项；`test ! -e openswarm` 成功。未暂存、未提交，因此 `git ls-files openswarm` 仍会列出索引中的待删除记录，不能单独用它判断工作树是否还存在旧文件。 |
| MAJOR 2：远端状态读写硬编码新目录 | `REMOTE_STATUS` 和 `RUN_RECORD_SNIPPET` 共用远端目录解析代码；优先 `OPENSWARM_HOME`，其次 `XSWARM_HOME`，再识别新旧已有目录，不移动或合并状态。 | `test_autodispatch_state_home.py` 修复前 5 项失败，修复后 5 项通过；实际执行两段远端 Python 代码，检查 lane/run 读取和 `pr-validation.json`、日志写入同一目录。自定义含空格路径、环境键优先级、新旧目录和显式缺失目录均覆盖。 |
| MAJOR 3：非归档待办仍用已删除路径 | 更新 `docs/backlog/next/` 中 79 张活动卡片及两张 `tools/cards/` 卡片，目录、CLI 和名称改为 swrm。保留审计、历史回执及兼容接口的原始名称。 | `git grep -in openswarm -- docs/backlog/next tools/cards` 无匹配；评审点名的卡片现用 `python3 swrm/tests/test_patterns.py`，实际 6 项通过。 |

## 验证环境与结果

所有测试临时文件使用 worktree 内 `.tmp/test-tmp`；测试 HOME、状态和控制面配置使用
`.tmp/` 下的隔离目录。没有执行 SSH、真实派发、发布、状态迁移或本仓库的提交、推送、分支切换。

| 命令 | 结果 |
| --- | --- |
| `python3 -m unittest discover -s tools/tests -p 'test_*.py' -v` | 151 项通过。 |
| `python3 -m unittest discover -s tools/lane-autodispatch -p 'test_*.py' -v` | 33 项通过。 |
| `python3 -m unittest discover -s tools/tests -p test_lane_pr_validation.py -v` | 22 项通过；在简化夹具环境配置后再次运行。 |
| `python3 swrm/tests/test_patterns.py` | 6 项通过；对应评审点名的待办验收路径。 |
| `swrm/bin/swrm status`、`swrm/bin/swrm route --type impl --difficulty 3`（以 python3 执行） | demo-state 隔离环境离线 smoke 通过。 |
| `BASE_SHA=HEAD bash tools/ci-swrm.sh` | 退出 1：415 项，2 errors、3 skipped；两项错误均为下述已复现的 guardrails 问题。脚本在此停止，后续工具测试及 smoke 已独立运行。 |
| `git diff --check` | 通过。 |

完整门禁与未修改基线的原始日志保存在忽略目录 `.tmp/`，不纳入公开仓库。

## 既有问题与边界

- `swrm/` 本轮无改动。未修改 HEAD 的 `swrm/` 树通过 `git archive HEAD swrm`
  导出到 `.tmp/review-baseline`，用相同 TMPDIR、隔离 HOME/状态运行 guardrails 15 项，
  复现两项 `TypeError: 'NoneType' object is not subscriptable`：
  `test_enforced_risks_all_protocols_and_policy_opt_out`、`test_rm_block_records_only_path_class`。
- 活动卡片仅修复命名与目录引用，没有执行卡片所要求的新功能或重新核定历史行号。
  上游删除的 org-diagnosis 示例及部分旧测试文件也没有恢复；有些卡片本身要求新建测试文件。
  后续领取这些任务时仍需核对当前上游 API 和验收范围。
- 状态目录的选择顺序、环境变量须设置在远端，以及多目录并存时的显式配置要求，
  已记录在 [部署说明](../../tools/deployment-scripts.md#状态目录兼容)。没有访问真实旧控制面，
  真实部署仍需运维验证环境变量是否进入 SSH 进程。
- 未访问 worktree 外的上游仓库，不对上游树或完整上游测试差异作新结论。
  `handoff/ARCHITECTURE.md` 在当前 worktree 不存在，未修改；Boss 需在其实际所在处核对
  openswarm → swrm 的目录、CLI、CI 路径，并保留旧状态目录/环境键兼容说明。
