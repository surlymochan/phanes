# phanes 自动驾驶交接（2026-11-01 03:00 UTC+8）

**GitHub Project（主看板）：** https://github.com/users/surlymochan/projects/6  
**Epic：** https://github.com/surlymochan/phanes/issues/223  
**双模式盘点：** `brain/dual-mode-status.md`

两个常驻循环跑在 box 上（setsid，退出会话也不停；box 没有 cron，**box 重启后需手动启动**，见下方“启动”）。

| 循环 | 监督进程 | 工作进程 | 作用 |
|---|---|---|---|
| 派活 | `~/phanes-ctl/bin/lane-autodispatch` | `autodispatch.py`（每 60s 一 tick） | 空闲 lane 从 `lane-ready` 队列派活；优先匹配 `backend:*`，否则派无 backend 标签的 issue；完成后推 `fleet/<L>-issue<N>-<run>` 并开 PR（Closes #N） |
| 合并 | `~/phanes-ctl/bin/lane-merge` | `merge-loop.sh`（每 60s） | fleet/* 与 brain/* PR 在 Linux 检查全绿且 MERGEABLE 时 squash 合并（不等 macOS）；main 上 org-health 变化时自动发布 demo |

## 双模式怎么用

### 1) SSH fleet（受控主机，默认）
1. 建 issue：标签 `lane-ready` + `p0|p1|p2`，可选 `backend:pi|agy|codex`。
2. 正文可写 `依赖：#a #b` 与 `验收：\`命令\``。
3. `lane-autodispatch` 自动 SSH 到对应主机 `openswarm-fleet lane assign`。
4. 看状态：`~/phanes-ctl/bin/lane-autodispatch-status` 或 `lanes-status.json`。

当前拓扑（目标 ≥10，实测可达 11）：M1–M4 pi@ib1–4；A1 agy@gocc；A2 agy@gorelay；CX1/CX3 codex@ib5；CX2/CX4 codex@govpn2；CX5 codex@govpn（内存紧，盯 swap）。**禁止 Copilot。** 控制面在 **box**，不依赖用户 Mac。

### 2) GitHub issue bus（非受控执行器）
协议见仓库 `openswarm/docs/BUS.md`。落地与冒烟任务：#225。概要：
```sh
openswarm bus init <bus-owner>/<bus-repo>
openswarm run --backend github-issue --cwd <target-repo> --base <sha> --task-file task.md
openswarm bus-worker --bus <bus-repo> --name <executor> --backend codex --once
```
受控主机请继续用 SSH fleet，不要改走 bus。

## 看状态 / 加任务 / 停启
- 状态：`~/phanes-ctl/bin/lane-autodispatch-status`；日志 `autodispatch.log` / `merge.log`；心跳超过 3 分钟未更新即异常。
- 加任务：issue 打 `lane-ready`，或丢 `~/phanes-ctl/autodispatch/backlog/<名>.md`（可派 <10 时自动建 issue）。
- 暂停：`touch ~/phanes-ctl/autodispatch/PAUSE` 或 `PAUSE-<LANE>` / `PAUSE-MERGE`。
- 启动（box 重启后）：
```
cd ~ && setsid nohup ~/phanes-ctl/bin/lane-autodispatch >/dev/null 2>&1 < /dev/null &
cd ~ && setsid nohup ~/phanes-ctl/bin/lane-merge >/dev/null 2>&1 < /dev/null &
```

## demo
https://bots.surlymo.cn/phanes-demo/ （basic auth 在保险库 bots.surlymo.cn）。手动：`~/phanes-ctl/bin/deploy-demo`。

## 账号 / 登录
缺登录或额度 → 私信 **主理人**，本 bot 不自助登录。agy 429 → `PAUSE-A1`/`PAUSE-A2`，勿硬重试。
