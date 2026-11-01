# 组织诊断 Builder：缺 schema 文件时 build 的失败提示单测
labels: p1,backend:pi
在 `openbot/examples/org-health-local/test_run_e2e.py` 补一条单测：schema 文件缺失时 `build` 失败并给出可读提示，只改测试，不改 `build.py` 的产物路径。

验收：`python3 openbot/examples/org-health-local/test_run_e2e.py`
