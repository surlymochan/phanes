# HANDOFF — 通用接手框架（公仓）

> 本仓只放**通用**的交接框架：角色模板、协作协议、工具脚本。个性化状态（目标进度、队列、规则、机器、路径、实验报告）在**私有仓库** `phanes-handoff`。不要在本仓写 token、IP、私有链接、PII。

## 一条命令起步（新机器/新会话）

```bash
git clone https://github.com/surlymochan/phanes ~/workspace/phanes 2>/dev/null; cd ~/workspace/phanes && git pull -q --ff-only; bash tools/ingest.sh
```

它会体检、自动取得私仓并打印入口。之后按下面的流程走。

## 接手流程（新 AI 会话 = 主会话/统筹）

1. 取得私仓：`git clone https://github.com/surlymochan/phanes-handoff ~/workspace/phanes-handoff`（需要该账号的 gh 登录；没有权限就向用户索取）。
2. 在本仓运行 `bash tools/ingest.sh`：打印实时体检 + 私仓入口摘要 + 未完成队列。以实时输出为准。
3. 读私仓 `HANDOFF-PRIVATE.md` → `STATE.md` → `QUEUE.md`（目标、事实、下一步）。
4. 读 `handoff/ROLES.md` 自己拉起子会话（builder/tester/ops/rsi），读 `handoff/PROTOCOL.md` 了解协作、事件触发和授权规则。
5. 从队列第一个未完成项开始推进，不等批准（授权边界见 PROTOCOL §5）。

## 文件

| 文件 | 作用 |
|---|---|
| `handoff/ROLES.md` | 角色职责、可复制的子会话提示词、不同宿主的拉起方式 |
| `handoff/PROTOCOL.md` | 少防御原则、星型协作、事件触发与定时、评审门禁分档、授权规则、变更记录格式 |
| `tools/ingest.sh` | 接手体检（含私仓定位/拉取） |
| `tools/watch.sh` | 通用事件等待器：输出一变就退出，免轮询 |
| `tools/msg.sh` | 共享信箱（宿主无 session 工具时的角色间通信） |

私仓位置：环境变量 `PHANES_PRIVATE`，默认 `~/workspace/phanes-handoff`。
