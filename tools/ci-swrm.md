# Swrm CI 门禁脚本（Refs #1867）

`tools/ci-swrm.sh` 是 Swrm 组件的 CI 门禁：`.github/workflows/phanes.yml` 的
`swrm` 任务直接调用它，所以 workflow 与脚本不会各自漂移。

## 门禁跑什么

| 步骤 | 命令 | 工作目录 |
| --- | --- | --- |
| A | `python -m unittest discover -s tests -v` | `swrm/` |
| B | `OPENSWARM_STATE_FILE=examples/demo-state.json`、`OPENSWARM_HOME="$(mktemp -d)"`、`python bin/swrm status`、`python bin/swrm route --type impl --difficulty 3` | `swrm/` |

A、B 两步是旧 `phanes.yml` 的 "Unit tests" 与 "Offline CLI smoke test" 两步的逐字副本。
脚本用运行器上的默认解释器跑一遍，并在开头打印「只覆盖当前单个 Python 版本」。

## 按改动路径追加的 unittest

| 改动路径 | 追加的 unittest |
| --- | --- |
| `tools/lane-autodispatch/` | 该目录下的 `python -m unittest discover -s . -p 'test_*.py' -v` |
| `tools/tests/` | 仓库根下的 `python -m unittest discover -s tools/tests -p 'test_*.py' -v` |

改动路径来自 `git diff --name-only --no-renames "$BASE_SHA...HEAD"`，再叠加工作区里未提交的
改动与未跟踪文件。

### 为什么禁用重命名检测

`git diff --name-only` 对重命名只报**目标**路径：从 `tools/lane-autodispatch/`、
`tools/tests/` 或 `swrm/` **移出**的文件因此不会命中上面的路径规则——Swrm 任务会
被整个跳过，追加的 unittest 也不会跑。

workflow（`.github/workflows/phanes.yml` 的 `changes` 任务）与脚本
（`tools/ci-swrm.sh` 的 `detect_changed_paths`）都改用
`git diff --name-only --no-renames`，移出的文件于是同时以旧路径和新路径出现：门禁照常触发，
对应目录的 unittest 照常追加。

## 上轮审查逐项交付映射

| 问题 | 本轮改动 | 对应测试 |
| --- | --- | --- |
| B1：`git diff --name-only` 只报重命名的目标路径，移出 `tools/lane-autodispatch/` 或 `tools/tests/` 的改动既不触发 Swrm 任务，也不追加对应目录的 unittest | workflow：`.github/workflows/phanes.yml:51`（`changes` 任务的路径检测）与触发规则 `.github/workflows/phanes.yml:40`；脚本：`tools/ci-swrm.sh:43`（`detect_changed_paths`）——两者都用 `--no-renames` | 移出目录场景：`test_moved_out_of_tools_tests_is_selected`、`test_moved_out_of_lane_autodispatch_is_selected`、`test_uncommitted_move_out_is_selected`；workflow 触发：`test_workflow_selects_moved_out_paths`、`test_workflow_runs_the_gate_for_moved_out_paths`；workflow 与脚本等价：`test_script_and_workflow_select_the_same_paths`（均在 `tools/tests/test_ci_swrm_gate.py`） |

### 验证

| 命令 | 结果 |
| --- | --- |
| `python -m unittest discover -s tools/tests -p 'test_ci_swrm_gate.py' -v` | `Ran 6 tests … OK` |
| `python -m unittest discover -s tools/tests -p 'test_*.py' -v` | `Ran 144 tests … OK` |
| `bash tools/ci-swrm.sh` | 门禁脚本本身可执行；Swrm 套件 `Ran 1959 tests`，本机只剩 8 个与本改动无关的环境失败（`test_fleet_refresh` 缺 `/usr/lib/ssl/openssl.cnf`、`test_pi_isolation` 缺 bwrap 用户命名空间）。本 PR 对 `swrm/` 目录零改动，CI 的 ubuntu/macOS 运行器上这两个依赖都在。 |

## 测试隔离

测试及离线探针的 `TMPDIR` 固定在 worktree 的 `.tmp/test-tmp`。门禁先清除继承的
`OPENSWARM_*` / `XSWARM_*` 覆盖项，再把 `HOME`、`OPENSWARM_HOME`、XDG 目录和
`GH_CONFIG_DIR` 指向新建临时目录，不读取操作者的真实控制面配置。
测试加载器清理环境后，会重新设置隔离的 `OPENSWARM_FLEET_GUARD_CONFIG`，
避免回退到 `/etc` 下的机器级协调器；门禁也为离线探针设置该覆盖项及容量目录。
`OPENSWARM_*` 保留为 swrm 上游兼容环境接口；命令和默认目录已使用 `swrm`。
