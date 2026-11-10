# 组织诊断 Builder：schema 缺失时 build 的失败提示单测
labels: p1,backend:codex

## Scope

- 在 `examples/org-health-local/test_run_e2e.py` 补一条单测：schema 文件缺失时 `build` 失败并给出可读提示。
- 只改测试文件，不改 `builder/build.py` 的产物路径。
- 不改 `examples/org-health-local/schema/` 下的任何文件。
- 不改 `examples/org-health-local/README.md` 里的命令示例。

## Findings

- `builder/build.py` 在 schema 缺失时抛 `FileNotFoundError`，错误信息里只有路径。
- 错误信息里没有说明缺的是哪个 schema 文件。
- 错误信息里没有说明调用方该怎么补。
- `test_run_e2e.py` 只覆盖 schema 齐全的正常路径。
- `test_run_e2e.py` 没有覆盖 schema 目录整体缺失的情况。
- `test_run_e2e.py` 没有覆盖 schema 文件存在但内容不是 JSON 的情况。
- `test_run_e2e.py` 没有断言失败时退出码非零。
- `test_run_e2e.py` 没有断言失败时不会写出产物文件。
- `test_run_e2e.py` 没有断言提示信息里含 schema 的名字。
- `test_run_e2e.py` 没有断言提示信息里含可执行的下一步。
- `test_run_e2e.py` 的临时目录在失败路径上没有清理。
- `test_run_e2e.py` 失败时打印的 traceback 比提示信息更早被看到。
- `builder/build.py` 的 `main()` 把异常转成退出码时丢掉了原始信息。
- `builder/build.py` 在写产物之前没有检查输出目录是否可写。
- `builder/build.py` 失败后残留了半写的产物目录。
- `test_e2e_smoke.py` 依赖 `test_run_e2e.py` 的临时目录清理顺序。
- `examples/org-health-local/run_tests.py` 收集测试时没有区分失败与错误。
- `examples/org-health-local/run_tests.py` 的汇总行数不含被跳过的用例。
- `examples/org-health-local/run_tests.py` 在没有测试文件时仍然报成功。

## Rules

- 先读 `examples/org-health-local/README.md` 与仓库根的 `AGENTS.md`，按仓库现有风格改动。
- 不引入新依赖；测试只用标准库 `unittest`。
- 用户可见提示走仓库已有的文案来源，不新增硬编码字符串到业务代码。
- 不改 `builder/build.py` 的行为，只补测试。
- 每个发现都要先在当前代码上确认：上游已经修好的就报告「已修」，不要重复改。
- 一次只跑一个窄检查：`python3 examples/org-health-local/test_run_e2e.py`。
- 提交前跑 `python3 examples/org-health-local/run_tests.py`。
- 加一条 `changelog.d/<短名>.md` 的 release note，格式看目录里已有的文件。
- 不读也不打印 `.env*`、`.dev.vars` 和任何凭证文件。
- 留在当前 worktree 里，不提交、不推送。

## Report

- 每个发现写「已修 / 已修(上游) / 未修(原因)」。
- 列出改动过的文件与跑过的命令及结果。

验收：`python3 examples/org-health-local/test_run_e2e.py`

## 失败模式清单

对照 `docs/defect-patterns.md` 检查相关失败模式。
