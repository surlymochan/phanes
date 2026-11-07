# ROLES — 派发与验收操作

角色边界、执行模型、并发约束及目标流程以 [ARCHITECTURE.md §1–§2](ARCHITECTURE.md) 为唯一准则；本文件只保留操作规则。

## 派发

任务卡写明范围、验收标准、基线、预算和产物。swrm 的命令名是 `openswarm`；按需派发一次性 run 或常驻 lane。独立审查必须针对作者固定提交，并使用单独任务卡。

```bash
openswarm run --detach --cwd <repo> --base <base-commit> --verify '<test-command>' --task-file <build-card>
openswarm watch <run-id> --quiet
openswarm run --detach --cwd <repo> --base <build-commit> --verify '<review-check-command>' --task-file <review-card>
openswarm watch <review-run-id> --quiet
```

`--base` 固定运行代码版本；存在适用检查时配置 `--verify` 并核对实际执行结果。独立 Reviewer 可调用 `tools/review_run.py` 和 evalid 保存固定版本、测试与行为差分证据。Linux 执行机加入执行池前运行 `tools/isolation_probe.sh`；隔离要求和实验真值见 [ARCHITECTURE.md §2.4](ARCHITECTURE.md)。派发成功不代表验收通过。

BLOCKER 与 FOLLOW-UP 按 [ARCHITECTURE.md §1.4](ARCHITECTURE.md) 分级。修复后由独立审查方复核新版本；审查与测试或 Spec 冲突时记录依据，交 Router 裁决。未解决的 BLOCKER 不放行。

## 等待与临时任务

- Claude Code：将 `openswarm watch <run-id> --quiet` 作为后台任务运行，用 Monitor 等待；到期后核对状态，按需重新等待。
- Codex 桌面版：在当前轮次使用宿主原生等待能力等待 watch；空闲时可用定时心跳兜底，恢复后核对在途运行和原始结果。已结束的轮次不会自动继续等待。
- 临时 subagent 只承担有界研究、资料整理、结果汇总或局部排障；不建立常驻替代角色。
