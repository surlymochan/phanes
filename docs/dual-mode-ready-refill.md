# 双模式 ready 补卡契约

`lane-autodispatch` 的 `_ready` 是当前可派发的 `lane-ready` 数量，低水位阈值由 `AD_LOW`（默认 10）决定。每次补仓按以下顺序查找任务：

1. `~/phanes-ctl/autodispatch/backlog/*.md` 本地任务卡；成功建 issue 后移入 `backlog/filed/`。
2. `origin/main` 上尚未导入的 `docs/backlog/next/*.md`。
3. 两者都耗尽时，若已有 planner issue，则不重复建卡。

离线可验证的日志契约如下：

- `_ready < AD_LOW` 且本地 backlog 为空、repo `docs/backlog/next/` 已耗尽时，日志包含 `WARNING queue low`。
- 存在本地任务卡并成功建 issue 时，日志包含 `filed N issue(s)`，其中 `N` 是本次实际落地的卡数。

检查使用假目录，不读取或修改运行中的 `~/phanes-ctl` 或仓库 backlog：

```bash
python3 tools/check-ready-refill-contract.py
```

## 任务卡的预算分级（Refs #1686）

planner 铸卡/拆卡时，每张卡默认 45 分钟预算；确属大卡（多文件实现 + 完整测试，45 分钟做不完）时，在 `labels` 里加 `budget:large`，或在正文加一行 `预算：90`，autodispatch 会给它 90 分钟预算：

```
# 大卡标题
labels: p1,backend:codex,budget:large
<任务正文>

验收：`<一条可直接运行的命令>`
```

- 卡片校验（`tools/check-ad-ready-target.py`）：标记写错（如 `预算：九十`、`预算：60`、`budget:huge`）的卡会被报出来、不落 issue；没有标记但正文很长的卡只给提示，不自动升格。
- 派发时按卡分级（`tools/lane-autodispatch/autodispatch.py` 的 `budget_for()`）：大卡发 `--budget-minutes 90`，其余仍发 45；分级依据写进派发日志（lane、issue、所选预算、依据）。
- 仓库里要有这个标签：`gh label create budget:large --description "dispatch budget tier" --color 1D76DB`。

离线检查：

```bash
python3 tools/check-ad-budget-tier.py
python3 tools/check-ad-ready-target.py
```

实现位置：[`tools/lane-autodispatch/autodispatch.py`](../tools/lane-autodispatch/autodispatch.py) 的 `tick()` / `replenish()`。
