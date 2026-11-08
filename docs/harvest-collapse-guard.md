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
4. **祖先**：实际推送的过滤后 head 必须包含 fix 前 PR head（`git merge-base --is-ancestor`）。
   越界产物过滤（#1835）可能重建提交；即使过滤前 lane 引用包含旧 head，过滤后丢失祖先关系
   仍拒绝推送，不能用内容相同代替祖先检查。
5. **体量**：使用 `gh api repos/<owner>/<repo>/pulls/<pr>` 一次读取 PR 的 `head.sha` 和 `base.sha`，
   相对该 base 比较 fix 前后的变更文件数与增删行数。任一项下降到阈值
   以下就拒绝（默认 50%，`AD_HARVEST_COLLAPSE_THRESHOLD` 可配；两个维度是“或”的关系）。
6. **git 失败**：任何 git 命令失败都拒绝（fail closed）。

拒绝时：不 push、不动 PR 分支、不消耗 review 轮次、不开 PR；给 issue 去掉 `in-progress` /
`lane-ready`、加上 `lane-failed` 并写明原因；brain 日志写一行（PR 号、前后 sha、前后体量、判定
原因），`$D/runs/<run>.log` 追加同一条判定（失败台账线索）。放行时 AD 日志记一行判定摘要。

## 审查意见映射

| Finding | 修复 | 回归覆盖 |
| --- | --- | --- |
| B1：正文读取/解析失败被当作空正文，目标 PR 丢失后仍放行 | `harvest_card_body()` 把读失败、JSON 解析失败和“没有正文”分开，三种情况都在 push 前拒绝并记因 | `test_unreadable_card_body_is_refused`、`test_unparsable_card_body_is_refused`、`test_unreadable_card_with_a_missing_harvest_branch_is_refused` |
| B2：事故回放使用合成提交，不能证明真实 SHA 的祖先与体量判定 | `test_replay_of_the_restore_manifest_refuses_every_incident` 从恢复清单取真实 before/after/base SHA 并实际调用 `finish`；按 SHA 从 AD 的仓库（`AD_LOCAL`、`~/work/phanes-repo`）取进临时仓库，dispatch base 和 PR base 都使用该例的真实 base | 必须完整包含六个指定 PR（含 #1824），缺项、重复、缺 base 或对象不可达均失败；逐 PR 输出完整 SHA、base、体量和拒绝原因，判定必须是“塌缩/不包含”而不是“git 失败”；仅未提供清单时 skip |
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

## 本轮审查 BLOCKER 修复映射（基于 05ed7622e）

- B1（过滤前祖先检查）：`harvest_collapse_guard` 删除过滤前引用参数，检查实际推送 SHA。
  `test_filtered_head_must_contain_the_old_head` 经过真实过滤器重建历史，验证拒绝、无 push、
  远端 PR 分支不变、`lane-failed` 和 brain 证据。
- B2（错误 base / 部分回放通过）：回放同时设置 dispatch base 和 PR metadata base，严格要求
  全部六例可达并逐例打印拒绝结果。`test_manifest_replay_uses_recorded_base_and_reports_all_six`
  用本地测试对象验证 base 传递和六行输出；`test_manifest_replay_requires_every_incident` 验证
  缺 #1824 必须失败。这两个用例只验证回放驱动，不作为真实事故证据。

本沙箱没有恢复清单，未生成六例真实拒绝输出。宿主机需提供清单和对象库，保存下面命令的
逐例输出用于 PR 交付（此任务不写 GitHub）：

```sh
AD_HARVEST_COLLAPSE_MANIFEST=<清单路径> AD_LOCAL=<真实对象仓库> python3 -m pytest -q -s tools/lane-autodispatch/test_harvest_collapse_guard.py
```

显式指定的清单不存在时测试失败；未指定且默认位置无清单时仅真实事故回放测试 skip。

## 本轮审查 BLOCKER 修复映射（基于 e1f9d8109）

- B1（`gh pr view` 不支持 `baseRefOid`）：改用只读 REST PR 接口的 `head.sha` / `base.sha`，
  从同一响应获取两个提交，避免依赖本地可能过期的 base 分支引用。
  `gh api` 不接受 `-R`，因此直接调用 `sh`，仓库名写入 endpoint。
  接口与参数参见 [GitHub REST PR 文档](https://docs.github.com/en/rest/pulls/pulls#get-a-pull-request)
  和 [gh api 手册](https://cli.github.com/manual/gh_api)。
- 回归：假 `gh pr view` 现在拒绝虚构字段；`test_rest_pr_metadata_allows_a_normal_fix`
  在修复前因 `Unknown JSON field: "baseRefOid"` 失败，修复后验证真实 Git 历史正常放行，
  且请求为不带 `-R` 的 REST endpoint。
  `test_unreadable_or_unparsable_pr_metadata_is_refused` 和
  `test_missing_or_invalid_pr_sha_is_refused` 覆盖 API 失败、坏 JSON、缺失及无效 SHA 的拒绝路径。
  原有塌缩、祖先、正文和回放测试继续保留；真实事故 SHA 回放仍需上述宿主机清单与对象库。

本轮验证：`python3 -m pytest -q tools/lane-autodispatch/test_harvest_collapse_guard.py
tools/lane-autodispatch/test_harvest_filter.py tools/tests/test_lane_pr_validation.py`
为 49 passed、1 skipped、20 subtests passed（skip 为真实事故清单缺失）；
`python3 tools/check-ad-harvest-backoff.py` 输出 `ad harvest fetch backoff: OK`。

## 第三轮审查 BLOCKER 修复映射（基于 e4ddcdee6）

- B1（祖先检查超时绕过拒绝流程）：`harvest_collapse_guard()` 捕获祖先检查的
  `subprocess.SubprocessError`（含 `TimeoutExpired`）及 `OSError`，返回拒绝判定，
  由 `finish()` 完成 `lane-failed` 标记、brain 日志和失败台账记录。异常和非零退出判定
  均附 PR 号、前后 SHA、已计算的前后体量及错误原因。
- 回归：`test_ancestry_check_errors_use_the_rejection_flow` 经公开 `finish()` 入口注入超时、
  启动异常和非零退出，检查无 push、远端 PR 分支不变、无 PR 写入、标记 `lane-failed`、
  brain/失败台账完整记因，结束状态只增加 `_done`，不消耗 review 轮次。
  修复前测试因 `TimeoutExpired` 外抛失败；既有复现测试全部保留。
- 本轮验证：上述护栏、过滤器和 PR 校验三套测试为 50 passed、1 skipped；
  `python3 -m pytest -q swrm/tests/test_lane_busy_stale.py` 为 15 passed；
  `python3 tools/check-ad-harvest-backoff.py` 输出 `ad harvest fetch backoff: OK`。
- 真实六例 SHA 回放继续使用上述宿主机清单与对象库命令，不将合成测试作为真实回放证据。

## 第四轮审查 BLOCKER 修复映射（基于 71c786ac4）

- B1（真实过滤器失败绕过拒绝流程）：`finish()` 的过滤异常分支保留 `filter-failed` 状态，
  补齐 `lane-failed` 标签、brain 日志、`$D/runs/<run>.log` 失败台账及 `mark_done()`，
  返回 `done=True` 释放车道。所有操作均在 push 和 PR 写入之前，不消耗 review 轮次。
- 回归：`test_real_filter_failures_use_the_rejection_flow` 经过公开 `finish()` 和真实过滤器，
  覆盖收割引用缺失（`real_filter=True, lane_ref=False`）、base 对象缺失，以及过滤器 Git
  非零退出、超时和启动异常。检查无 push、PR 远端分支不变、无 PR 写入、标记 `lane-failed`、
  brain/失败台账保留原因，且状态只增加 `_done`。修复前五种场景均因缺少 `lane-failed` 失败。
- 保留已有塌缩护栏及复现测试；原 `openswarm/tests/` 测试继续对应
  `tools/lane-autodispatch/test_harvest_collapse_guard.py`，不修改废弃目录。
- 验证：`python3 -m pytest -q tools/lane-autodispatch/test_harvest_collapse_guard.py
  tools/lane-autodispatch/test_harvest_filter.py tools/tests/test_lane_pr_validation.py
  swrm/tests/test_lane_busy_stale.py` 为 66 passed、1 skipped、46 subtests passed；
  `python3 tools/check-ad-harvest-backoff.py` 输出 `ad harvest fetch backoff: OK`。
- 真实六例 SHA 回放仍需宿主机提供上述清单及对象库；只读 GitHub 请求受沙箱 socket 限制，
  本轮按任务附件核对，无 GitHub 写操作。
