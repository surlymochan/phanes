# Tester 风险分级约定

使用 `python3 tools/tester-risk.py <PR号>` 输出分级、理由和本次执行比例。命令依赖已登录的 `gh`，并读取 PR 的 diff、reviews、labels 和 closing issue 的 labels。离线复核可传 `--input file.json`，格式为 `{"pr": {...}, "issue": {...}}`。

## 执行深度

- **全读**：逐文件阅读全部 diff、相关测试结果及关联 issue 的失败记录；复核每个高风险路径。分类器命中大 diff、review 轮次达到 4、PR 带 `needs-human`，或关联 issue 带 `lane-failed`/`failure:*` 时触发。比例 100%。
- **抽检**：阅读至少 20% 的变更文件，覆盖主要逻辑、测试和配置；记录抽样文件及结果。比例 20%。数据缺失时仍按抽检并在理由中明确缺口，不能据此声称无前科；tester 应补查可用证据。

## 阈值依据与限制

全读线设为 20 文件或增删合计 1000 行、4 个 review 事件。20 文件/1000 行是便于机械执行的初始操作阈值，不代表统计学风险界线。4 轮阈值锚定本仓库今晚（2026-11-02）审查实证：[`docs/evidence/architecture-review/report-c.json.gz`](evidence/architecture-review/report-c.json.gz) 的 review-round 分布包含 round>3 前科名单；复核报告 [`docs/architecture-review-brief.md`](architecture-review-brief.md) 记录该次快照和复核口径。阈值选择意味着名单中的高轮次 PR 全读，不把该样本外推为概率模型。

分类器结果随执行记录保存：tester 将命令 JSON 输出附入该 PR 的审查记录，保留分级理由和抽样文件清单，供后续复核。
