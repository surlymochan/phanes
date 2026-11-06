#!/bin/bash
# 接手体检：打印当前事实，供新会话 ingest。对主机只读；私仓缺失会 clone，存在则 pull --ff-only（唯一的写动作）。几秒内结束。
# 用法：bash tools/ingest.sh
R="$(cd "$(dirname "$0")/.." && pwd)"
echo "== 时间/仓库 =="; date '+%F %T %Z'; (cd "$R" && git log --oneline -3 && git status --short | head -8)
echo; echo "== 主机连通（ssh 别名，3 秒超时；gocc 为控制面，govpn2/gores1/gores2 经 gocc 检）=="
chk() { # label, command...
  out=$("${@:2}" 2>&1); rc=$?
  out=$(printf '%s' "$out" | tr '\n' ' ' | cut -c1-110)
  if [ "$rc" -eq 0 ] && [ -n "$out" ]; then echo "OK    $1: $out"; else echo "FAIL  $1: $out"; fi
}
SSH="ssh -o ConnectTimeout=5 -o BatchMode=yes -o ControlMaster=no"
chk gocc    $SSH gocc "uptime | sed 's/.*load/load/'"
chk gorelay $SSH gorelay "uptime | sed 's/.*load/load/'; df -h / | tail -1 | awk '{print \"disk\",\$5}'"
chk govpn   $SSH govpn "uptime | sed 's/.*load/load/'; free -m | awk 'NR==2{print \"mem_avail\",\$7\"M\"}'"
for h in govpn2 gores1 gores2; do
  chk $h $SSH gocc "$SSH $h 'uptime | sed \"s/.*load/load/\"; free -m | awk \"NR==2{print \\\"mem_avail\\\",\\\$7\\\"M\\\"}\"'"
done
echo; echo "== gocc 最近实验日志（~/phanes-ctl/rl4h）=="
$SSH gocc 'cd ~/phanes-ctl/rl4h 2>/dev/null && ls -t *.log | head -4 && tail -n 3 "$(ls -t *.log | head -1)" | cut -c1-240' 2>&1 | head -12
PRIV="${PHANES_PRIVATE:-$HOME/workspace/phanes-handoff}"
echo; echo "== 私仓（个性化交接）=="
if [ ! -d "$PRIV/.git" ]; then
  echo "私仓不在 $PRIV，尝试 clone（需要 gh 登录）..."
  git clone -q https://github.com/surlymochan/phanes-handoff "$PRIV" 2>&1 | tail -1
fi
if [ -d "$PRIV/.git" ]; then
  (cd "$PRIV" && if ! git pull -q --ff-only 2>/dev/null; then echo "私仓拉取失败；以下内容来自本地，请核对网络、权限及未提交改动。"; fi; echo "私仓：$PRIV  ($(git log --oneline -1))")
  echo; echo "== 工作队列（私仓 QUEUE.md 未完成项）=="; grep -n '^- \[ \]' "$PRIV/QUEUE.md" 2>/dev/null | head -10
  R="$(cd "$(dirname "$0")/.." && pwd)"
  cat <<EOF2

================ 接手指令（你现在是新统筹/主会话，照做，不要等批准）================
1. 依次读：$PRIV/HANDOFF-PRIVATE.md → STATE.md → QUEUE.md；再读 $R/handoff/ROLES.md、PROTOCOL.md。
2. 按 ROLES.md 由 Boss 兼任 RSI，按任务需要派出并复用 subagent；不调用平级角色聊天。从队列第一个未完成项开始，遵守用户对暂停测试的最新指示。
3. 只有 PROTOCOL §5 的事（含队列 Q0：MiniMax 密钥转发）先问用户，其余自己决定、事后一句话汇报。
4. swrm 任务派出后立即挂 openswarm watch；到期重挂，按原始结果处理。subagent 完成使用宿主原生通知；不要调用平级聊天 wait_threads 或启用重复 bridge。
=====================================================================================
EOF2
else
  echo "无法获取私仓：请向用户索取访问（surlymochan/phanes-handoff），先只按公仓框架工作。"
fi
