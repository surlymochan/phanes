# #17 子任务：四张回归任务卡的基线验证现状

只读调研，未改动任何被调研代码。本文件记录 `cursor-acp-lane`、`cursor-steer-report`、
`dispatch-approval-gate`、`process-search-results` 四张回归任务卡的**定义位置**、**当前验证命令**、
**基线是否通过**（能本地跑的已跑并贴出结果），以及**如何改成"基线失败、修复后通过"**。

Refs #17（子任务 #112）。

---

## 0. 结论摘要

| 任务卡 | 当前 verify 命令 | 基线是否通过 | 本地是否实测 | 判定 |
| :--- | :--- | :--- | :--- | :--- |
| `cursor-acp-lane` | `python3 -m unittest discover -s tests` | **是（exit 0）** | 是（python3 3.13.5） | 无法证明变更 |
| `cursor-steer-report` | `python3 -m unittest discover -s tests` | **是（基线树全绿；本沙箱因宿主机 uid 不在 /etc/passwd 报无关错误，见 §2.2）** | 是（python3） | 无法证明变更 |
| `dispatch-approval-gate` | `bun run typecheck:node` | **是（静态类型检查，与运行时行为无关）** | 否（本机无 bun / 无 node_modules） | 无法证明变更 |
| `process-search-results` | `bun run typecheck:renderer` | **是（同上）** | 否（同上） | 无法证明变更 |

四张卡的 verify 都**在基线上返回 0**，因此卡片无法区分"executor 做了修复"与"executor 什么都没做"。
机制原因见 §1.3：回归运行的工作树就是从 `base_commit` 拉出来的（`openswarm/bin/openswarm:5221` → `:4848`、`:4862`），
凡是基线能过的命令，在修复前也一定能过。

---

## 1. 任务卡定义与验证命令在哪里

### 1.1 卡片本体不在本仓库（在外部 `agent-knowledge` 仓库）

卡片目录约定为 `<knowledge.repo>/regression/<id>/{card.md,meta.yaml}`：

- `openswarm/bin/openswarm:212` — `DEFAULT_KNOWLEDGE_REPO = "~/work/agent-knowledge"`
- `openswarm/bin/openswarm:11156` — 卡片根目录 = `<knowledge repo>/regression`
- `openswarm/bin/openswarm:11155-11178` — `regress_load_cards()`：按 id 找目录并逐张解析
- `openswarm/bin/openswarm:11127-11153` — `regress_resolve_card()`：读 `card.md`/`meta.yaml`，并解析 `base_commit`
- `openswarm/bin/openswarm:11089-11126` — `regress_card_meta()`：卡片元数据字段与校验
- `tools/cards/c1-58.md:3-4` — "Cards live in `regression/<id>/{card.md,meta.yaml}`"（同一约定）

本工作树（phanes）里**没有** `agent-knowledge` 仓库的副本（`ls` 无 `regression/` 目录，`find . -name "*.yaml"` 只有 openbot 自带的
yml/toml），因此四张卡的 `card.md` / `meta.yaml` 原文无法在此直接读到；下面引用的 verify 命令来自本仓库内的权威记录：

- `docs/audit/issue-17-card-baselines.md:6-9` — 四张卡 id 的清单
- `docs/audit/issue-17-card-baselines.md:13-16` — 四张卡在 `agent-knowledge` 中的目录
- `docs/audit/issue-17-card-baselines.md:188-193` — 汇总表：卡 id / 目标仓库 / `base_commit` / `source_commit` / **当前 verify 命令**
- `docs/audit/issue-17-card-baselines.md:31-73`、`:75-117`、`:119-150`、`:152-184` — 逐卡小节（含各自 verify 命令与基线/修复提交）
- `tools/cards/c1-58.md:10-12` — “Cards `dispatch-approval-gate` and `process-search-results` use a verify
  (`bun run typecheck:node` / `bun run typecheck:renderer`) that already passes at `base_commit`, so the card cannot show
  that an executor made the change.”
- `tools/cards/c1-58b.md:8` — 要求把 `process-search-results` 的检查从 Vitest fixture 改成 `scripts/` 下的 E2E 脚本
- `tools/cards/c1-58b.md:14-16` — 同上问题陈述（重复记录）

> 说明：`docs/audit/issue-17-card-baselines.md` 是本仓库已有的 Issue #17 审计稿；本文件是对它的**复核 + 实测补强**
> （原审计稿里的 openbot 结论是推理，本文件补了 git 对象级的证据；openswarm 结论本文件重跑并复现）。

### 1.2 `meta.yaml` 里 `verify` 字段的语义（代码依据）

- `openswarm/bin/openswarm:11095` — 必填字段：`("id", "card_type", "repo", "base_commit", "verify", "difficulty", "est_minutes")`
- `openswarm/bin/openswarm:11107-11108` — `verify` 必须是非空字符串
- `openswarm/bin/openswarm:11117-11123` — `verify_fails_on_base` 允许 `true` / `false` / `'unknown'`（`'unknown'` 归一为 `None`），缺省 `False`
- `openswarm/tests/test_regress.py:60-67` — `meta.yaml` 样例（`verify:` 与 `verify_fails_on_base:` 字段）
- `openswarm/tests/test_regress.py:117-122` — `verify_fails_on_base: unknown` 归一为 `null`
- `openswarm/tests/test_regress.py:124-145` — 非法取值只影响该卡并报 `verify_fails_on_base must be true, false, or 'unknown'`

### 1.3 "基线失败、修复后通过" 的判定机制（为什么 verify 必须在基线挂掉）

- `openswarm/bin/openswarm:11252-11254` — 仅当 `verify_fails_on_base` 为真，才在 `base_commit` 上先跑一次 verify
  （`verify_in_worktree(card["repo_path"], meta["base_commit"], meta["verify"], ...)`）
- `openswarm/bin/openswarm:9748-9772` — `verify_in_worktree()`：`git worktree add --detach <tmp> <rev>` 后在该目录执行 verify
  （`:9754` 建树、`:9765` 执行、`:9767` 清理）
- `openswarm/bin/openswarm:11289-11296` — 结果落库：`"verify_fails_on_base"`, `"base_verify"`, `"pass": verify_rc == 0`（`:11292`）
- `openswarm/bin/openswarm:11312-11313` — lane 路径同样支持 base verify
- `openswarm/bin/openswarm:5221` + `:4843-4871` — **运行用的工作树就是从 `base_commit` 拉出来的**
  （`:4848` 解析 `base_rev`、`:4862` `git worktree add -b openswarm/<rid> <wt> <base_rev>`）

推论：executor 的工作树起点就是基线，所以**只要 verify 在基线上是绿的，修复前后都是绿的**，卡片毫无鉴别力。
必须换成"在基线上必然非零"的靶向命令，并置 `verify_fails_on_base: true`。

---

## 2. 逐卡现状

### 2.1 `cursor-acp-lane`

- 目标仓库：`openswarm`
- `base_commit`：`9cbcb4040`（"openswarm(codex): Fix resident lanes on a real host"）
- `source_commit`：`355f28487`（"openswarm(codex): Add Cursor Agent CLI lane over ACP"）
  （两者见 `docs/audit/issue-17-card-baselines.md:37-38`、`:190`）
- 当前 verify：`python3 -m unittest discover -s tests`（`docs/audit/issue-17-card-baselines.md:41`、`:190`）

**基线是否通过：是（实测 exit 0）。**

复现方式（只读导出，不动 git 状态）：

```bash
git archive 9cbcb4040 | tar -x -C $SCRATCH/9cbcb4040
cd $SCRATCH/9cbcb4040 && python3 -m unittest discover -s tests
```

结果：

```
........................................................
----------------------------------------------------------------------
Ran 56 tests in 34.930s

OK
EXIT=0
```

代码依据：基线树 `tests/test_lane.py` 只有 10 个测试方法，**没有任何 `test_cursor*` 方法**
（`grep -n "def test_cursor" tests/test_lane.py` 在基线树上无输出）；`tests/fixtures/` 下只有
`fake_app_server.py`、`fake_pi_rpc.py`、`state.json`，**没有** `fake_cursor_acp.py`。
Cursor lane 的实现在基线树里根本不存在，`discover` 自然全绿 → 卡片无法证明任何变更。

**改成"基线失败、修复后通过"：** 把 verify 换成点名 Cursor 用例的靶向命令。

```bash
PYTHONPATH=tests python3 -m unittest test_lane.LaneTests.test_cursor_assign_selects_model_and_commits_without_verify
```

实测：

- 基线 `9cbcb4040`：

  ```
  ERROR: test_cursor_assign_selects_model_and_commits_without_verify (unittest.loader._FailedTest...)
  AttributeError: type object 'LaneTests' has no attribute 'test_cursor_assign_selects_model_and_commits_without_verify'
  Ran 1 test in 0.000s
  FAILED (errors=1)
  EXIT=1
  ```

- 修复 `355f28487`（该方法见该提交 `tests/test_lane.py:142`）：

  ```
  .
  Ran 1 test in 1.524s
  OK
  EXIT=0
  ```

同时在 `meta.yaml` 置 `verify_fails_on_base: true`（`openswarm/bin/openswarm:11252-11254` 才会去基线跑一次并留证）。

**布局陷阱（必须注意）**：基线树（`9cbcb4040`）是 openswarm 独立仓库时代，`tests/` 在**仓库根**；
当前 phanes 单仓里是 `openswarm/tests/`（`git log --follow openswarm/tests/test_lane.py` 显示该文件是被
subtree 迁入的：`a55d20b61 Add 'openswarm/' from commit 'eb0ea4c1...'`）。而 base verify 的 cwd 是
**基线树根目录**（`openswarm/bin/openswarm:9754-9765`），运行 verify 的 cwd 是 `prepare_workspace` 返回的
`<worktree>/<repo 相对路径>`（`openswarm/bin/openswarm:4867-4868`），两者目录层级可能不同。
在当前 phanes 单仓根目录实测 `python3 -m unittest discover -s tests` 会直接失败：

```
ImportError: Start directory is not importable: 'tests'
EXIT=1
```

——即"失败"了，但原因是路径错，不是行为缺失，**这种失败不能作为反证**。
建议：把 `meta.repo: openswarm` 指向 openswarm 独立仓库检出（该仓库 HEAD 与基线同为根级 `tests/` 布局），
或使用对布局不敏感的写法：

```bash
sh -c 'd=$(pwd); [ -d "$d/tests" ] || d="$d/openswarm"; cd "$d" && PYTHONPATH=tests python3 -m unittest test_lane.LaneTests.test_cursor_assign_selects_model_and_commits_without_verify'
```

**环境脆弱性提示**：在 `355f28487` 上跑整包 `discover` 会因宿主机 uid 不在 `/etc/passwd` 而报
`KeyError: 'getpwuid(): uid not found: 1000'`（`tests/test_lane.py:183` → `bin/openswarm:2652`
`pwd.getpwuid(os.getuid())`），退出码 1。这是与本卡无关的宿主环境问题，但说明
"整包 discover" 作为卡片 verify 不稳定，靶向用例更稳。

### 2.2 `cursor-steer-report`

- 目标仓库：`openswarm`
- `base_commit`：`355f28487`（"openswarm(codex): Add Cursor Agent CLI lane over ACP"）
- `source_commit`：`3715f5bd0`（"openswarm(pi): Cursor lane steer keeps the final report"）
  （见 `docs/audit/issue-17-card-baselines.md:81-82`、`:191`）
- 当前 verify：`python3 -m unittest discover -s tests`（`docs/audit/issue-17-card-baselines.md:85`、`:191`）

**基线是否通过：是（基线树本身不含任何覆盖本卡行为的用例）。**

代码依据（`git diff 355f28487 3715f5bd0`）：

- `bin/openswarm`：新增 `REPORT_INSTRUCTION` 常量；steer 续跑时把 report 指令追加进 correction prompt；
  回合正常结束但没打印 JSON report 时，error_class 由 `exec_error` 改为 `no_report`，
  并且"有改动但没 report"判为 `partial`（现 HEAD 对应 `openswarm/bin/openswarm:3261`、`:7720`、`:7913`、`:7947`、`:8031-8035`）。
- `tests/test_lane.py`：新增 3 个用例 `test_cursor_turn_without_report_is_partial` /
  `..._honours_verify` / `..._or_change_is_failed`，并在 `test_cursor_steer_cancels_then_continues_same_session`
  里补了 `your FINAL message must be ONLY a JSON object` 的断言。
- `tests/fixtures/fake_cursor_acp.py`：新增 7 行 fixture。

实测（基线 `355f28487` 树）：

- 靶向命令 `PYTHONPATH=tests python3 -m unittest test_lane.LaneTests.test_cursor_turn_without_report_is_partial`：

  ```
  ERROR: test_cursor_turn_without_report_is_partial (unittest.loader._FailedTest...)
  AttributeError: type object 'LaneTests' has no attribute 'test_cursor_turn_without_report_is_partial'
  Ran 1 test in 0.000s
  FAILED (errors=1)
  EXIT=1
  ```

- 修复 `3715f5bd0`（方法见该提交 `tests/test_lane.py:243`）：

  ```
  .
  Ran 1 test in 1.467s
  OK
  EXIT=0
  ```

- 补充：`test_cursor_steer_cancels_then_continues_same_session` 在**基线上就已存在且通过**
  （实测 `EXIT=0`），修复提交只是给它补断言 → **单独用它无法反证**，必须配合新增的 3 个用例。

- 补充（诚实说明）：在**本沙箱**里跑基线整包 `python3 -m unittest discover -s tests` 的退出码是 1，
  但失败原因是 §2.1 提到的 `pwd.getpwuid()` 宿主环境问题
  （`test_cursor_systemd_unit_is_printed_not_installed`），**与本卡行为无关**；
  原审计稿 `docs/audit/issue-17-card-baselines.md:96-99` 记录的是正常宿主上的 exit 0。
  结论不冲突：基线树里没有任何断言覆盖"steer 续跑要保留 report 指令"和"无 report 判 partial"这两件事。

**改成"基线失败、修复后通过"：**

```bash
PYTHONPATH=tests python3 -m unittest \
  test_lane.LaneTests.test_cursor_turn_without_report_is_partial \
  test_lane.LaneTests.test_cursor_turn_without_report_honours_verify \
  test_lane.LaneTests.test_cursor_turn_without_report_or_change_is_failed
```

（基线实测 `EXIT=1`，`3715f5bd0` 实测 `EXIT=0`。）并置 `verify_fails_on_base: true`。
布局注意事项同 §2.1。

### 2.3 `dispatch-approval-gate`

- 目标仓库：`openbot`
- `base_commit`：`5b37965fa1`（"Read the real openswarm output and explain dispatch failures"）
- `source_commit`：`317be65636`（"Ask the person before a risky dispatch runs"）
  （见 `docs/audit/issue-17-card-baselines.md:125-126`、`:192`）
- 当前 verify：`bun run typecheck:node`（`docs/audit/issue-17-card-baselines.md:129`、`:192`；
  问题陈述见 `tools/cards/c1-58.md:10-12`）
- 该脚本定义：`openbot/package.json:165` → `"typecheck:node": "bun scripts/type-checker.ts --noEmit -p tsconfig.node.json"`

**基线是否通过：是。** 依据：

1. `typecheck:node` 只做 TS 类型检查（`--noEmit`），完全不执行运行时逻辑；本卡要证明的是
   "风险调度必须先过 approval gate"这种**运行时控制流**，类型检查天然测不到。
2. 基线树里根本不存在该功能：`git diff 5b37965fa1 317be65636 -- src/backend/dispatch/dispatch-tools.ts` 显示修复提交
   才新增 `risk` schema 字段、`RISK_RULES` 关键词表、`isRiskyDispatch()`，以及
   `handleDispatchTool(..., approve: DispatchApproval)` 的审批分支（拒绝时返回
   `error.backend.dispatchApprovalDenied` / `error.backend.dispatchApprovalMissing`）。
   基线代码里没有这些符号，类型检查无从失败。
3. 基线树 `scripts/dispatch-approval-e2e.ts` 不存在（`git cat-file -e 5b37965fa1:scripts/dispatch-approval-e2e.ts`
   → `fatal: path 'scripts/dispatch-approval-e2e.ts' does not exist in '5b37965fa1'`），
   而修复提交新增了它（`git show --stat 317be65636` → `scripts/dispatch-approval-e2e.ts | 317 +++++++++++++++++++++`）。

**本地未能实跑（如实说明）**：本机没有 `bun`（`bun --version` → `command not found`），
`openbot/node_modules` 也不存在，且不允许联网安装依赖，因此 `bun run typecheck:node` 与
`bun scripts/dispatch-approval-e2e.ts` 均未在此环境执行；上述结论为代码阅读 + git 对象级证据。

**改成"基线失败、修复后通过"：**

```bash
bun scripts/dispatch-approval-e2e.ts
```

- 基线行为：脚本文件不存在 → `bun` 报脚本找不到、退出码非 0 → **base 失败**；
  即便把脚本搬到基线跑，基线的 `handleDispatchTool` 也没有 `approve` 门禁，会直接启动调度 → 断言失败。
- 修复后行为：脚本断言"未获批准前不得启动 executor"（现 HEAD `openbot/scripts/dispatch-approval-e2e.ts:143`
  `an executor started before approval`）、"拒绝/缺失时返回 `error.backend.dispatchApprovalDenied` /
  `error.backend.dispatchApprovalMissing`"（`:165-171`）、"不残留待批项"（`:173`），退出码 0。
- 配套动作（`tools/cards/c1-58.md:18-20` 已要求）：
  1. 把该 E2E 脚本列入卡片的"预期变更"（executor 需要把脚本一起提交，否则修复后 verify 也跑不起来）；
  2. `meta.yaml` 置 `verify_fails_on_base: true`；
  3. 证据写入 `.openbot-build/rc58/<card-id>.json`（`tools/cards/c1-58.md:25-26`）。

### 2.4 `process-search-results`

- 目标仓库：`openbot`
- `base_commit`：`ef3993b73b`（"Show each turn's response process and animate the working avatar"）
- `source_commit`：`00daa495da` / `feeaba4ae0`（"Open the response process when a search hit selects a process note" /
  "Show the response process with a working avatar (fuses #18 and #23)"）
  （见 `docs/audit/issue-17-card-baselines.md:158-159`、`:193`）
- 当前 verify：`bun run typecheck:renderer`（`docs/audit/issue-17-card-baselines.md:162`、`:193`；
  问题陈述见 `tools/cards/c1-58.md:10-12`）
- 该脚本定义：`openbot/package.json:166` → `"typecheck:renderer": "bun scripts/type-checker.ts --noEmit -p tsconfig.web.json"`

**基线是否通过：是。** 依据：

1. 与 §2.3 同理，`typecheck:renderer` 只做类型检查；本卡要证明的是"搜索命中 process note 时展开
   response process 折叠块"这一 DOM 运行时行为，类型检查测不到。
2. 修复的实际代码（`git diff efb269907 feeaba4ae -- src/renderer/src/features/conversation/stores/search-store.ts`）：
   - 命中判定从只认 `dataset.chatSearchMessage` 扩展为同时认 `dataset.chatSearchGroup` +
     `[data-chat-search-item]`（现 HEAD `openbot/src/renderer/src/features/conversation/stores/search-store.ts:94-100`）；
   - 命中后如果消息是 `.response-process` 且存在折叠按钮，则 `disclosure?.click()` 自动展开
     （现 HEAD 同文件 `:131-134`）。
   这两处都是运行时 DOM 操作，类型检查不覆盖。
3. 数据属性侧：`openbot/packages/ui/src/features/conversation/ResponseProcess.tsx:25`
   （`data-chat-search-group={props.message.itemIds?.join(" ")}`）、`:46`（`data-chat-search-item`）、
   `:32`（`aria-expanded`）。

**⚠️ 新发现（卡片 owner 需要处理）**：卡片记的 `base_commit = ef3993b73b` 在**本仓库不可解析**：

```bash
git cat-file -t ef3993b73b0c155133da4a0b8197e0d3965389db
fatal: git cat-file: could not get object info
```

它是上游 `nightly-labs/openbot` 的提交号（openbot 已被 subtree 并入 phanes，见
`db82b6adc Add 'openbot/' from commit 'afcc320667907bdda4b57ac876cc90885b1ffc7a'`）。
若卡片配置的 `regress.repos.openbot` 指向本仓库，基线解析会直接失败，
报错来自 `openswarm/bin/openswarm:11145-11148`（`base_commit is unavailable for <id>`）——卡片连跑都跑不起来。
本仓库里可用的等价锚点：

- `efb269907` "Show collapsible response process notes in chats"（新增 `scripts/response-process-e2e.ts`、
  `packages/ui/src/features/conversation/ResponseProcess.tsx`；父提交 `aa65229c1`）
- `3a0247905` "Animate working avatars and simplify response activity"
- `feeaba4ae` "Show the response process with a working avatar (fuses #18 and #23)"（= 卡片记的 source 侧改动）
  ——其父 `6b72597d4`；`git show 6b72597d4:.../search-store.ts` 中**没有** `chatSearchGroup` / `aria-expanded` 展开逻辑，
  `feeaba4ae` 起才出现。

**改成"基线失败、修复后通过"：**

```bash
bun scripts/response-process-e2e.ts
```

- 基线行为：`scripts/response-process-e2e.ts` 在 `efb269907^`（`aa65229c1`）不存在
  （`git cat-file -e efb269907^:scripts/response-process-e2e.ts` → 不存在），基线跑必然非 0；
- 修复后行为：现 HEAD 的 `openbot/scripts/response-process-e2e.ts`（281 行）断言
  `processNotesAreSearchable`（`:261-265`）、`reopenedHistoryKeepsProcessAndAnswer`（`:257`）等并写出
  `.openbot-build/response-process-e2e/report.json`（`:271`），退出码 0。

**覆盖度缺口（如实记录）**：该 E2E 脚本只覆盖**后端**（process note 落库后可被搜索）；
renderer 侧的"搜索命中后自动展开折叠块"（`search-store.ts:97-99`、`:133`）目前**没有任何脚本覆盖**
（`grep -rl "createSearchStore" openbot/scripts/` 无结果）。要完整覆盖本卡，需要再补一个
`scripts/*-e2e.ts` 驱动 renderer 的脚本；按 `openbot/AGENTS.md:226-230`（"Use E2E tests only. Do not add unit
tests… Vitest files are permitted only for these protected areas"），不能用 Vitest fixture 绕过，
这正是 `tools/cards/c1-58b.md:8` 要求做的转换。

---

## 3. 汇总

| 任务卡 | 卡 id 记录处 | 当前 verify | 基线结果 | 建议 verify | 建议 verify 的基线/修复结果 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `cursor-acp-lane` | `docs/audit/issue-17-card-baselines.md:190` | `python3 -m unittest discover -s tests` | 实测 exit 0（56 tests OK） | `PYTHONPATH=tests python3 -m unittest test_lane.LaneTests.test_cursor_assign_selects_model_and_commits_without_verify` | 实测 exit 1（AttributeError） / exit 0 |
| `cursor-steer-report` | `:191` | `python3 -m unittest discover -s tests` | 基线树不含相关用例（整包在本沙箱因宿主 uid 报无关错误） | `PYTHONPATH=tests python3 -m unittest test_lane.LaneTests.test_cursor_turn_without_report_is_partial` | 实测 exit 1（AttributeError） / exit 0 |
| `dispatch-approval-gate` | `:192` | `bun run typecheck:node` | 静态类型检查，基线无该功能 → exit 0（本机无 bun，未实跑） | `bun scripts/dispatch-approval-e2e.ts` | 基线脚本不存在 → 非 0 / exit 0 |
| `process-search-results` | `:193` | `bun run typecheck:renderer` | 同上；且 `base_commit ef3993b73b` 在本仓库不可解析 | `bun scripts/response-process-e2e.ts` | 基线脚本不存在 → 非 0 / exit 0 |

---

## 4. 统一的落地做法

1. **改 meta.yaml 的 `verify`**：把四张卡的 `verify` 换成上面"建议 verify"列的命令。
2. **置 `verify_fails_on_base: true`**：否则运行器根本不会去基线跑一次
   （`openswarm/bin/openswarm:11252-11254`、`:11312-11313`）。
3. **把新增的检查脚本纳入卡片的"预期变更"**：openbot 两张卡必须让 executor 一并提交
   `scripts/dispatch-approval-e2e.ts` / `scripts/response-process-e2e.ts`
   （`tools/cards/c1-58.md:18-20`；`openbot/AGENTS.md:226-230` 只允许 `scripts/` 下的 E2E）。
4. **openswarm 两张卡注意目录布局**：verify 必须在"基线树根"和"运行工作树"两个 cwd 下都能定位测试目录
   （§2.1 末尾），否则会得到"因路径错而失败"的假反证。
5. **修 `process-search-results` 的 `base_commit`**：改成 `regress.repos.openbot` 实际检出的仓库里能解析的哈希
   （本仓库可用 `aa65229c1` / `6b72597d4` / `efb269907`），否则卡片直接报 `base_commit is unavailable`。
6. **留证**：按 `tools/cards/c1-58.md:25-26`，把 base rc 与 candidate rc 写进 `.openbot-build/rc58/<card-id>.json`。
7. **入库标准（长期）**：卡片入库必须满足"verify 在 `base_commit` 非 0、在 `source_commit` 为 0"；
   禁止用大范围静态类型检查或空泛的 `unittest discover` 作为单点功能变更的验证命令
   （与 `docs/audit/issue-17-card-baselines.md:197-202` 的建议一致）。

---

## 5. 本次实测记录与环境限制

| 项 | 结果 |
| :--- | :--- |
| 运行环境 | `python3 3.13.5`；`node v22.23.3`；**无 `bun`**；`openbot/node_modules` 不存在；无网络安装 |
| 只读导出基线树 | `git archive <rev> \| tar -x -C $SCRATCH/<rev>`（未改 git 状态、未建 worktree、未提交） |
| 实测通过 | `9cbcb4040` 整包 discover → exit 0（56 tests）；`9cbcb4040` 靶向用例 → exit 1；`355f28487` 靶向用例 → exit 0；`355f28487` 靶向（steer-report 用例）→ exit 1；`3715f5bd0` 靶向 → exit 0；`355f28487` `test_cursor_steer_cancels_then_continues_same_session` → exit 0（说明该用例单独无法反证） |
| 未能实测 | `bun run typecheck:node`、`bun run typecheck:renderer`、`bun scripts/dispatch-approval-e2e.ts`、`bun scripts/response-process-e2e.ts`（本机无 bun / 无依赖）；openbot 侧结论来自代码阅读与 git 对象级证据（`git cat-file -e` / `git diff` / `git show --stat`） |
| 顺带发现 | `process-search-results` 的 `base_commit ef3993b73b` 在本仓库不可解析；整包 `unittest discover` 在本沙箱会因 `pwd.getpwuid()` 缺宿主用户而误报失败 |

## 6. 建议的后续项（不属于本只读任务）

- 修 `process-search-results` 卡的 `base_commit`（见 §4.5）。
- 为 renderer 侧"搜索命中自动展开 response process"补一个 `scripts/*-e2e.ts`（受 `openbot/AGENTS.md:226-230` 约束）。
- 把 §1.3 的"verify 必须基线非 0"写进回归卡入库规范（现有审计稿 `docs/audit/issue-17-card-baselines.md:197-202` 已有建议）。
