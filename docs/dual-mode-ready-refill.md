# 双模式 ready 补卡契约

`lane-autodispatch` 的 `_ready` 是当前可派发的 `lane-ready` 数量，低水位阈值由 `AD_LOW`（默认 10）决定。每次补仓按以下顺序查找任务：

1. `~/phanes-ctl/autodispatch/backlog/*.md` 本地任务卡；成功建 issue 后移入 `backlog/filed/`。
2. `origin/main` 上尚未导入的 `docs/backlog/next/*.md`。
3. 两者都耗尽时，若已有 planner issue，则不重复建卡。

## 触发条件（Refs #1662）

2026-11-02 19:00 之后 `autodispatch.log` 再没有新的 mint 记录：池水位从 14 掉到 5，看门狗
19:45 那一轮在 backlog 的 21 张卡里找不到一张合规卡。补卡的触发条件因此固定为：

- **池见底补到 target，而不是只补到 low**：`ready < LOW` 时补卡数是
  `need = max(TARGET - ready, starved, 0)`（`TARGET = 20`，`AD_TARGET` 可调）。只补到 low
  会让池子一直贴着阈值，下一轮 tick 之前车道又是空的。
- **队列读不出来时补卡有界**：队列查询失败时水位未知，补卡按「上次已知水位 + 之后铸出的
  张数」计算，连续失败次数记在 `state["_queue_failures"]`，超过
  `AD_QUEUE_FAIL_REFILL_MAX`（默认 3）就不再铸卡，转而等待一次权威读；水位完全未知
  （从未成功读过队列）时一次也不铸。
- **不合规的卡被隔离，不拖垮整批**：缺 `# ` 标题、`labels:` 行或正文的卡移到
  `backlog/rejected/` 并记录原因，其余卡照常铸出；同名旧卡不会被覆盖（旧卡保留，新卡得到
  `.1`、`.2` … 后缀）。repo backlog（`docs/backlog/next/*.md`）的卡还要满足 planner 契约
  （正文两行以上且带 `验收：` 行，见 `is_thin_card`），不满足的卡被跳过而不铸出。
- **单 planner 门控**：planner 查询返回的结果无法解析时无法证明「没有 planner 已开」，
  此时保守跳过建卡并记录原因，不破坏门控。

## 日志证据

| 场景 | 日志 |
| --- | --- |
| 池见底 | `queue low (5 < 10): filed N issue(s) from backlog: #…` |
| 池见底但没有卡可铸 | `WARNING queue low: … need 15 card(s) (threshold 10, target 20), none filed (1 card(s) rejected; planner issue #42 already open)` |
| 队列读不出来 | `queue error: …` + `queue unreadable #1: minting 15 card(s) for the last known level (5 dispatchable)` |
| 有界重试耗尽 | `queue unreadable #4: no card minted any more (bounded retries exhausted, waiting for an authoritative read)` |
| 不合规的卡 | `backlog a-thin.md: thin card: no body → …/rejected/a-thin.md` |
| planner 门控 | `planner issue already open (#42): no new planner card minted` |
| planner 查询读不出来 | `planner query unreadable: the planner JSON cannot be parsed (Expecting value: line 1 column 1 (char 0)); skip planner creation` |
| 停铸卡 | `replenish skipped: mint paused (PAUSE-MINT marker or the mint pause scope)` |

`lanes-status.json` 另外带 `_queue_failures` 与 `_minted`（队列读不出来时还有
`_ready_estimate`），看门狗可以直接看出水位是权威值还是估算值。

离线可验证的日志契约如下：

- `_ready < AD_LOW` 且本地 backlog 为空、repo `docs/backlog/next/` 已耗尽时，日志包含 `WARNING queue low`。
- 存在本地任务卡并成功建 issue 时，日志包含 `filed N issue(s)`，其中 `N` 是本次实际落地的卡数。

检查使用假目录，不读取或修改运行中的 `~/phanes-ctl` 或仓库 backlog：

```bash
python3 tools/check-ready-refill-contract.py
```

实现位置：[`tools/lane-autodispatch/autodispatch.py`](../tools/lane-autodispatch/autodispatch.py) 的 `tick()` / `replenish()`。
