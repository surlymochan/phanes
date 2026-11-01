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

实现位置：[`tools/lane-autodispatch/autodispatch.py`](../tools/lane-autodispatch/autodispatch.py) 的 `tick()` / `replenish()`。
