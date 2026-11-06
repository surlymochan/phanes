#!/bin/bash
# 接手体检：打印当前事实，供新会话 ingest。对主机只读；私仓缺失会 clone，存在则 pull --ff-only（唯一的写动作）。几秒内结束。
# 用法：bash tools/ingest.sh
R="$(cd "$(dirname "$0")/.." && pwd)"
echo "== 时间/仓库 =="; date '+%F %T %Z'; (cd "$R" && git log --oneline -3 && git status --short | head -8)
echo; echo "== 主机连通（ssh 别名，3 秒超时；gocc 为控制面，govpn2/gores1/gores2 经 gocc 检）=="
chk() { # label, command...
  out=$("${@:2}" 2>&1 | tr '\n' ' ' | cut -c1-110); [ -n "$out" ] && echo "OK    $1: $out" || echo "FAIL  $1"; }
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
  (cd "$PRIV" && git pull -q --ff-only 2>/dev/null; echo "私仓：$PRIV  ($(git log --oneline -1))")
  echo; echo "== 工作队列（私仓 QUEUE.md 未完成项）=="; grep -n '^- \[ \]' "$PRIV/QUEUE.md" 2>/dev/null | head -10
  R="$(cd "$(dirname "$0")/.." && pwd)"
  cat <<EOF2

================ 接手指令（你现在是新统筹/主会话，照做，不要等批准）================
1. 依次读：$PRIV/HANDOFF-PRIVATE.md → STATE.md → QUEUE.md；再读 $R/handoff/ROLES.md、PROTOCOL.md。
2. 按 ROLES.md 自己拉起 builder / tester / ops 子会话（占位符对应路径见 HANDOFF-PRIVATE.md §5），然后从上面队列第一个未完成项开始推进。
3. 只有 PROTOCOL §5 的事（含队列 Q0：MiniMax 密钥转发）先问用户，其余自己决定、事后一句话汇报。
4. 等待用事件（tools/watch.sh、tools/msg.sh、openswarm watch），不要睡眠轮询。
=====================================================================================
EOF2
else
  echo "无法获取私仓：请向用户索取访问（surlymochan/phanes-handoff），先只按公仓框架工作。"
fi
