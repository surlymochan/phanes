# 组织诊断 schema：额外未知顶层键被忽略或告警的文档一句
labels: p2,backend:codex
在 `openbot/examples/org-health-local/README.md` 的 schema 说明段落补一句：额外未知顶层键被忽略（必要时告警），不要改 `validate.py` 的解析逻辑。

验收：`rg -n "未知顶层键" openbot/examples/org-health-local/README.md`
