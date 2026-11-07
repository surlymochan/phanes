# swrm watch --wake 第二轮修复证据

REPORT: ready — 三项退回问题均有回归覆盖；事件写入 stdout 并 flush 后才提交游标。

范围依据本轮任务卡内附的原要求和两轮评审全文，以及当前目录内的代码、测试和第一轮证据。
未读取目录外的原始任务卡或评审，未提交、推送或切换分支。

## 逐项结果

| 问题 | 修复 | 实测证据（`tests/test_watch.py`） |
| --- | --- | --- |
| BLOCKER：保存后、打印前终止会永久丢通知 | `emit` 先 `print(..., flush=True)`，成功后才原子保存；stdout 写入/flush 失败直接退出，避免被日志读取的 `OSError` 处理吞掉 | `test_cli_sigkill_at_delivery_boundaries_never_loses_event_or_done`；`test_failed_stdout_flush_does_not_checkpoint` |
| 重复投递须可去重 | 每行包含 `id=<sha256>`。文件事件 ID 由 run、设备/inode、文件代次、起始偏移与原始行摘要决定；状态通知 ID 由 run、类别和通知内容决定 | `test_checkpoint_failure_replays_flushed_event_with_same_id_and_cleans_temp`；`test_identical_event_lines_have_distinct_ids_at_distinct_offsets`；`test_state_alerts_replay_after_output_interruption_with_same_id` |
| MAJOR：同 inode、相同内容截断重写漏报 | 记录已观察文件大小和纳秒 mtime；无增长且 mtime 改变时保守回退到零。先持久化回退位置及新代次，随后才读、输出新事件；这一步不消费任何待输出事件 | `test_same_content_truncation_has_new_generation_but_retry_id_is_stable` 验证 inode 不变、同内容重写可唤醒、新代次 ID 不同，且中断重试保持新 ID；`test_legacy_cursor_is_upgraded_even_without_new_events` 验证旧游标补齐元数据 |
| MAJOR：Cursor 单次运行 `tool_call` 风险命令被消费 | 将原 `WatchMessages` 的命令提取移到共享 `summarize_event`，wake 和默认 watch 使用相同规则 | `test_cursor_one_shot_tool_calls_wake_for_risk` 覆盖顶层 command、嵌套 command 和自定义风险；原默认消息测试补充两种命令格式 |

## 真实崩溃注入

子进程调用真实 CLI `main`；测试只替换输出/原子替换边界，以 `SIGKILL` 终止自身。
ERROR 和 DONE 分别覆盖下面四种情况，共 8 个子场景，均通过。

| 杀死位置 | 管道输出 | 重启行为 |
| --- | --- | --- |
| 打印前 | 无 | 重新输出第一条 ERROR / DONE |
| stdout flush 后 | 已有完整事件行 | 重放相同 ID |
| 游标原子替换前（临时文件已写入并 fsync） | 已有完整事件行 | 重放相同 ID |
| 游标原子替换后 | 已有完整事件行 | ERROR 前进到下一条；已报告 DONE 不再输出 |

另有 stdout flush 抛错、磁盘保存失败、输出时 KeyboardInterrupt 的测试。
状态通知 DONE / EXITED / BUDGET / STALL 均验证中断后以相同 ID 重放。
这提供 at-least-once 输出；消费方负责按 ID 去重，flush 不代表消费方已经处理事件。

## 验证

所有测试设置 `TMPDIR="$PWD/.tmp/test-tmp"`，`OPENSWARM_HOME` 指向临时目录。
`tests/_load.py` 进一步将测试 HOME 放进 TMPDIR 下自动清理的隔离目录；CLI 子进程使用对应 fixture HOME。

```sh
mkdir -p .tmp/test-tmp .tmp/test-home
TMPDIR="$PWD/.tmp/test-tmp" OPENSWARM_HOME="$PWD/.tmp/test-home" \
  python3 -m unittest discover -s tests -p test_watch.py -v
TMPDIR="$PWD/.tmp/test-tmp" OPENSWARM_HOME="$PWD/.tmp/test-home" PYTHONPATH=tests \
  python3 - <<'PY'
import contextlib
import unittest
from unittest import mock
import test_agy, test_cursor, test_cli_golden
modules = (test_agy, test_cursor, test_cli_golden)
with contextlib.ExitStack() as stack:
    for module in modules:
        stack.enter_context(mock.patch.object(module.xs, 'fleet_guard_command', return_value=None))
    suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromModule(module) for module in modules)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
raise SystemExit(not result.wasSuccessful())
PY
git diff --check
```

- 修复前：新增的 4 项针对性测试均失败，复现保存/输出顺序、相同内容重写和 Cursor RISK 漏报。
- 修复后：watch **25/25 通过**，约 6.97 秒；真实 macOS kqueue 等待测试实际执行，未跳过。
- agy / Cursor / CLI golden **21/21 通过**，约 16.27 秒；使用 fake 后端并屏蔽全局 fleet guard。
- 保留并通过第一轮的 `--all`、DONE 游标、并发锁、规范化 error、轮转、半行、`--from-end`、大文件增量读取和空闲 CPU 测试。
- `git diff --check` 通过。未运行全仓测试；默认 watch 的现有用例通过，不宣称穷尽所有默认路径。

## 文件协议边界

同大小文件仅修改 mtime 也会保守重放。普通追加只读取新增行，改名轮转保持原代次和进度。
文件快照无法区分“保留所有旧字节的追加”与“截断后重写完全相同旧前缀并增长”，
也无法恢复在读取前已被覆盖的日志；需要覆盖此类写入时，写入方应使用改名轮转或显式代次。
本轮的相同内容、相同大小重写，以及已有缩短/改变锚点后的增长回归均已验证。
