# 双模式盘点（2026-11-01 03:00 UTC+8）

Project：https://github.com/users/surlymochan/projects/6

## 模式 A — SSH fleet（受控主机）

| 项 | 状态 |
|---|---|
| box → 远程 SSH（phanes_ctl key + ControlMaster） | ✅ 已通 ib1–5 / gocc / gorelay / govpn / govpn2 |
| openswarm-fleet lane assign / status | ✅ lane-autodispatch 在用 |
| 10+ 路真实并行 | ✅ 02:59 起 11/11 busy（含 CX5 govpn） |
| Hub App ↔ bridge ↔ fleet（Mac 无关） | ❌ Hub bridge 仍默认 `~/.claude/openswarm-hub-bridge` + `openswarm-hub`→gores1；文档/配置迁移见 #224 #226 #230 |
| coordinator 在 gocc | ⚠️ 可用；gorelay↔gocc 高峰 banner timeout 有退避（#233） |

入口：`openswarm-fleet lane assign <L> --task-file …`（经 box SSH），或本机 `lane-autodispatch` 读 `lane-ready` issue。

## 模式 B — GitHub issue bus（非受控执行器）

| 项 | 状态 |
|---|---|
| 协议与代码（`openswarm bus` / `bus-worker` / `run --backend github-issue`） | ✅ 在仓库 `openswarm/docs/BUS.md` |
| bus 仓 init + policy.local 落地 | ❌ 未在本环境跑通（#225） |
| claim/digest/cancel 回归 | 🔧 #227 |
| 与 phanes Project / lane-ready 标签体系衔接说明 | 🔧 backlog rsi-08 / #238 一带；标签↔看板 Status 对应建议见 [`docs/project-phanes-rsi.md`](../docs/project-phanes-rsi.md) |

说明：当前 **lane-ready → autodispatch → PR** 是「用 GitHub issue 做任务队列」的受控闭环，**不是** openswarm 的 `github-issue` bus 协议。两者都要稳定：前者喂受控 SSH lane；后者给外部/他人 agent。

## RSI / 异构路由

| 项 | 状态 |
|---|---|
| 多 backend（pi/agy/codex）同池 | ✅ |
| 权重路由默认只记录、对比后启用 | 文档+门控 #228 |
| Copilot | 🚫 已禁用 |

## 阻塞

1. Hub 正式 UI 派发仍绑 Mac 路径（#224）——控制面已在 box，缺 bridge 配置与文档落地。
2. Issue bus 未做真实 init/冒烟（#225）。
3. A1/A2 共用 nicknight0616 额度；429 时 PAUSE，找主理人换号。
4. govpn CX5 内存紧（2G），可跑但需盯 swap（#229）。
