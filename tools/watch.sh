#!/bin/bash
# 通用事件等待器：每隔 INTERVAL 秒执行一次 CMD，输出相对上一次有"新增行"就打印新增行并退出（退出即唤醒）。
# 用法：tools/watch.sh '<取状态的命令>' [INTERVAL=60] [STALL_SECONDS=2700]
# 例：  tools/watch.sh "ssh gocc 'tail -n 50 ~/phanes-ctl/rl4h/prod_chain.log'" 60
# 命令连续失败 3 次 -> 打印 WATCH-FAIL 并退出；STALL_SECONDS 内无变化 -> 打印 WATCH-STALL 并退出。
CMD="$1"; INTERVAL="${2:-60}"; STALL="${3:-2700}"
[ -z "$CMD" ] && { echo "usage: watch.sh '<cmd>' [interval] [stall_seconds]"; exit 2; }
prev=$(bash -c "$CMD" 2>/dev/null | sort); last=$(date +%s); fails=0
while true; do
  sleep "$INTERVAL"
  cur=$(bash -c "$CMD" 2>/dev/null); rc=$?
  if [ $rc -ne 0 ]; then
    fails=$((fails+1)); [ $fails -ge 3 ] && { echo "WATCH-FAIL: command failed 3 times"; exit 0; }; continue
  fi
  fails=0
  new=$(comm -13 <(echo "$prev") <(echo "$cur" | sort) | grep -v '^$')
  if [ -n "$new" ]; then echo "$new"; exit 0; fi
  [ $(( $(date +%s) - last )) -gt "$STALL" ] && { echo "WATCH-STALL: no change for ${STALL}s"; exit 0; }
done
