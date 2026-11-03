# Evidence pack — PR #1806

Head: 分支 `fleet/M10-issue1788-20261103-040108-6d6182` 的头（本包随该头一起提交；当前头用 `git log --oneline -1 rebase-pr-1806` 取），base `850ad326e`（main；本 PR 已重基到 main）
被审对象：#1788（P1-2，Epic #1785）的实现 PR；本包是 P1-2 要求的「在途 PR 走一轮新格式 review」的样例。
取证方式：离线（github.com 的 DNS 在本沙箱解析失败、`gh` 未安装）。PR 头与 diff 取自本地 git 对象库（`rebase-pr-1806`）。

## 1. CI results (CI 结论逐项)
CI: OpenSwarm tests (ubuntu-24.04, Python 3.11) | unknown | 该 head 的 check run 读不到（离线：`gh` 未安装、github.com DNS 解析失败）；AD 报告这个 job 在前一 head 877e24f499 上失败，本地等价复现见第 2 段
CI: OpenSwarm tests (ubuntu-24.04 与 macos-latest, Python 3.9/3.13) | unknown | AD 只报告了 3.11 这一个 job，其余矩阵项离线读不到

## 2. Test log summary and links (测试日志摘要与链接)
TEST: cd openswarm && python3 -m unittest discover -s tests | 1785 tests, 3 failures + 6 errors：8 项是本沙箱的环境限制（openssl req 与 bwrap 用户命名空间，base 上同样失败），另 1 项是 test_pi_broker 的偶发 ConnectionRefused（单独重跑在本树与 base 上都通过） | docs/evidence/review-pack/pr-1806-testlog.txt
LOG: Ran 1785 tests in 316.259s
LOG: FAILED (failures=3, errors=6)
LOG: ERROR: test_restart_is_idempotent (test_fleet_refresh.FleetRefreshTests.test_restart_is_idempotent)
LOG: subprocess.CalledProcessError: Command '['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', '/tmp/fleet-refresh-tfwo8jyq/key.pem', '-out', '/tmp/fleet-refresh-tfwo8jyq/renewed.cert', '-days', '10', '-subj', '/CN=fake']' returned non-zero exit status 1.
LOG: FAIL: test_host_secrets_and_network_are_not_visible (test_pi_isolation.PiIsolationTests.test_host_secrets_and_network_are_not_visible)
LOG: AssertionError: 1 != 0 : bwrap: No permissions to create a new namespace, likely because the kernel does not allow non-privileged user namespaces.
LOG: # 同样的两个模块在 base（main 850ad326e，不含本 PR）上跑：Ran 8 tests in 0.270s / FAILED (failures=3, errors=5) → docs/evidence/review-pack/pr-1806-base-env-testlog.txt
TEST: cd openswarm && python3 -m unittest tests.test_review_evidence tests.test_dispatch_evidence_pack | 64 tests, all passed（本 PR 自己的两个测试模块） | docs/evidence/review-pack/pr-1806-testlog.txt
LOG: Ran 64 tests in 0.047s
LOG: OK

## 3. Diff statistics (diff 统计)
DIFFSTAT: files=12 | insertions=2072 | deletions=12 | effective_lines=1541
RULE: effective_lines = added + deleted lines of code and test files; documentation (.md/.txt/.rst/.adoc, docs/) is excluded (the rule of the thin-PR gate)
原始数字（`git diff --numstat 850ad326e..HEAD`，HEAD 即本包所在的提交）：.lane-review/PR-1806.md 51/0；docs/ctl-boot.md 1/0；docs/evidence/review-pack/in-flight-pr-1788.md 49/0；docs/evidence/review-pack/pr-1806-base-env-testlog.txt 90/0；docs/evidence/review-pack/pr-1806-testlog.txt 195/0；openswarm/docs/review-evidence-pack.md 157/0；openswarm/lib/dispatch/engine.py 234/11；openswarm/lib/review/evidence.py 507/0；openswarm/tests/test_dispatch_evidence_pack.py 371/0；openswarm/tests/test_dispatch_review_budget.py 2/1；openswarm/tests/test_review_evidence.py 335/0；tools/check-review-evidence-pack.py 80/0。文档行 543 不计入有效行（有效行 = 245 + 507 + 371 + 3 + 335 + 80 = 1541）。

## 4. Acceptance checks (验收项逐条原始核查)
CHECK: 交付一：证据包格式定义并接入 review 产物（CI 结论逐项、测试日志摘要与链接、diff 统计、验收项逐条原始核查） | pass | `openswarm/lib/review/evidence.py` 定义五段固定格式；`openswarm/lib/dispatch/engine.py` 的 `review_item()` 派任务时附上 dispatcher 已有的原始 CI 结论与 diff 统计，`finish_review()` 读车道分支里的 `.lane-review/PR-<n>-evidence.md` 校验后写进 `reviews.json` 的 `evidence` 字段 | openswarm/lib/review/evidence.py、openswarm/lib/dispatch/engine.py:1658 review_item()、openswarm/lib/dispatch/engine.py:1887 finish_review()
CHECK: 交付二：lane 可附初步意见段，必须标注「参考、不算判定」 | pass | `openswarm/tests/test_review_evidence.py:FailureModeTests.test_an_unmarked_opinion_section_is_refused` 期望 errors 含 `opinion-not-marked`；本包第 5 段的标题带「参考，不算判定」，解析结果 opinion.marked=true
CHECK: 交付三：缺段的证据包在 finish_review 侧记为不完整并留痕 | pass | `openswarm/tests/test_dispatch_evidence_pack.py:FinishReviewEvidenceTests.test_a_pack_without_a_section_is_recorded_as_incomplete` → reviews.json 的 evidence.complete=false、evidence.missing=["checks"]；`test_a_missing_pack_is_recorded_as_incomplete` → missing=["ci","testlog","diffstat","checks"]
CHECK: 验收一：任选一张在途 PR 走一轮新格式 review，证据包五段齐全且可被脚本解析出结构化字段 | pass | 本包（PR #1806 的头）；`python3 tools/check-review-evidence-pack.py docs/evidence/review-pack/in-flight-pr-1788.md` → sections : ci, testlog, diffstat, checks, opinion；missing : -；errors : -；complete : yes
CHECK: 验收二：单元测试覆盖格式拼装与缺段检测 | pass | `cd openswarm && python3 -m unittest tests.test_review_evidence tests.test_dispatch_evidence_pack` → Ran 64 tests in 0.047s / OK；拼装在 test_review_evidence.py 的 BuildEvidenceTests，缺段检测在 MissingSectionTests 与 FailureModeTests
CHECK: 验收三：相关测试全过 | pass | 本 PR 的两个测试模块 64 项全过；全量套件在本沙箱剩 9 项失败：8 项是环境限制（`cd openswarm && python3 -m unittest tests.test_fleet_refresh tests.test_pi_isolation` 在 base（main 850ad326e，不含本 PR）上同样是 Ran 8 tests in 0.270s / FAILED (failures=3, errors=5)，原始日志见 docs/evidence/review-pack/pr-1806-base-env-testlog.txt），第 9 项是 test_pi_broker 的偶发 ConnectionRefusedError，单独重跑在本树与 base 上都通过
CHECK: 验收四：有效行 ≥30 | pass | `git diff --numstat 850ad326e..HEAD` 按 thin-PR 门口径算得 effective_lines=1541（代码与测试文件的新增+删除行，文档不计）
CHECK: 失败模式一：证据包退化为自由文本（不可解析） | pass | `test_free_text_is_not_a_pack` → parse_evidence("The PR looks fine to me, tests pass.") 的 missing=["ci","testlog","diffstat","checks"]、complete=false
CHECK: 失败模式二：初步意见混进证据段冒充结论 | pass | `test_a_verdict_line_in_the_pack_is_refused` 与 `test_a_judgement_line_in_the_pack_is_refused` → errors 含 `verdict-in-pack`
CHECK: 失败模式三：测试日志只给结论不给原始依据 | pass | `test_a_test_log_without_the_raw_lines_is_refused` → errors 含 `testlog-no-raw`；`test_a_test_log_without_a_link_is_refused` → errors 含 `testlog-no-link`
CHECK: 上一轮 BLOCKER（恢复获取 PR #1806 diff 的途径） | pass | 离线可得：`git log --oneline -1 rebase-pr-1806` 取 PR 当前头；`git diff 850ad326e..<head> --stat` 无需网络；本分支的 `engine.py` 有 `local_diff()`/`local_head_ref()`/`local_base_ref()`，`review_item()` 在 `gh pr diff` 不可用时改用本地 git 对象算 diff 并在任务里注明来源
CHECK: B1（上一轮 BLOCKER）：已知 SHA 时只使用与该 SHA 精确匹配的本地提交；取不到时阻止以旧 diff 审查当前头 | pass | 改动：`openswarm/lib/dispatch/engine.py:1752 local_head_ref()` 在 sha 已知时只接受 `same_commit()`（engine.py:1780）判定为同一 commit 的 ref，不再回退到第一个旧 ref；`openswarm/lib/dispatch/engine.py:1802 local_diff()` 因此拿不到旧 diff；`engine.py:1658 review_item()` 在 head SHA 已知却没有可用 diff 时记 blocked 日志并返回 None；`engine.py:2077 dispatch_reviews()` 跳过 None 的 review item（不派发审查）。验证测试：`openswarm/tests/test_dispatch_evidence_pack.py:HeadBoundDiffTests`（5 项：旧 ref 不被当作 head、local_head_ref 不返回旧 ref、精确匹配时返回 head、blocked 写进日志、不派发审查）
CHECK: B2（上一轮 BLOCKER）：交付说明按 B1 编号逐条列出对应改动与验证测试 | pass | 本段按 B1、B2、B3 逐条给出改动位置与验证测试；本包与 .lane-review/PR-1806.md 同为本轮的交付说明
CHECK: B3（上一轮 BLOCKER）：增加覆盖实际 tick/审查终态路径的集成测试，验证完整和缺段证据包的读取与持久化 | pass | `openswarm/tests/test_dispatch_evidence_pack.py:ReviewTerminalIntegrationTests`（4 项）走真实的 tick → probe → harvest → finish → finish_review 路径：车道分支里的证据包被读出，完整包与缺段包（缺 checks、没有包）分别写进 reviews.json 的 evidence（complete=true / complete=false + missing），缺段还写进 dispatch.log
CHECK: 范围：本 PR 不应夹带与本卡无关的文件 | pass | 重基后的树里没有新增 `openswarm/tests/*.so`：`git diff --name-status 850ad326e..HEAD` 里没有二进制产物（main 上原有的 2 个 `openswarm/tests/.dde4df7ff*` 是历史遗留，不在本 PR 的改动里）
CHECK: 正确性：被删文件的 diff 统计要记在它自己的路径上 | pass | `openswarm/lib/dispatch/engine.py:numstat_of()` 把删除文件的 `+++ /dev/null` 换回 `---` 里的路径；回归测试 `openswarm/tests/test_dispatch_evidence_pack.py:DiffstatOfTests.test_a_deleted_file_counts_under_its_own_name`
CHECK: 正确性：原始依据里的竖线不能被解析器截断 | pass | `openswarm/lib/review/evidence.py:split_fields()` 对 CHECK/CI/TEST 字段用 maxsplit=2，原始依据里的 `|` 保留在第三段；回归测试 `openswarm/tests/test_review_evidence.py:ParseEvidenceTests.test_the_raw_evidence_keeps_a_pipe_inside_it`

## 5. Preliminary opinion (初步意见 · 参考，不算判定)
OPINION: 交付物（证据包格式、finish_review 接线、单元测试）按卡里的验收逐条核对下来是齐的；上一轮的三条 BLOCKER 这次都落了地：B1 的旧 ref 兜底改成了「只认精确匹配的 head，取不到就不派发审查」，B2 的交付说明按编号逐条列出了改动与验证测试，B3 补了走真实 tick 的集成测试。CI 状态在离线环境仍然读不到，这一项只能由联网的一轮复核确认。以上是参考意见，不是判定。
