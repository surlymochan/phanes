# swrm-watch-wake 第一轮修复证据

第一轮历史记录；其中“先持久化再输出”的实现已被第二轮撤销。当前语义与验证见
[第二轮修复证据](swrm-watch-wake-review-r2.md)。

所有测试的临时数据均在当前 worktree 内；不提交、不推送、不切换分支。

## 复现与验证命令

```sh
mkdir -p .tmp/test-tmp
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

`tests/_load.py` 会将 `OPENSWARM_HOME` 再设置为 `TMPDIR` 下自动清理的隔离目录。
并发 watch CLI 测试使用各自 fixture 的 `OPENSWARM_HOME`。
补充的后端测试用上述 harness 明确屏蔽全局 fleet guard。
首次直接运行后端测试时，在 Cursor 的 provider concurrency 等待处中止（退出码 130），
该次未完成，不计为通过；临时 HOME 本身不能隔离默认全局 guard，故使用 harness 重跑。

验证结果：watch **18/18 通过**；隔离全局 guard 后的 agy / Cursor / CLI golden
**21/21 通过**；`git diff --check` 通过。真实 Darwin kqueue 对空文件等待 4 次，
另行测得墙钟 **4.0083 秒**、进程 CPU **0.0003 秒**。

## 逐项证据

以下测试名均位于 `tests/test_watch.py` 的 `WakeRegressionTests`。

| 评审问题 | 修复 | 回归证据 |
| --- | --- | --- |
| BLOCKER：`--all` 缺少 pathlib 导入 | 使用已导入的 `os.listdir` 和 `os.path.isdir` | `test_all_checkpoints_done_and_reaches_other_run_on_restart` 实际遍历存在的 runs 目录；`test_all_discovers_runs_created_while_waiting` 验证等待后新增 run |
| BLOCKER：kqueue 忙等 | `kqueue.control` 的 `max_events` 改为 1 | `test_real_kqueue_existing_file_waits_without_busy_spin` 在 macOS 对真实存在文件连续等待 4 次，断言墙钟至少 3.6 秒且进程 CPU 小于 0.2 秒；本次在 Darwin 实际运行，未跳过 |
| MAJOR：DONE 不持久化、饿死其他 run | 游标保存已报告状态；重启跳过已报告 DONE 并继续扫描 | `test_all_checkpoints_done_and_reaches_other_run_on_restart`：第一次 DONE、第二次另一 run 的 ERROR、第三次进入等待，无重复输出 |
| MAJOR：同 inode 截断漏报 | 同时检查文件大小与游标前 64 字节的 SHA-256 | `test_same_inode_truncation_and_regrowth`：确认 inode 不变，分别覆盖缩短和截断后增长超过旧 offset |
| MAJOR：并发、固定临时文件、投递顺序 | 独占锁、唯一临时文件、fsync、原子替换保留；先持久化再输出会漏报，已于第二轮改为 stdout flush 后提交 | 并发测试保留；原先要求中断后不重放的两项测试已改为验证 at-least-once 与稳定事件 ID，见第二轮证据 |
| MAJOR：忽略规范化 error | 处理 `summarize_event` 的 `error` 返回值 | `test_normalized_backend_errors_are_not_consumed_silently` 顺序验证 JSON-RPC `turn/failed`、`error`、agy ERROR 和 INVALID，全部逐次唤醒 |
| MINOR：`--from-end` 与覆盖不足 | 未建游标的文件从末尾开始，已有游标优先；文件身份跨路径保持进度，兼容旧路径游标 | `test_from_end_skips_history_but_preserves_saved_progress`；`test_rotation_and_incomplete_line` 验证改名轮转且等待半行补全；`test_legacy_path_cursor_migrates_before_file_is_renamed` 验证旧格式迁移后轮转不重放；其余测试覆盖 `--all`、并发和真实 kqueue |

## 投递边界（第二轮修正）

先写 stdout 并 flush，成功后才提交对应事件偏移或状态通知。输出失败保持待投递；
flush 后、提交前中断会重放相同 ID，由消费方去重。保存失败抛错，也允许重放。
共享同一 consumer 的第二个 watcher 会等待第一个退出后才加载最新游标。
