# 原始证据包（raw evidence pack）

车道审查（lane-review）的交付物从「自由文本 + 车道自己下判定」改成**原始证据包**：
车道只交证据，判定留给 tester（影子期并行，切换后唯一）。见 Epic #1785，卡 #1788（P1-2）。

- 实现：`openswarm/lib/review/evidence.py`（纯模块，只依赖标准库）
- 接线：`openswarm/lib/dispatch/engine.py`（`review_item` 派任务、`finish_review` 校验）
- 读取脚本：`tools/check-review-evidence-pack.py`
- 测试：`openswarm/tests/test_review_evidence.py`、`openswarm/tests/test_dispatch_evidence_pack.py`

## 为什么

车道既执行又判定是判定漂移的根源之一：同一个车道既写代码又宣布「可以合」，
证据和结论混在一段自由文本里，谁也说不清结论是从哪条原始证据来的。
把交付物改成固定格式的证据包之后：

- 证据是**逐项原始**的（CI 结论、测试日志、diff 统计、验收项逐条核查）；
- 结论**只由 tester 出**，车道最多附一段标注「参考、不算判定」的初步意见；
- 证据包**可被脚本解析**成结构化字段，不合格（退化成自由文本、意见冒充结论、
  测试日志只给结论不给原始依据）在 `finish_review` 侧被记为不完整并留痕。

## 格式

证据包是一个 markdown 文件（车道分支里的 `.lane-review/PR-<n>-evidence.md`），
固定五段，段落标题和字段前缀固定，解析器只读这些：

```
# Evidence pack — PR #123

## 1. CI results (CI 结论逐项)
CI: OpenSwarm tests | pass | https://github.com/acme/project/runs/1

## 2. Test log summary and links (测试日志摘要与链接)
TEST: python -m unittest discover -s tests | 42 tests, all passed | https://.../logs/1
LOG: Ran 42 tests in 1.234s
LOG: OK

## 3. Diff statistics (diff 统计)
DIFFSTAT: files=3 | insertions=120 | deletions=12 | effective_lines=95
RULE: effective_lines = added + deleted lines of code and test files; documentation is excluded

## 4. Acceptance checks (验收项逐条原始核查)
CHECK: acceptance item 1 | pass | `python -m unittest` → Ran 42 tests ... OK
CHECK: acceptance item 2 | 未查 | 日志没取到：gh pr run view 失败（无凭据）

## 5. Preliminary opinion (初步意见 · 参考，不算判定)
OPINION: looks fine to me
```

字段口径：

| 字段 | 含义 |
| --- | --- |
| `CI:` | 一行一个 CI 检查：`名称 \| 结论 \| run 链接或说明`。结论是原始结论（`pass`/`fail`/`pending`/…），不是车道的看法 |
| `TEST:` | 一行一条测试日志：`命令 \| 摘要 \| 日志链接或路径` |
| `LOG:` | 测试日志的**原始行**（逐行照抄）。只给结论不给原始行 → `testlog-no-raw` |
| `DIFFSTAT:` | `files`/`insertions`/`deletions`/`effective_lines`。`effective_lines` = 代码与测试文件的新增+删除行（文档不计），与 thin-PR 门同口径 |
| `CHECK:` | 一行一个验收项：`验收项 \| pass/fail/未查 \| 原始依据（跑的命令 + 它的输出）`。没查到的写「未查」，依据写清为什么没查 |
| `OPINION:` | 车道的初步意见，**可选**。写了就必须在标题里带「参考，不算判定」 |

## 校验

`parse_evidence(text)` 返回结构化字段：

```python
{
  "sections": ["ci", "testlog", "diffstat", "checks", "opinion"],
  "missing": [],            # 缺掉的必填段（ci/testlog/diffstat/checks）
  "errors": [],             # 格式错误，见下表
  "complete": True,
  "ci": [{"name": ..., "status": ..., "detail": ...}],
  "test_logs": [{"command": ..., "summary": ..., "link": ...}],
  "log_lines": [...],
  "diffstat": {"files": 3, "insertions": 120, "deletions": 12, "effective_lines": 95},
  "checks": [{"item": ..., "result": "pass|fail|unchecked", "evidence": ...}],
  "opinion": {"present": True, "marked": True, "text": ...},
}
```

| 错误码 | 含义 |
| --- | --- |
| `ci-empty` | CI 段在，但没有逐项结论 |
| `testlog-empty` | 测试日志段在，但没有 `TEST:` 行 |
| `testlog-no-link` | 有 `TEST:` 行，但没给日志链接/路径（原始依据在哪？） |
| `testlog-no-raw` | 有 `TEST:` 行，但没有 `LOG:` 原始行（只给结论） |
| `diffstat-missing` | diff 统计段在，但没有 `DIFFSTAT:` 行 |
| `diffstat-incomplete` | `DIFFSTAT:` 缺 files/insertions/deletions/effective_lines 之一 |
| `diffstat-no-rule` | diff 统计段没有写有效行口径（`RULE:` 行） |
| `checks-empty` | 验收核查段在，但没有 `CHECK:` 行 |
| `check-no-evidence` | 有 `CHECK:` 行，但没写原始依据 |
| `check-bad-result` | `CHECK:` 的结果不是 pass/fail/未查 |
| `opinion-not-marked` | 意见段没带「参考，不算判定」标记 |
| `verdict-in-pack` | 证据段里出现 `VERDICT:`/`判定：`（意见冒充结论） |
| `section-duplicated` | 同一段出现两次 |

## 接线

- `review_item()` 在审查任务里附上：dispatcher 已经拿到的**原始输入**（每个 CI 检查的
  结论与链接、diff 统计数字）+ 证据包格式说明（`render_spec()`），并要求车道把
  证据包和原来的判定文件一起提交。
- `finish_review()` 读车道分支里的证据包，校验后写进 `reviews.json` 的
  `evidence` 字段（`complete`/`missing`/`errors`/`sections`/条目数/有效行数），
  并在日志里留痕：

  ```
  review PR #123 by M5: evidence pack complete (3 CI items, 1 test logs, 2 acceptance checks)
  review PR #123 by M5: evidence pack INCOMPLETE (missing: checks; errors: -) — recorded, the judgement is untouched
  ```

  证据包完整时，PR 评论里附上原始证据包；不完整时只附一行「不完整」说明
  （免得半截的自由文本冒充证据）。**判定不受影响**：影子期里车道判定照旧，
  tester 的判定是并行的另一条线。

## 没有网络时（Refs #1788 复审）

GitHub 偶尔够不着（DNS 解析失败、`gh` 未安装），审查车道拿不到 diff 就没法干活。
`review_item()` 因此有本地兜底：`gh pr diff` 没有输出时，改用本地 git 对象算 diff
（`local_head_ref()` 依次找 `rebase-pr-<n>`、PR 自己的分支、车道分支 `refs/fleet/<lane>`、
`refs/pull/<n>/head`；`local_base_ref()` 取与 base 分支的 merge-base，退回 `refs/ad/base`），
并在任务里注明这个 diff 是从哪个 ref 算出来的：

```
NOTE: GitHub was not reachable, so this diff was taken from the local git objects: `git diff <base>..<head>` ...
```

**只认精确匹配的头**：dispatcher 知道 PR 头的 SHA 时，本地兜底**只**用与该 SHA 精确相等的
那个 commit（`same_commit()`），绝不用一个旧 ref 顶上——旧 diff 会把头的新改动藏起来，
让遗漏的改动拿到当前头的通过。取不到这个 commit 时不派发审查，只留痕：

```
review PR #123: blocked — the local git objects do not hold the head 877e24f499; an older diff is never reviewed as the current head
```

人工审查要自己看 diff 时同理（PR 头和 base 都在本地对象库里）：

```sh
git diff --stat refs/ad/base..rebase-pr-<n>
git diff refs/ad/base..rebase-pr-<n>
```

## 样例

一轮按新格式写的在途 PR 审查（PR #1806，头 = 分支 `fleet/M10-issue1788-20261103-040108-6d6182` 的当前提交）：

- 证据包：`docs/evidence/review-pack/in-flight-pr-1788.md`
- 原始测试日志：`docs/evidence/review-pack/pr-1806-testlog.txt`、
  `docs/evidence/review-pack/pr-1806-base-env-testlog.txt`
- 本地审查入口：`.lane-review/PR-1806.md`

## 读法

```sh
python3 tools/check-review-evidence-pack.py .lane-review/PR-123-evidence.md
python3 tools/check-review-evidence-pack.py --json .lane-review/PR-123-evidence.md
```

退出码 0 = 完整，1 = 缺段或格式错（与 `reviews.json` 里记的一致）。
