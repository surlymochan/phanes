# Phanes CI 复核

基线 `origin/main` / 工作树 HEAD：`b81fc7fc8fe42e8d4a6c91c85f8216a3977a75eb`。查询为 2026-11-07 的只读快照；状态会继续变化。结论：**main 的 macOS 确有共因，但 Linux、外部契约和审查红灯不能统归 main**。历史红灯不会因本地修复自动消失。

## main

最近一次 main 组件 workflow [41706813765](https://github.com/surlymochan/phanes/actions/runs/41706813765) 失败：Ubuntu 3.9/3.11/3.13、OpenBot、示例 JSON 和 drift 均成功；macOS 三个 Python 版本均失败。此前 [41685880984](https://github.com/surlymochan/phanes/actions/runs/41685880984) 也失败，中间多次 main 运行取消，取消不能视为成功或失败。

| 类型 | 证据与根因 | 修复 |
|---|---|---|
| macOS 脚本导入 | [main 3.11 job](https://github.com/surlymochan/phanes/actions/runs/41706813765/job/126181343003)：`ImportError: sibling json.py shadowed the stdlib`；脚本路径 `/var/...` 与 Python 的 `/private/var/...` 同目录但 `abspath` 不相等，导致未从 `sys.path` 移除脚本目录。 | `engine.py` 比较 `realpath`；测试增加符号链接入口。 |
| macOS worktree 路径 | 同一 job：`AssertionError: Lists differ: ['/private/var/.../repo'] != ['/var/.../repo']`；Git 输出真实路径，测试却比较临时目录别名。 | 测试对期望路径使用 `resolve()`，仍检查没有残留 worktree。 |
| macOS 额外失败 | [#1782 run](https://github.com/surlymochan/phanes/actions/runs/41683810959) 的 3.9 job 另报 `fleet_recovered_at not found`，当时记录仍有 `fleet_recovery_stop_requested`。是否为时序问题尚未证明；不能保证仅凭上述两处修复即可全绿。 | 未改，需独立复现。 |

## 候选 PR 失败分组

以下为查询时 head 的 rollup；链接指向可查看全部 job 的 run。组内列出的是对应检查失败，不代表其他检查通过或所有 job 的失败原因完全相同。

| PR | 失败或未完成 | 证据 |
|---|---|---|
| #1391 | macOS、lane-review | [41333581182](https://github.com/surlymochan/phanes/actions/runs/41333581182)、[41333610340](https://github.com/surlymochan/phanes/actions/runs/41333610340) |
| #1505 | macOS，另有取消 | [41682465876](https://github.com/surlymochan/phanes/actions/runs/41682465876)；日志含上述两个路径失败 |
| #1632 | external-lane/contract | head status 描述 `thin: effective<30: effective=15 docs=25 other=0`，不是 Actions run；[PR](https://github.com/surlymochan/phanes/pull/1632) |
| #1654 | macOS、lane-review | [41478859038](https://github.com/surlymochan/phanes/actions/runs/41478859038)、[41618126070](https://github.com/surlymochan/phanes/actions/runs/41618126070) |
| #1677 | macOS、lane-review | [41612671777](https://github.com/surlymochan/phanes/actions/runs/41612671777) |
| #1722 | Linux、macOS | [41485816257](https://github.com/surlymochan/phanes/actions/runs/41485816257)：Linux `FileNotFoundError ... state/lanes-status.json`，3 个 lane status 测试出错；该旧 PR 的失败不能仅归因于 main macOS。当前基线对应 5 个测试本地通过。 |
| #1764 | lane-review | [PR](https://github.com/surlymochan/phanes/pull/1764)；status `CHANGES` |
| #1782 | macOS、lane-review | [41683810959](https://github.com/surlymochan/phanes/actions/runs/41683810959)，还含上表 recovery 失败 |
| #1802 | Linux、macOS | [41723210557](https://github.com/surlymochan/phanes/actions/runs/41723210557)：Linux `test_json_report_structure` 期望字段列表缺少实际的 `burn`，属该合并结果的测试/接口不一致。 |
| #1813 | Linux、macOS | [41616716342](https://github.com/surlymochan/phanes/actions/runs/41616716342)：Linux `2c4543ecaac779fb29499807c89c8a704a846e25 is not a commit of this repository`，fixture 引用 CI checkout 不可用的对象。 |
| #1831 | lane-review | [PR](https://github.com/surlymochan/phanes/pull/1831)；status `CHANGES` |
| #1876 | 当前 BLOCKED，但查询时无失败项；5 个测试仍在运行 | [42357136971](https://github.com/surlymochan/phanes/actions/runs/42357136971)，Ubuntu 3.13 已成功。不能沿用“每个都有失败检查”的旧快照。 |

`external-lane/contract` 由 `openswarm/bin/openswarm_external.py` 检查外部 lane PR 契约并发布 commit status；#1632 的描述是有效变更量不足阈值，是否为合理例外需按其实际任务审查，不应为消红改阈值。

## lane-review 责任与决策

它是 commit status，而非 `.github/workflows` 中的 Actions job。`openswarm/lib/review/status.py` 将 APPROVE 映射 success、CHANGES 映射 failure，dispatcher 的 `openswarm/lib/dispatch/engine.py` 在收获 review 后核对当前 head 并发布状态和评论。#1391、#1654、#1677、#1764、#1782、#1831 的实际 head status 均为 `CHANGES`，因此这些红灯是已给出的拒绝，并非仅凭证据可归为生产者离线；须查看各自 review finding 并修复后重新审查，不能机械覆盖。

新架构中历史常驻会话已取消：仓库代码描述的是原 dispatcher 生产路径，**目前哪个运行中的服务承担此职责未获运行态证据，无法确认**。Boss 需指定新的 review 调度与 status 发布者、身份/权限、失败重试和 head 变化时的重新审查责任，并决定如何处理历史 CHANGES；不得仅为通过保护伪造 success。当前只读 API 的传统 branch protection 查询返回 404；可见 active ruleset 仅含删除及非快进限制，未暴露 required status checks。因此不能独立确认 `lane-review` 当前确为 GitHub 必需检查，也不能将所有 BLOCKED 归因于它。

## 验证与限制

本机 macOS：`PYTHONPATH=openswarm/tests python3 -m unittest test_dispatch_engine.ScriptRun -v` 通过 1 项（含普通与符号链接路径）；`python3 -m unittest discover -s openswarm/tests -p 'test_merge_union.py' -v` 通过 7 项；`PYTHONPATH=openswarm/tests python3 -m unittest test_dispatch_lanes_status -v` 通过 5 项。没有运行完整 CI 矩阵或远端重跑；尚未验证各旧 PR 应用修复后的结果。未提交、推送或执行 GitHub 写操作。
