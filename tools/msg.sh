#!/bin/bash
# 共享信箱（宿主无 session 工具时用）。目录默认 ~/phanes-comm，每个角色一个 inbox 文件，追加写。
#   msg.sh send <to> "<text>"   追加一行 "<时间> <from>: <text>"（from 取 $ROLE，默认 anon）
#   msg.sh wait <role> [stall]  阻塞到 inbox 出现新行，打印新行后退出；stall 秒无消息则退出（默认 3600）
#   msg.sh read <role>          打印全部
D="${PHANES_COMM:-$HOME/phanes-comm}"; mkdir -p "$D"
case "$1" in
  send) printf '%s %s: %s\n' "$(date +%H:%M:%S)" "${ROLE:-anon}" "$3" >> "$D/$2.inbox" ;;
  read) cat "$D/$2.inbox" 2>/dev/null ;;
  wait) f="$D/$2.inbox"; touch "$f"; n=$(wc -l < "$f"); t=$(date +%s); stall="${3:-3600}"
        while true; do
          sleep 3; m=$(wc -l < "$f")
          [ "$m" -gt "$n" ] && { tail -n $((m-n)) "$f"; exit 0; }
          [ $(( $(date +%s) - t )) -gt "$stall" ] && { echo "MSG-STALL"; exit 0; }
        done ;;
  *) echo "usage: msg.sh send <to> <text> | wait <role> [stall] | read <role>"; exit 2 ;;
esac
