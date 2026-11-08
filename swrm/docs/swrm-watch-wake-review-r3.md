REPORT: blocked — 已修复 2 项 MAJOR 和 1 项 MINOR；相同前缀 copytruncate 后增长仍无法由现有读取端可靠识别。

| 评审项 | 结果 | 验证证据 |
| --- | --- | --- |
| 相同前缀截断后增长 | 未修复 | 隔离复现：消费 `ERROR old`，同 inode 截断写入 `old + new`，再次唤醒只输出 `ERROR: new`；`same_inode=True`、`grew=True`。 |
| STALL 永久去重 | 已修复 | `test_stall_rearms_after_activity_with_new_delivery_id`：活动后清除旧 STALL，持久化后重启可再次告警，新 episode 的 ID 不同，无新活动不重复告警。 |
| kqueue 轮转竞态 | 已修复 | `test_kqueue_open_rotation_race_backs_off_and_rediscovers_events`：注入 open 失败后退避，再发现轮转后的 ERROR；`test_kqueue_registration_race_closes_resources_and_backs_off`：注册失败关闭资源并退避。 |
| `--max-minutes 0` 与 `--wake` 不等价 | 已修复 | `test_cli_max_minutes_zero_matches_wake_for_errors_and_risks`：两种参数均支持 `--all`，忽略普通事件和心跳，ERROR/RISK 后 rc=0 退出。 |

未修复项的边界：读取端停机期间，普通追加与同 inode 截断后写回相同前缀再追加，均可呈现相同的 inode、内容、大小和 mtime/ctime 变化。现有 anchor 甚至全文件摘要也无法证明中间发生过截断。将所有增长视为新 generation 会重放普通追加前的事件，破坏“不重”要求。因此未采用该启发式修复。

下一步需由写入端保证 rename/create 轮转（更换 inode），或在每次截断时写入持久 generation 标识，然后由游标记录该标识。仅靠退出后不再运行的 watcher 无法记录停机期间的文件操作历史。本轮未扩大到写入协议改造。

验证命令（仅当前 worktree，临时目录隔离）：

```sh
mkdir -p .tmp/test-tmp .tmp/test-home
TMPDIR="$PWD/.tmp/test-tmp" OPENSWARM_HOME="$PWD/.tmp/test-home" \
  python3 -m unittest discover -s tests -p test_watch.py -v
```

结果：29 项通过，7.442 秒；包括真实 macOS kqueue 等待/CPU 检查，无跳过。未访问真实控制面，未读取 phanes，未提交或切换分支。带持久游标的大文件性能优化不在本轮四项修复范围内。
