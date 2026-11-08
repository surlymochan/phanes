# AD fix/rebase 收割塌缩护栏（#1833）

## 背景

fix/rebase 轮没有在 PR 当前 head 上续改、只产出增量补丁时，AD 把该提交推回 PR 分支，整个 PR 被
替换（2026-11-03：#1807 8/1780→1/11、#1804 6/1401→3/39、#1715 8/868→2/23、#1755 8/746→1/19、
#1653 6/200→2/19、#1824 9/835→1/17）。护栏在推送之前拦住这类收割。

## 行为

`tools/lane-autodispatch/autodispatch.py::finish` 在推送之前调用 `harvest_collapse_guard()`：

1. **读卡**：卡正文用 `gh issue view <n> --json body` 读。读失败（gh 非零退出）、JSON 解析失败、
   响应里没有正文，都**不是空正文**：没有正文就没有目标 PR，护栏无从判断，直接拒绝（fail closed）。
2. **豁免**：卡正文写明 `整体重写：是`（也接受 `true` / `yes` / `允许` / `豁免`）时跳过护栏。
3. **目标 PR**：只有 fix/rebase 卡才比对：正文里的 `重基PR：#1845`、`fix/rebase PR #1845`、
   `rebase PR #1845`、`fix PR #1845`。普通实现卡即使提到别的 PR 也不比对——否则普通收割会被拿
   别的 PR 的 head 去比而误拒。没有目标 PR 的卡照常收割。
4. **祖先**：新 head 必须包含 fix 前 PR head（`git merge-base --is-ancestor`）。比对的是 lane
   产出的分支 `refs/fleet/<lane>`：越界产物过滤（#1835）会重建提交对象，被推送的 head 因此不再
   是原来的对象，但内容仍是那一轮的内容。
5. **体量**：相对 base（PR 的 `baseRefOid`）比较 fix 前后的变更文件数与增删行数。任一项下降到阈值
   以下就拒绝（默认 50%，`AD_HARVEST_COLLAPSE_THRESHOLD` 可配；两个维度是“或”的关系）。
6. **git 失败**：任何 git 命令失败都拒绝（fail closed）。

拒绝时：不 push、不动 PR 分支、不消耗 review 轮次、不开 PR；给 issue 去掉 `in-progress` /
`lane-ready`、加上 `lane-failed` 并写明原因；brain 日志写一行（PR 号、前后 sha、前后体量、判定
原因），`$D/runs/<run>.log` 追加同一条判定（失败台账线索）。放行时 AD 日志记一行判定摘要。

## 审查意见映射

| Finding | 修复 | 回归覆盖 |
| --- | --- | --- |
| B1：正文读取/解析失败被当作空正文，目标 PR 丢失后仍放行 | `harvest_card_body()` 把读失败、JSON 解析失败和“没有正文”分开，三种情况都在 push 前拒绝并记因 | `test_unreadable_card_body_is_refused`、`test_unparsable_card_body_is_refused`、`test_unreadable_card_with_a_missing_harvest_branch_is_refused` |
| B2：事故回放使用合成提交，不能证明真实 SHA 的祖先与体量判定 | `test_replay_of_the_restore_manifest_refuses_every_incident` 从恢复清单取真实 before/after SHA 并实际调用 `finish`；清单的 SHA 只有在对象可达时才有意义，回放因此先从 AD 的仓库（`AD_LOCAL`、`~/work/phanes-repo`）把真实分支取进临时仓库 | 清单与真实对象都在时逐 PR 断言拒绝、判定是“塌缩/不包含”而不是“git 失败”；当前沙箱没有清单时该测试 skip，宿主机需设置 `AD_HARVEST_COLLAPSE_MANIFEST=<清单路径>` 重跑 |
| 交付说明未逐条映射 B1/B2 | 本表 | 本表 + 下面的“测试对应” |

## 测试对应

`tools/lane-autodispatch/test_harvest_collapse_guard.py` 经公开入口 `finish()` 驱动真实临时 Git
仓库，并用假 `gh` 观察标签和 PR 写操作。用例覆盖仅文件数下降、仅行数下降、非祖先、正常祖先、
整体重写豁免、git 错误、卡正文读取/解析错误、收割引用缺失、卡正文读不到且收割分支不存在、普通
实现卡、提到别的 PR 的普通卡，以及六例事故体量回放。拒绝用例检查无 push、PR 远端分支不变、无 PR
写入、添加 `lane-failed`，且 brain 日志带有判定证据；六例体量回放还断言判定原因确实是体量塌缩
（`collapsed`），而不只是“被拒了”。真实 SHA 清单由部署环境提供，不随仓库提交。

验收命令（清单路径按 Boss 裁定迁到被测实现同目录）：
`python3 -m pytest -q tools/lane-autodispatch/test_harvest_collapse_guard.py`。

护栏让 AD 多读一次卡正文，因此直接驱动 `finish()` 的既有假 `gh` 需要回答一张卡：
`tools/lane-autodispatch/test_harvest_filter.py`、`tools/tests/test_lane_pr_validation.py`、
`tools/check-ad-harvest-backoff.py` 的假 `gh` 已改为返回一张普通卡（只改假数据，不改断言）。

## 迁移

需求原路径 `openswarm/tests/test_harvest_collapse_guard.py` 随 #1894 迁移后，AD 收割的实际实现仍在
`tools/lane-autodispatch/autodispatch.py`，因此测试迁到同目录的 `test_harvest_collapse_guard.py`；
不在已废弃的 `openswarm/` 下新增文件。部署运行副本 `~/phanes-ctl/bin/autodispatch.py` 需由部署流程
同步仓内实现后才生效。
