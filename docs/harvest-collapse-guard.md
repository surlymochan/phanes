# AD fix/rebase 收割塌缩护栏（#1833）

## 行为

`tools/lane-autodispatch/autodispatch.py::finish` 在 fix/rebase 结果推回 PR 分支之前，先读取
issue 正文，再读取正文声明的目标 PR 的 head 和 base。卡正文、PR 元数据或 git 检查无法读取或
解析时均 fail closed。显式 `整体重写：是` 可豁免；没有目标 PR 的普通实现卡继续正常收割。

护栏要求待推送 head 以 fix 前 PR head 为祖先（`git merge-base --is-ancestor`）。另外分别比较
fix 前后相对 base 的变更文件数和增删行数：任一项下降超过阈值就拒绝，阈值由
`AD_HARVEST_COLLAPSE_THRESHOLD` 配置，默认 50%。拒绝时不 push、不修改 PR、不消耗 review 轮次，
移除 `in-progress` / `lane-ready` 并添加 `lane-failed`；brain 日志包含 issue、PR、前后 SHA、前后
体量和拒绝原因。

## 审查意见映射

| Finding | 修复 | 回归覆盖 |
| --- | --- | --- |
| B1：正文读取/解析失败可能按空正文放行 | `harvest_collapse_guard()` 区分读取错误、JSON 错误和空正文；在 push 前拒绝并记日志 | `test_unreadable_card_body_is_refused`、`test_unparsable_card_body_is_refused` |
| B2：事故回放使用合成提交，不能证明真实 SHA 的祖先与体量判定 | `test_replay_of_the_restore_manifest_refuses_every_incident` 从恢复清单取真实 before/after SHA 并实际调用 `finish` | 清单含六组真实 SHA 时逐 PR 断言拒绝及无 push；当前沙箱未提供清单时该测试 skip，宿主机需设置 `AD_HARVEST_COLLAPSE_MANIFEST` 指向清单重跑 |

## 测试对应

`tools/lane-autodispatch/test_harvest_collapse_guard.py` 经公开入口 `finish()` 驱动真实临时 Git 仓库，
并用假 `gh` 观察标签和 PR 写操作。用例覆盖仅文件数下降、仅行数下降、非祖先、正常祖先、整体重写
豁免、git 错误、卡正文读取/解析错误、收割引用缺失、普通实现卡，以及六例事故体量回放。
拒绝用例检查无 push、PR 远端分支不变、无 PR 写入、添加 `lane-failed`，且 brain 日志带有判定
证据。真实 SHA 清单由部署环境提供，不随仓库提交。

## 迁移

需求原路径 `openswarm/tests/test_harvest_collapse_guard.py` 随 #1894 迁移后，AD 收割的实际实现仍在
`tools/lane-autodispatch/autodispatch.py`，因此测试迁到同目录的 `test_harvest_collapse_guard.py`；
不在已废弃的 `openswarm/` 下新增文件。部署运行副本 `~/phanes-ctl/bin/autodispatch.py` 需由部署流程
同步仓内实现后才生效。
