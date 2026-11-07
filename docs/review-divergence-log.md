# 分歧日志（lane 判定 vs tester 影子判定）

任务：[#1789](https://github.com/surlymochan/phanes/issues/1789)（Epic [#1785](https://github.com/surlymochan/phanes/issues/1785) 子卡 P1-3）。
状态：**基建已就绪**（数据格式 + 工具 + 文档 + 端到端演示）。tester 车道本身尚未上线；本卡的影子判定由本地确定性判定器 `tools/review-shadow-tester.py` 真实跑出来（§4.1），它只是机器可判定的部分，**本卡不产生任何切换结论**。

## 1. 目的与范围

合并门从「lane 审查说了算」切到「tester 判定说了算」之前，需要可量化的影子期数据。这张卡只做**记录基建**：

- 每张走完 review 的 PR，按**轮次（round）**并排记录 `lane 判定` 与 `tester 影子判定`；
- PR 走到结局（合并 / 关闭 / 再审）后，**回填**结局与「谁对」；
- 用**同一口径**统计两方各自的样本数与准确率，以及两方的**一致率**。

不在本卡范围：tester 影子车道的自动写入（见 §9 后续）、合并门切换本身、任何基于本数据的策略改动。

## 2. 数据格式

一个 JSON Lines 文件（UTF-8、LF、每行一个 JSON 对象）承载两类事件。两类事件共享同一把键 `(pr, head, round)`：**同一轮的判定与结局成对出现，但永远分行写入，文件只追加、不改写**。

### 2.1 `verdict` 事件（双判定入账）

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `schema` | string | 是 | 固定 `phanes.review-divergence/1` |
| `kind` | string | 是 | 固定 `verdict` |
| `pr` | int | 是 | PR 号，正整数 |
| `head` | string | 是 | 被审 head sha，7–40 位十六进制（小写） |
| `round` | int | 是 | 轮次，同一 PR 从 1 递增 |
| `lane_verdict` | string | 是 | `APPROVE` / `CHANGES` |
| `tester_verdict` | string | 是 | `PASS` / `FAIL` / `INSUFFICIENT`（证据不足） |
| `recorded_at` | string | 是 | 记录时间，ISO-8601 带时区（UTC）。指**这条记录写进日志的时刻**：影子期里它就是 tester 影子判定跑完的时刻；回溯补录历史轮次时它晚于 lane 判定时间，lane 判定时间写在 `note` 里 |
| `evidence` | string | 否 | 判定证据链接（lane 审查评论 URL 或影子报告 URL） |
| `note` | string | 否 | 自由备注 |

### 2.2 `outcome` 事件（结局回填）

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `schema` / `kind` | string | 是 | `phanes.review-divergence/1` / `outcome` |
| `pr` / `head` / `round` | — | 是 | 与被回填的那轮**完全一致** |
| `outcome` | string | 是 | `merged` / `closed` / `re_review` |
| `outcome_at` | string | 是 | 结局发生时间，ISO-8601 带时区 |
| `correct` | string | 是 | 谁对回填：`both` / `lane` / `tester` / `neither` / `undecided`（见 §5） |
| `backfilled_at` | string | 是 | 回填动作时间，ISO-8601 带时区 |
| `note` | string | 否 | 自由备注（如结局出处） |

### 2.3 折叠（读）规则

读日志时把事件按 `(pr, head, round)` 分组，**同类事件后写的覆盖先写的**（last wins）。因此：

- 同一 PR 的多轮判定是多个不同的键，**互不覆盖**；
- 回填是**追加**一条 `outcome` 事件，不是就地改写 `verdict` 行；
- 写错了要修正，就再追加一条同类事件（工具用 `--force` 放行），旧行仍在文件里，历史不被抹掉。

## 3. 存放位置与读写约定

### 3.1 位置

日志路径按以下顺序解析（`tools/review-divergence-log.py:94` `log_path()`）：

1. 命令行 `--log PATH`；
2. 环境变量 `REVIEW_DIVERGENCE_LOG`；
3. `$OPENSWARM_HOME/review-divergence.jsonl`（`OPENSWARM_HOME` 由 fleet 运行时目录提供，典型值 `~/.claude/openswarm/`）；
4. `~/.claude/openswarm/review-divergence.jsonl`（`OPENSWARM_HOME` 未设时的兜底）。

仓库内随版本走的影子期证据副本放在 `docs/evidence/review-divergence/divergence-log.jsonl`（本卡的演示数据），决策时引用的快照也放同一目录；每次 tester 影子判定的原始报告放在 `docs/evidence/review-divergence/shadow/pr-<PR>-r<轮次>.json`，日志行的 `evidence` 字段指向它。

### 3.2 读写约定

- **只追加**：任何命令都不改写既有行；回填与修正都是新行。
- **一次写入一行**：`append_event()`（`tools/review-divergence-log.py:234`）用 `O_APPEND` 单次 `write()` 落盘，并行写不会互相穿插。
- **单写者**：一个文件同一时刻只有一个写入者（lane 或 tester 侧各写自己的事件），避免语义冲突。
- **坏行不跳过**：文件里出现非 JSON、schema 不认识、字段不合规的行时，命令以退出码 2 报错，不静默丢弃（`read_events()`，`tools/review-divergence-log.py:216`）。
- **时间一律带时区**：naive 时间戳（如 `2026-11-02 17:33:59`）被拒绝。
- **退出码**：0 成功；2 输入非法或日志损坏（`main()`，`tools/review-divergence-log.py:581`）。

## 4. 工具用法

工具：`tools/review-divergence-log.py`（仅标准库，Python 3.9+）。

```sh
# 追加一轮双判定（round 省略时按该 PR 自动递增）
python3 tools/review-divergence-log.py --log "$LOG" append \
  --pr 1751 --head <sha> --lane APPROVE --tester PASS \
  --recorded-at 2026-11-02T18:57:04Z --evidence "<review url>"

# 回填结局（round 省略时取该 head 唯一待回填的那轮）
python3 tools/review-divergence-log.py --log "$LOG" backfill \
  --pr 1751 --head <sha> --outcome merged --outcome-at 2026-11-02T19:00:37Z

# 未回填清单（结局回填靠人工记忆时的兜底查询）
python3 tools/review-divergence-log.py --log "$LOG" pending

# 统计：按判定方分组的样本数/准确率 + 一致率
python3 tools/review-divergence-log.py --log "$LOG" stats --json

# 折叠后的全部轮次
python3 tools/review-divergence-log.py --log "$LOG" list --json
```

输入校验（任一不满足即退出码 2，且**不写文件**）：

| 输入 | 规则 |
| --- | --- |
| `--pr` | 正整数 |
| `--head` | 7–40 位十六进制 |
| `--round` | 正整数；同一 `(pr, head)` 已有判定时必须显式给出，否则报错并提示下一个可用轮次（防止误开新一轮） |
| `--lane` | `APPROVE` / `CHANGES`（别名 `approve`、`通过`、`不通过` 等） |
| `--tester` | `PASS` / `FAIL` / `INSUFFICIENT`（别名 `通过`、`不通过`、`证据不足`） |
| `--outcome` | `merged` / `closed` / `re_review`（别名 `合并`、`关闭`、`再审`） |
| 时间戳 | ISO-8601 且必须带时区 |
| 回填目标 | 必须存在对应的 `verdict` 轮次；重复回填需 `--force`；同一 `(pr, head)` 有多轮待回填时必须显式 `--round` |

### 4.1 tester 影子判定器：`tools/review-shadow-tester.py`

分歧日志只管记账，**判定从哪来**必须可核对。`tools/review-shadow-tester.py`（仅标准库）在一个**真实 head** 上跑机器可判定的检查，产出三档判定：

```sh
# 跑一次影子判定，报告写到文件（退出码 0=PASS，1=FAIL，3=INSUFFICIENT）
python3 tools/review-shadow-tester.py --pr 1751 \
  --head f3af3db83c36eaa0bf7c816a2994c6e82d6774ee \
  --json-out docs/evidence/review-divergence/shadow/pr-1751-r2.json
```

检查项与判定口径（`decide()`，`tools/review-shadow-tester.py:199`）：

| 检查 | 作用 | 角色 |
| --- | --- | --- |
| `diff-hygiene` | diff 里不得混入二进制/构建产物（`.so`、`.o`、`.pyc` …，#1729 质量门口径） | **否决项**：不通过即 FAIL |
| `python-syntax` | 变更的 Python 文件必须能编译 | 证据项 |
| `unit-tests` | 覆盖变更模块的测试文件必须全绿 | 证据项 |
| `cli-smoke` | 变更的入口脚本必须还能启动（`--help`） | 证据项 |

- 任一检查不通过 → **FAIL**；
- 有变更的源文件没有任何检查覆盖（`uncovered` 非空）→ **INSUFFICIENT**；
- 没有任何检查适用于这个 diff（只有文档改动等）→ **INSUFFICIENT**；
- 其余 → **PASS**；
- head 对象不在本地对象库 → **INSUFFICIENT**（拿不到被审 head 是证据不足，不是失败）。

报告里逐项记录检查名、结论（`pass`/`fail`/`skipped`）、检查对象数量与细节，`recorded_at` 是这次运行的时刻。工具只读仓库、不写日志；入账仍由 `review-divergence-log.py append` 完成，避免两个写入者争同一个文件。

> 这个判定器是**本地的确定性检查器**，不是未来上线的 tester 车道：它只看 diff 卫生、语法、单测与入口冒烟，看不到语义缺陷（§7.2 就是一个真实例子）。影子期真正要比较的，是它与 lane 审查的分歧率。

输入校验：head 必须是 7–40 位十六进制（否则退出码 2）；`--base` 省略时取 head 的第一个父提交。

### 4.2 离线复核入口：`tools/check-review-packet.py`

审查沙箱常常没有网络（`git fetch` 解析失败、`gh` 未安装），被审 head 也可能不在对象库里；「看不到材料」就不能审。因此每张卡都随 PR 交一份**纯文本复核包**，本卡的这套材料在 `.lane-review/` 下：

| 文件 | 内容 |
| --- | --- |
| `.lane-review/PR-1803.md` | 审查入口：所有命令、材料清单、上轮阻塞项 → 本轮修复 → 验证的逐条映射 |
| `.lane-review/PR-1803-packet.json` | 复核包清单：每个文件的大小与 sha256、上一轮 head、patch 路径 |
| `.lane-review/PR-1803-heads.txt` | 每个文件的上一轮 blob sha、当前 blob sha、字节数 |
| `.lane-review/PR-1803-round4.patch` | 上一轮 head → 本轮 head 的**完整** unified diff（纯文本，不依赖对象库） |

复核包用一条命令自证（不需要网络、不需要 `gh`）：

```sh
python3 tools/check-review-packet.py
```

它依次检查：

- `manifest`：清单里的每个文件都在，大小与 sha256 对得上；
- `patch`：patch 的每个 hunk 都持有头部声明的行数——被截断的 patch 会报错，而不是被当成更小的 diff；
- `patch-vs-git`：patch 与 `git diff <上一轮 head>..HEAD` 逐字节相同（上一轮 head 不在对象库时跳过）；
- `heads`：清单里的 blob sha 与已提交的文件树一致；
- `evidence`：日志里每个 `verdict` 行都指向一份存在的影子报告，且 PR 号、head、判定三者一致；
- `statistics`：仓库里的 `stats.json` 就是当前日志算出来的统计。

退出码 0 表示全部通过，1 表示有检查失败。

## 5. 判定与回填口径

三方映射到同一个「正面 / 负面」轴（`tools/review-divergence-log.py:65-69`）：

| 判定方 | 判定 | 含义 |
| --- | --- | --- |
| lane | `APPROVE` | 正面（可以过） |
| lane | `CHANGES` | 负面（不能过） |
| tester | `PASS` | 正面 |
| tester | `FAIL` | 负面 |
| tester | `INSUFFICIENT` | **弃权**（证据不足），不计对错 |
| 结局 | `merged` | 正面（这一轮最终合入） |
| 结局 | `closed` | 负面（PR 关闭，未合入） |
| 结局 | `re_review` | 负面（head 变更后进入下一轮审查，本轮未合入） |

「谁对」按上表判定（`who_was_right()`，`tools/review-divergence-log.py:292`）：

| `correct` | 条件 |
| --- | --- |
| `both` | 两方都对 |
| `lane` | 只有 lane 对 |
| `tester` | 只有 tester 对 |
| `neither` | 两方都错 |
| `undecided` | tester 弃权（证据不足），无法并排归因；lane 仍单独计分 |

`correct` 由工具在回填时按上表算出并写进事件，不靠人工记忆；`stats` 会重算一遍，若与文件里记录的值不一致则在 `inconsistent_backfills` 里点名。

## 6. 统计口径

`build_stats()`（`tools/review-divergence-log.py:436`）的规则：

- **只有已回填结局的轮次进入任何比率的分母**。未回填的轮次只出现在 `rounds.pending` 与 `pending` 列表里，**不冒充准确率样本**。
- **有结局但没有成对判定的轮次**（手写日志或日志被截断可能出现）单列在 `incomplete` 里，既不算样本也不进分母。
- **tester 的 `INSUFFICIENT` 是弃权**：计入 `sides.tester.abstained`，不进入 `sides.tester.samples`，因此不算错、也不拉低准确率。
- `sides.<判定方>.samples` = 该方给出判定的已回填轮次数；`accuracy` = `correct / samples`（分母为 0 时为 `null`，不写 0）。
- `agreement` 只在两方都给出可比判定时统计：`APPROVE ≡ PASS`、`CHANGES ≡ FAIL` 记为一致，否则记为分歧；tester 弃权的轮次计入 `not_comparable`，不进入一致率分母。`agreement_rate = agreed / comparable`。
- `attribution` 给出「谁对」的分布（`both` / `lane` / `tester` / `neither` / `undecided` 计数），即合并门切换时最直接要看的一张表。
- `divergence` 给出 `(lane_verdict, tester_verdict)` 的计数矩阵，用于定位分歧形态。
- `outcomes` 给出已回填轮次按结局分类的计数。
- `inconsistent_backfills` 给出「文件里记录的 `correct`」与「按 §5 重算的 `correct`」不一致的轮次。

## 7. 端到端演示（真实 PR，真实影子判定）

演示覆盖两张真实 PR 的五轮 review，两侧的判定都是真的：

- **lane 判定、判定时间、结局、结局时间**取自仓库内已归档的 GitHub 快照 [`docs/evidence/architecture-review/snapshot.json.gz`](evidence/architecture-review/snapshot.json.gz)（窗口 2026-10-26T19:06:11Z ≤ 事件 < 2026-11-02T19:06:11Z）；
- **tester 影子判定全部是 `tools/review-shadow-tester.py` 的真实运行结果**，每次运行留下一份 JSON 报告（`docs/evidence/review-divergence/shadow/pr-<PR>-r<轮次>.json`），报告里有逐项检查的结论与判定理由。

> **口径声明**：这些数字只说明「双判定入账 → 结局回填 → 统计」这条链路在真实数据上跑通了。5 轮里 tester 只有 **3 个可比样本**（2 轮因拿不到 head 而弃权），样本量远不够支撑任何合并门切换结论；本卡也不产生切换结论。

| 轮次 | PR | head | lane 判定（真实） | tester 影子判定（真实运行） | 结局（真实） | 谁对 |
| --- | --- | --- | --- | --- | --- | --- |
| #917 r1 | 917 | `a141b7b371` | APPROVE | INSUFFICIENT（拿不到 head） | re_review | `undecided`（lane 判错） |
| #917 r2 | 917 | `57ed1dae5d` | APPROVE | INSUFFICIENT（拿不到 head） | merged | `undecided`（lane 对） |
| #1751 r1 | 1751 | `e96ad25d2360e6961ad1f12a4bca536cc80e714f` | CHANGES | **FAIL**（diff 混入 2 个 `.so`） | re_review | `both` |
| #1751 r2 | 1751 | `f3af3db83c36eaa0bf7c816a2994c6e82d6774ee` | CHANGES | **PASS**（语法/单测/冒烟全绿） | re_review | `lane` |
| #1751 r3 | 1751 | `b85a501bfba3e08a9af576b129e9e40f13d4e1e0` | APPROVE | **FAIL**（diff 混入 2 个 `.so`） | merged | `lane` |

head 列：#917 两轮用审查评论里公布的 10 位前缀（快照里只有前缀），#1751 三轮用完整 sha（快照的审查评论里有完整 sha）。`recorded_at` 是影子判定的运行时刻（回溯补录历史轮次，所以晚于 lane 判定时间，lane 判定时间写在每行的 `note` 里）。

> **#1751 r3 的判定随环境变化**：上一轮记录的 r3 是 INSUFFICIENT（当时那台机器的对象库里没有 `b85a501bfb…`）；本轮在对象库齐的车道里重跑，拿到的是 FAIL（同样的两个 `.so`）。影子判定是**在某个环境里对某个 head 的一次真实运行**，head 读不到就只能弃权；head 读得到就必须给出判定，不能拿上一轮的弃权当结论。

### 7.1 每一轮判定的出处

lane 侧（快照可核对）：

- **#917 r1**：[lane review APPROVE](https://github.com/surlymochan/phanes/pull/917#issuecomment-6559541442)（2026-11-01T14:50:50Z，head `a141b7b371`）；随后车道 rebase 并 force-push 新 head（[评论](https://github.com/surlymochan/phanes/pull/917#issuecomment-6561235331)，2026-11-01T17:16:13Z），本轮以 `re_review` 收尾——lane 的 APPROVE 与结局不符。
- **#917 r2**：[lane review APPROVE](https://github.com/surlymochan/phanes/pull/917#issuecomment-6561340187)（2026-11-01T17:25:11Z，head `57ed1dae5d`），17:25:27Z 合并。
- **#1751 r1**：[lane review CHANGES](https://github.com/surlymochan/phanes/pull/1751#issuecomment-6583864706)（2026-11-02T17:33:59Z，head `e96ad25d23`），18:19:28Z 车道 rebase（[评论](https://github.com/surlymochan/phanes/pull/1751#issuecomment-6584817588)）。同一 head/run/verdict 的评论在 17:34:22Z 有一次同内容重发，按「一轮 = 一个逻辑判定」只入账一次。
- **#1751 r2**：[lane review CHANGES](https://github.com/surlymochan/phanes/pull/1751#issuecomment-6584969734)（2026-11-02T18:26:41Z，head `f3af3db83c`），18:50:29Z 车道 rebase（[评论](https://github.com/surlymochan/phanes/pull/1751#issuecomment-6585463917)）。
- **#1751 r3**：[lane review APPROVE](https://github.com/surlymochan/phanes/pull/1751#issuecomment-6585646758)（2026-11-02T18:57:04Z，head `b85a501bfb`），19:00:37Z 合并（提交 `9b48c227b0`）。

tester 侧（真实运行，原始报告在 `docs/evidence/review-divergence/shadow/`）：

| 轮次 | tester 判定 | 判定依据（报告里的原文摘要） |
| --- | --- | --- |
| #917 r1 | INSUFFICIENT | head 对象不在本地对象库，影子运行拿不到被审 head——证据不足，不是失败 |
| #917 r2 | INSUFFICIENT | 同上 |
| #1751 r1 | FAIL | `diff-hygiene`：diff 里混进两个与状态聚合无关的构建产物 `openswarm/tests/.dde4dddfeebef63b-00000000.so`、`openswarm/tests/.dde4dddffef7f474-00000000.so` |
| #1751 r2 | PASS | `python-syntax` 2 个变更文件可编译；`unit-tests` `openswarm/tests/test_fleet_status.py` 34 项全绿；`cli-smoke` `tools/fleet-status-page.py --help` 正常退出 |
| #1751 r3 | FAIL | `diff-hygiene`：r3 的 diff 里混进两个构建产物 `openswarm/tests/.dde4df7ffebedc3d-00000000.so`、`openswarm/tests/.dde4df7ffeffd7b8-00000000.so` |

### 7.2 两处可比分歧：r2 与 r3

**r2（lane 对、tester 错）**：lane 给 CHANGES，tester 的机器检查全绿给 PASS，结局是 `re_review`——**lane 判对了，tester 判错了**。

lane 在 r2 的阻塞项（`openswarm/lib/dispatch/fleet_status.py:130`：`epoch()` 对字符串直接 `float(text)`，`"nan"`/`"inf"` 不会被判为无效，后续 `iso()` 调 `datetime.fromtimestamp` 会抛异常）是真实存在的，可以离线复核：

```sh
mkdir -p /tmp/r2 && git archive f3af3db83c36eaa0bf7c816a2994c6e82d6774ee openswarm | tar -x -C /tmp/r2
cd /tmp/r2 && python3 -c "from openswarm.lib.dispatch.fleet_status import iso; print(iso('nan'))"
# -> ValueError: Invalid value NaN (not a number)
```

同一段代码在合并结果 `9b48c227b0` 上返回 `None`（r3 修好了）。

**r3（结局轴与缺陷轴不一致）**：lane 给 APPROVE，tester 给 FAIL，结局是 merged，于是按 §5 的结局轴记成 `lane`（lane 判对）。但 tester 指出的两个 `.so` **确实随 #1751 合进了主干**，可以离线核对：

```sh
git log --oneline --diff-filter=A -- 'openswarm/tests/*.so'
# -> 9b48c227b openswarm(pi): #1718 ... (#1751)
```

也就是说：**结局轴会把「机器检查抓到的真问题」记成 lane 赢**（PR 合了就算正面）。这是本卡在合并门切换前必须先量出来的一处口径偏差——准确率不是「谁抓到了缺陷」，而是「谁和结局一致」。切换决策要同时看 `attribution` 和 `divergence` 两张表，不能只看准确率。

r2 展示的是另一种形态：**机器检查全绿不等于这个 head 没问题**。合并门切换之前，必须先量出 tester 这类机器判定会漏掉多少 lane 能抓到的缺陷——这 5 轮里 tester 只有 3 个可比样本、1 对 2 错，离任何门限都远。

### 7.3 复现命令

```sh
LOG=docs/evidence/review-divergence/divergence-log.jsonl
SHADOW=docs/evidence/review-divergence/shadow

# 1) 跑真实影子判定，拿到报告与判定
python3 tools/review-shadow-tester.py --pr 1751 \
  --head f3af3db83c36eaa0bf7c816a2994c6e82d6774ee --json-out "$SHADOW/pr-1751-r2.json"
# -> verdict PASS（退出码 0）

# 2) 双判定入账（lane 判定来自已发布的审查评论）
python3 tools/review-divergence-log.py --log "$LOG" append --pr 1751 \
  --head f3af3db83c36eaa0bf7c816a2994c6e82d6774ee --round 2 \
  --lane CHANGES --tester PASS --recorded-at <影子运行时刻> \
  --evidence "$SHADOW/pr-1751-r2.json" \
  --note "lane verdict CHANGES from the published lane review at 2026-11-02T18:26:41Z (<url>)"

# 3) 结局回填（结局与结局时间都是真实记录）
python3 tools/review-divergence-log.py --log "$LOG" backfill --pr 1751 \
  --head f3af3db83c36eaa0bf7c816a2994c6e82d6774ee --round 2 \
  --outcome re_review --outcome-at 2026-11-02T18:50:29Z

# 4) 未回填清单与统计
python3 tools/review-divergence-log.py --log "$LOG" pending
python3 tools/review-divergence-log.py --log "$LOG" stats --json
```

回填前的未回填清单（5 轮全部待回填）：

```
PR #917 round 1 head a141b7b371 lane=APPROVE tester=INSUFFICIENT recorded=2026-11-03T08:21:44.342793Z
PR #917 round 2 head 57ed1dae5d lane=APPROVE tester=INSUFFICIENT recorded=2026-11-03T08:21:44.448302Z
PR #1751 round 1 head e96ad25d2360e6961ad1f12a4bca536cc80e714f lane=CHANGES tester=FAIL recorded=2026-11-03T08:21:44.571030Z
PR #1751 round 2 head f3af3db83c36eaa0bf7c816a2994c6e82d6774ee lane=CHANGES tester=PASS recorded=2026-11-03T08:21:45.953899Z
PR #1751 round 3 head b85a501bfba3e08a9af576b129e9e40f13d4e1e0 lane=APPROVE tester=FAIL recorded=2026-11-03T08:21:47.778205Z
5 round(s) without an outcome
```

回填后的统计（完整报告见 [`stats.json`](evidence/review-divergence/stats.json)）：

```
rounds: 5 total, 5 backfilled, 0 pending, 0 incomplete
lane: 5 sample(s), 4 correct, accuracy 0.800, 0 abstained
tester: 3 sample(s), 1 correct, accuracy 0.333, 2 abstained
agreement: 3 comparable, 1 agreed, 2 diverged, rate 0.333, 2 not comparable
divergence: lane=APPROVE tester=FAIL count=1
divergence: lane=APPROVE tester=INSUFFICIENT count=2
divergence: lane=CHANGES tester=FAIL count=1
divergence: lane=CHANGES tester=PASS count=1
attribution: both 1, lane 2, tester 0, neither 0, undecided 2
```

对照 §6 的口径：tester 的 2 次弃权没有进入分母（`samples=3` 而非 5），未回填轮次为 0 所以没有样本被冒充；可比分歧有两处（`#1751 r2`、`#1751 r3`），归因都是 `lane`。

## 8. 失败模式与防护

| 失败模式 | 防护 | 落点 |
| --- | --- | --- |
| 结局回填靠人工记忆 | 回填有**明确入口** `backfill`（无对应判定记录时拒绝执行），并有**未回填清单查询** `pending`；`stats` 里 `rounds.pending` 永远可见 | §4、§6 |
| 同 PR 多轮判定互相覆盖 | 键含 `round`，多轮即多键；同一 `(pr, head)` 再记一轮必须显式 `--round`，否则报错 | §2.3、§4 |
| 统计把未回填样本冒充准确率 | 准确率与一致率的分母只含**已回填**轮次；弃权不进 tester 分母；`pending` 单列 | §6 |
| tester 判定是编的、无法核对 | 影子判定必须由判定器**真实跑出来**，并留下逐项检查的报告；日志行的 `evidence` 指向该报告 | §4.1、§7 |
| 日志行与影子报告对不上 | `tools/check-review-packet.py` 逐行核对：每个 `verdict` 行的 `evidence` 报告必须存在，且 PR 号、head、判定三者与该行一致 | §4.2、§7 |

## 9. 后续（不属于本卡）

1. tester 车道上线后，由它自己写 `verdict` 事件（或写一份影子报告后由 lane 侧入账），本卡只提供格式与工具。届时本地的确定性判定器可以作为 tester 车道的机器检查底座，也可以继续作为影子期的一个固定观测点。
2. 合并门切换的判定门限（最小样本量、一致率/准确率差、观察窗口）由 Epic #1785 的主卡定义；本卡的 `stats` 输出即为该门限的输入。
3. 回填动作若要常态化，可在 PR 状态变为 `merged`/`closed` 时由 dispatcher 触发一次 `backfill`，避免依赖人工记忆。
4. 判定器目前只覆盖 Python 侧（`openswarm/`、`tools/`）；`openbot/` 等前端改动对它是「无适用检查」，会落到 INSUFFICIENT。

## 10. 测试

```sh
python3 -m unittest discover -s tools/tests -p 'test_review_divergence_log.py' -v
python3 -m unittest discover -s tools/tests -p 'test_review_shadow_tester.py' -v
python3 -m unittest discover -s tools/tests -p 'test_check_review_packet.py' -v
python3 tools/review-divergence-log.py --self-test
python3 tools/review-shadow-tester.py --self-test
python3 tools/check-review-packet.py --self-test
```

日志工具覆盖：追加（多轮不覆盖、自动轮次、同一 head 需显式 `--round` 且报错提示下一个可用轮次）、回填（正确归因、未知轮次拒绝、多轮待回填需显式 `--round`、全部回填完毕后的提示、重复回填需 `--force` 且后者生效）、未回填清单、统计（只算已回填轮次、弃权不进分母、一致率与分歧矩阵、归因规则表全覆盖、归因分布计数、篡改的 `correct` 被点名、有结局无判定的轮次单列且不进分母）、坏输入拒绝（PR 号、head、判定值、轮次、无时区时间戳，且拒绝时不写文件）、坏行报错不跳过、环境变量路径解析、模块 docstring 里的事件样例可被校验器接受（文档与代码不漂移）。

影子判定器覆盖（每个用例都在临时 git 仓库里造一个真实 head）：干净 head 判 PASS、diff 混入二进制产物判 FAIL、变更文件语法错误判 FAIL、相关单测变红判 FAIL、没有测试覆盖的源文件判 INSUFFICIENT（且不计入失败）、head 不在对象库判 INSUFFICIENT、没有适用检查的 diff 判 INSUFFICIENT、非法 head 退出码 2。

复核入口覆盖（`tools/check-review-packet.py`）：完整 patch 能解析出文件块与 hunk、被截断的 patch 报错而不是当成更小的 diff、hunk 行数超过头部声明时报错、manifest 指向缺失文件/错误 sha256/错误字节数时报错、判定行缺少影子报告时报错、影子报告与判定行判定不一致时报错、一致时不报错、本仓库自带的复核包全部通过。
