# HANDOFF — 通用接手框架（公仓）

本仓只保存通用交接框架；目标进度、队列和机器信息在私仓。不要在本仓写 IP、token、私有路径或个人信息。

## 接手

1. 在已取得本仓和私仓的环境中设置 `PHANES_PRIVATE` 指向私仓；需要自动取得私仓时另设 `PHANES_PRIVATE_URL`。
2. 在本仓运行 `bash tools/ingest.sh`，以实时体检和输出的接手指令为准。
3. 依次读私有工作资料，再读 `handoff/ARCHITECTURE.md`、`handoff/ROLES.md` 和 `handoff/PROTOCOL.md`。角色与架构唯一以 `handoff/ARCHITECTURE.md` 为准。
4. 按架构 spec 和队列第一项未完成任务开展工作；派发与验收操作遵守 `handoff/ROLES.md`，授权与隔离规则遵守 `handoff/PROTOCOL.md`。

## 文件

| 文件 | 作用 |
|---|---|
| `handoff/ARCHITECTURE.md` | Phanes 唯一权威的详细架构 spec；架构变更在此更新并追加演进记录 |
| `handoff/ROLES.md` | 派发、验收与宿主等待操作；角色定义引用架构 spec |
| `handoff/PROTOCOL.md` | 状态汇报、授权、隔离和环境变更操作；门禁引用架构 spec |
| `tools/ingest.sh` | 接手体检与私仓入口 |
| `tools/review_run.py` | 受控测试执行与证据记录 |
| `tools/isolation_probe.sh` | Linux 主机隔离探针 |
