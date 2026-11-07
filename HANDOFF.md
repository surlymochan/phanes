# HANDOFF — 通用接手框架（公仓）

本仓只保存通用交接框架；目标进度、队列和机器信息在私仓。不要在本仓写 IP、token、私有路径或个人信息。

## 接手

1. 在已取得本仓和私仓的环境中设置 `PHANES_PRIVATE` 指向私仓；需要自动取得私仓时另设 `PHANES_PRIVATE_URL`。
2. 在本仓运行 `bash tools/ingest.sh`，以实时体检和输出的接手指令为准。
3. 依次读私仓 `HANDOFF-PRIVATE.md`、`STATE.md`、`QUEUE.md`，再读 `handoff/ROLES.md` 和 `handoff/PROTOCOL.md`。
4. Boss 兼 RSI，通过 swrm（命令名 `openswarm`）按需派 build lane 和独立 review lane；从队列第一项未完成任务开始，遵守 PROTOCOL §5 的授权边界。

## 文件

| 文件 | 作用 |
|---|---|
| `handoff/ROLES.md` | Boss 职责、lane 派发与宿主等待方式 |
| `handoff/PROTOCOL.md` | 协作、评审门禁、授权与隔离规则 |
| `tools/ingest.sh` | 接手体检与私仓入口 |
| `tools/review_run.py` | 受控测试执行与证据记录 |
| `tools/isolation_probe.sh` | Linux 主机隔离探针 |
