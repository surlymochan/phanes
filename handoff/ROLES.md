# ROLES — Boss 与按需 lane

## Boss

唯一常驻的 Boss 负责目标、优先级、队列、任务卡、依赖、证据核对和最终决策，兼任 RSI：分析失败、吞吐与成本，提出改进并用独立实验验证。巡检交给脚本，异常按需处理；对抗测试在 lane 中进行。

## 派发与验收

每项任务写清范围、验收标准、基线、预算和产物。swrm 的命令名是 `openswarm`；按需派一条 build lane 实现，再针对其固定提交派独立 review lane。两者分别使用任务卡；审查卡包含需求、验收标准和待审版本。

```bash
openswarm run --detach --cwd <repo> --base <base-commit> --verify '<test-command>' --task-file <build-card>
openswarm watch <run-id> --quiet
openswarm run --detach --cwd <repo> --base <build-commit> --verify '<review-check-command>' --task-file <review-card>
openswarm watch <review-run-id> --quiet
```

`--base` 固定每次运行的代码版本；有适用的测试命令时用 `--verify`，并核对实际执行结果。独立 review lane 调用 `tools/review_run.py` 和 evalid 留存固定版本、测试及行为差分证据；Linux 新主机进池前用 `tools/isolation_probe.sh`，Mac 按 PROTOCOL §6 执行。Boss 核对原始结果和证据，不能把派发成功当作验收通过。

BLOCKER 按 PROTOCOL §4 判断；修复后由独立审查方复核新版本，受影响的已审内容也须复核。审查与测试或 Spec 冲突时记录依据，由 Boss 决定；未解决的 BLOCKER 不放行。

## 等待与临时任务

- Claude Code：将 `openswarm watch <run-id> --quiet` 作为后台任务运行，用 Monitor 等待结果；到期后核对状态并按需重新等待。
- Codex 桌面版：在当前轮次使用宿主原生等待能力等待 watch；空闲时用定时心跳兜底，恢复后核对在途运行和原始结果。已结束的轮次不会自动继续等待。
- 不创建 bridge、信箱或 watchdog。巡检由脚本执行；只有研究、资料整理、结果汇总或局部排障等有界临时任务适合时，才按需使用宿主 subagent，不设置常驻角色。
