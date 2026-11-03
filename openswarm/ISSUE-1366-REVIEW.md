# #1366 上轮阻塞项与修复映射

- B1：`lib/dispatch/pause.py:193`、`:205` 统一默认 home，`lib/dispatch/engine.py:657` 将其纳入调度器读取；`tests/test_autodispatch.py:452` 验证未设置 OPENSWARM_HOME 时实际 tick 读取默认 home。
- B2：`lib/dispatch/engine.py:2513`、`:2538` 分别在 replenish 和 tick 检查 mint 与 all；`tests/test_autodispatch.py:418`、`:428`、`:444` 覆盖 all、mint 恢复及 mint 过期。
- B3：`bin/openswarm:13984` 从 lane 或调度配置确定 host，`:14008` 在接收任务前匹配；`tests/test_autodispatch.py:570`、`:601` 覆盖无 host 字段的直接分配及配置解析。
- B4：`tests/test_autodispatch.py:368` 起覆盖实际 dispatch tick 的 lane、host、backend、all、mint、恢复及过期；`:549` 起覆盖 lane serve 的直接 assign、各适用 scope、恢复及过期。mint 只影响补充任务，不拒绝已有任务的直接分配。
