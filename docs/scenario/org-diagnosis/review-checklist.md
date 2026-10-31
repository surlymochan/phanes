# 组织诊断 e2e：独立审查清单（Reviewer）

Refs #92（#6「三 Bot 场景」中的独立审查环节）。

本文是 Reviewer 角色的操作清单：三 Bot（Boss / DA / Builder）产出后，由**独立审查者**按本文逐项复核，
并在 `<scenario-workdir>/verification/review.md` 落盘审查记录。配套契约见
[`README.md`](README.md)（角色职责、交接工件、验收清单）与 [`pipeline.md`](pipeline.md)（三阶段编排与审查门禁）。

审查对象是**工件与可复现命令**，不是执行者的口头或运行摘要式自述（`README.md:27`）。

> 引用约定：本文中的 `README.md` 指同目录的 [`docs/scenario/org-diagnosis/README.md`](README.md)；示例站点的说明文件一律写全路径 `openbot/examples/org-health-local/README.md`。行号以当前 `main` 为准。

## 0. 审查者身份与独立性

| 检查项 | 看哪里 | 通过判据 |
| --- | --- | --- |
| 审查者不是产出者 | 本次审查的署名与三个阶段的产出者 | 审查者既不是 DA（快照与口径说明的作者），也不是 Builder（站点改动与验证报告的作者）；跨执行器族审查时按 `pipeline.md:45`、`:154` 的约束选人 |
| 审查只读 | 审查者的操作记录 | 审查只读取工件、运行只读检查命令，不修改快照、任务书、站点源码；发现问题时写退回意见，不代改 |
| 审查证据可复现 | `<scenario-workdir>/verification/review.md` | 每条结论都带文件路径或可复现命令，第三方按同一命令能得到同一结果 |
| 越界数据处理方式 | 本次审查记录 | 出现个人级、越权来源、口径不符的数据时判「不通过」并退回，不接受「页面隐藏」式处理（`README.md:122`） |

## 1. 输入工件与路径

| 工件 | 生产者 | 路径 | 是否含真实数据 |
| --- | --- | --- | --- |
| Boss 任务书 | Boss | 流水线编排下为 `docs/scenario/org-diagnosis/out/boss-brief.json`（仓库内提交，`pipeline.md:56`）；按交接契约的等价物为 `<scenario-workdir>/plan.md`（仓库外，`README.md:18`、`:46`） | 否 |
| DA 诊断快照 | DA | `<scenario-workdir>/evidence/diagnosis.json`（即 `${DIAGNOSIS}`，`pipeline.md:23`、`:75`） | 是（仓库外） |
| DA 口径说明 | DA | `<scenario-workdir>/evidence/`（说明文件）+ 仓库内 `docs/scenario/org-diagnosis/out/da-method.md` | 否（说明文件不含真实数字） |
| Builder 站点产物 | Builder | Builder 验证报告记录的站点目录（`check_site.py` 的入参 `dist`） | 否 |
| Builder 验证报告 | Builder | `<scenario-workdir>/verification/`（`README.md:66`） | 否（含本机路径细节） |
| 合成示例 | 仓库维护者 | `openbot/examples/org-health-local/data/diagnosis.json` | 否（人工构造） |

**审查前先确认工件位置**：把上表中的实际路径抄进 `review.md` 抬头；路径缺失即判该项不通过，不靠推测补齐。

## 2. 审查环境准备

在仓库根目录执行（`<scenario-workdir>` 与 `${DIAGNOSIS}` 换成实际路径）：

```bash
# 1) 快照结构：schema v4 结构定义校验（validate.py 接受位置参数的文件路径，validate.py:289-299）
python3 openbot/examples/org-health-local/validate.py "<scenario-workdir>/evidence/diagnosis.json"

# 2) 站点产物：文件齐备、无外链、build-report sha256 一致
python3 openbot/examples/org-health-local/builder/check_site.py "<站点产物目录>"

# 3) 本机服务边界（仅标准库，不调用 Bun）
cd openbot/examples/org-health-local && python3 -m unittest test_serve -v

# 4) 合成示例回归（需要 bun 与 Chrome；只覆盖合成示例数据）
bun run test:org-health-demo

# 5) 三方一致性：任务书 / 快照 / 站点产物
#    注意：review/cross_check.py 尚未合入，见第 5 节
python3 openbot/examples/org-health-local/review/cross_check.py \
  --brief docs/scenario/org-diagnosis/out/boss-brief.json \
  --data "<scenario-workdir>/evidence/diagnosis.json" \
  --dist "<站点产物目录>"
```

**每个命令的退出码与输出都要抄进 `review.md`**，作为「证据路径」一节的依据；命令缺失或不可执行时，
记录原因，并改用人工对照第 3 节逐条判据复核，同时把该缺口写进「未决问题」。

## 3. 审查项

编号规则：`A` Boss 任务书、`B` DA 快照、`C` Builder 站点、`D` 数据与隐私边界。
每项给出「看哪里（文件/字段）」与「怎样算通过」。

### 3.1 A 组：Boss 任务书（目标、观察期间、口径、必答问题、数据需求、禁止事项）

| 编号 | 看哪里（文件/字段） | 怎样算通过 |
| --- | --- | --- |
| A1 | 任务书 `目标` | 存在且为数组，1-5 条（`openswarm/examples/org-diagnosis-pipeline.json:92`）；每条是本次诊断要回答的问题，能在快照的 `insights` / `recommendations` 里找到对应回答或对应缺口；不含具体真实数字 |
| A2 | 任务书 `观察期间` | 与快照 `scope.观察月份`、`scope.数据截止` 对齐（`openswarm/examples/org-diagnosis-pipeline.json:92`）：`scope.观察月份` 落在 `观察期间` 的期次集合内；若 `观察期间` 显式给出 `截止期次` / `数据截止`，两者必须分别等于 `scope.观察月份` / `scope.数据截止` |
| A3 | 任务书 `口径` | 每个指标一个口径定义，与快照 `metrics` 的标题一一对应（`openswarm/examples/org-diagnosis-pipeline.json:92`）；成本单位（万元/月，不含运维与设施）、折算人头（当量折算，非自然人数）、收入单位（万美元/月）、成本率定义与页面口径一致（`openbot/packages/i18n/src/messages/zh/orgHealthDemo.ts:30`、`:34`）；明确哪些期次允许为 `null` |
| A4 | 任务书 `必答问题` | 存在且为数组（`openswarm/examples/org-diagnosis-pipeline.json:92`）；每条是 DA 必须回答的问题；每条都能在快照的 `insights` 或 `recommendations` 中被引用（按问题 id、原文或关键词命中，`review/cross_check.py` 的判定规则）；未被引用的每条都要么补答、要么在 `risks` / `recommendations` 的「缺失证据」里说明为何答不了 |
| A5 | 任务书 `数据需求` | 存在且为数组，列出要读的字段路径与到 `metrics` 的口径映射（`openswarm/examples/org-diagnosis-pipeline.json:92`）；DA 口径说明里的每个来源都能对应到 `数据需求` 的一项，DA 没有使用任务书之外的来源 |
| A6 | 任务书 `禁止事项` | 至少覆盖：禁止编造数字、禁止把真实数据写进仓库、无法证实的结论必须落到 `risks` / `recommendations` 的「缺失证据」（`openswarm/examples/org-diagnosis-pipeline.json:92`）；`schema版本` 存在且为 4 |
| A7 | 任务书正文与全部字段值（通读） | 任务书只写口径、问题与字段映射，不含真实组织数字；含真实数字即判不通过（`pipeline.md:56-57`、`README.md:5`） |

### 3.2 B 组：DA 快照（schema v4、口径说明、null 含义、合成数据不得冒充真实数据）

| 编号 | 看哪里（文件/字段） | 怎样算通过 |
| --- | --- | --- |
| B1 | 快照 `scope.schema版本` | 等于 4（`openbot/examples/org-health-local/schema/diagnosis.schema.json` 中 `scope.schema版本` 为 `const: 4`；`app.js:74` 非 4 即判结构错误） |
| B2 | 快照 `scope` 四项 | `数据截止` 形如 `YYYY-MM-DD`、`观察月份` 形如 `YYYY-MM`、`来源` 非空、`schema版本` = 4（`openbot/examples/org-health-local/data/diagnosis.json:2-7`；`openbot/examples/org-health-local/schema/diagnosis.schema.json` 的 `scope` 定义） |
| B3 | 快照 `metrics` 标题与顺序 | 依次为「整体技术人力成本」「整体折算人头」「营收业务（含共享平台）技术人力成本与折算人头」「收入与成本率」，顺序不可变（`app.js:72`；`openbot/examples/org-health-local/data/diagnosis.json:18`、`:27`、`:36`、`:53`） |
| B4 | 快照各期次数值 | 数值型序列的每个值为有限数值或 `null`；比率序列可为数值、`null` 或字符串（`openbot/examples/org-health-local/schema/diagnosis.schema.json` 的 `series` / `ratioSeries`；`app.js:111-128` 非法即判失败） |
| B5 | 快照 `null` 的用法 | `null` 只表示该期次未统计、未提供或无同口径对比数据，不用于「还没取数」；不以 0 或其他推测值替代（`openbot/examples/org-health-local/data/diagnosis.json:12`；`app.js:129` 渲染为「未提供」）。整体成本与整体人头的参考期次（`2026-05H1参考`）必须为 `null`（`app.js:121-122`；`openbot/examples/org-health-local/schema/diagnosis.schema.json` 的 `actualSeries`） |
| B6 | 快照 `口径说明`（可选字段） | 若存在，每条说明期间、单位、年末测算与 H1 参考、`null` 含义，且与任务书 `口径` 不冲突（`openbot/examples/org-health-local/data/diagnosis.json:8-13`；`openbot/examples/org-health-local/schema/diagnosis.schema.json` 的 `口径说明` 定义）。若不存在，记录为「无口径说明面板」，并在 A3 的口径对照中确认缺口已被 `da-method.md` 覆盖 |
| B7 | 快照 `insights` / `definitions` / `risks` / `recommendations` | 四个键存在且为数组（`app.js:78-80`）；`insights` 每条 `类型` 明确区分事实 / 推断 / 边界，推断不写成事实（`openbot/packages/i18n/src/messages/zh/orgHealthDemo.ts:35-36`）；缺失领域显式写为证据缺口，不以成本或人数代替 |
| B8 | 快照 `recommendations` 每条 | 五个字段齐全：`优先级`、`标题`、`依据`、`缺失证据`、`验证方式`（`openbot/examples/org-health-local/schema/diagnosis.schema.json` 的 `recommendation` 定义；`openbot/examples/org-health-local/data/diagnosis.json:95-103`） |
| B9 | 快照 `conclusion` / `origin` | 结论句能说明数据边界；`origin` 指向真实来源标识；**不得出现合成示例的字样冒充真实数据**：真实运行的快照 `scope.来源` 不得是「合成示例」、`origin` 不得是 `synthetic fixture`、`conclusion` 不得是合成示例的免责句（`openbot/examples/org-health-local/data/diagnosis.json:5`、`:14`、`:15`） |
| B10 | 快照 `scope.观察月份` 与页面期望月份 | 页面固定按 `2026-09` 校验观察月份（`app.js:75`）；若真实运行的观察月份不是 `2026-09`，必须记为**阻断项**：站点会直接抛结构错误并判加载失败，须由 Builder 改页面期望值后再审 |
| B11 | 快照的来源可回溯性 | 对 `da-method.md`（或 `<scenario-workdir>/evidence/` 内的口径说明）逐条核对：每个出现在快照里的数字都能指到来源文件路径与计算口径；指不到的数值视为不可回溯，不接受，须退回 |
| B12 | 合成数据不得冒充真实数据（仓库侧） | 仓库内 `openbot/examples/org-health-local/data/diagnosis.json` 仍是人工构造的合成示例，未被替换成真实数据（`README.md:73`）；`git diff --name-only <stage-base>..<stage-commit>` 的输出里除该合成示例外不出现任何 `diagnosis*.json`（`pipeline.md:173-181`；`openswarm/examples/org-diagnosis-pipeline.json` 的 `data_policy`） |

### 3.3 C 组：Builder 站点（只读本机数据、无外链、build-report sha256）

| 编号 | 看哪里（文件/字段） | 怎样算通过 |
| --- | --- | --- |
| C1 | Builder 的启动命令与验证报告 | 站点通过 `serve.py --data /absolute/path/to/diagnosis.json` 启动，真实数据不入站点目录（`serve.py:43-48`、`:60-67`；`openbot/examples/org-health-local/README.md:5`） |
| C2 | 服务实际行为 | 只监听回环地址 `127.0.0.1`（`serve.py:50-53`、`:66`）；非 `127.0.0.1:<port>` / `localhost:<port>` 的 Host 头返回 403、白名单外路径返回 404（`serve.py:20-28`）；目录列表返回 404（`serve.py:35-37`）；请求日志不落盘（`serve.py:39-41`）。逐条对照 `python3 -m unittest test_serve -v` 的结果 |
| C3 | 页面取数路径 | 页面只请求同源 `./data/diagnosis.json`（`app.js:297`），服务端把该路径映射到 `--data` 指定的真实文件（`serve.py:30-33`）；页面不向外部发起请求 |
| C4 | 页面外链与脚本 | `index.html` 只有本地 `styles.css` 与 `app.bundle.js`（`index.html:9-10`），无第三方脚本、无外链；内容安全策略为 `default-src 'self'`（`index.html:7`）；`builder/check_site.py` 对 `index.html`、`styles.css` 的外部 `http(s)` 引用检查通过（`openbot/examples/org-health-local/builder/check_site.py:21`、`:84`） |
| C5 | `build-report.json` | 站点产物目录含 `build-report.json`，且 `index.html`、`app.bundle.js`、`styles.css`、`data/diagnosis.json` 四个文件都有 64 位十六进制 sha256（`openbot/examples/org-health-local/builder/check_site.py:13-20`） |
| C6 | sha256 与实际文件一致 | `build-report.json` 中每个文件的 sha256 与磁盘上同名文件重算结果相同（`openbot/examples/org-health-local/builder/check_site.py:94-99`；摘要可写 `files` 对象或顶层 `sha256` 对象，两种表示都接受，见 `openbot/examples/org-health-local/builder/check_site.py:37`） |
| C7 | 站点数据与快照一致 | 站点产物里的 `data/diagnosis.json` 与 DA 快照字节相同（`review/cross_check.py --dist` 的「站点数据一致」检查，见第 5 节）；若该脚本尚未合入，人工比对 sha256 并记录比对结果 |
| C8 | Builder 说明与验证报告 | `builder-notes.md` 记录改动点、验证命令与已知限制；验证报告记录访问 URL、启动命令、端口、逐项检查结果与失败项，可被第三方复现（`README.md:107`） |
| C9 | 改动范围 | Builder 的分支 diff 限于 `openbot/examples/org-health-local/` 与说明文件；未引入新的运行时依赖（当前只用 Python 标准库加 bun 构建前端，`pipeline.md:100`） |

### 3.4 D 组：数据与隐私边界

| 编号 | 看哪里（文件/字段） | 怎样算通过 |
| --- | --- | --- |
| D1 | 快照全部键名（含嵌套） | 不含个人 / 薪酬 / 编制粒度字段名（姓名、薪酬、编制、工号等，见 `review/cross_check.py` 的 `FORBIDDEN_FIELD_TOKENS`，见第 5 节）；只含聚合口径 |
| D2 | 页面文案 | 只描述聚合信号，不作个人、薪酬或编制判断（`openbot/packages/i18n/src/messages/zh/orgHealthDemo.ts:21`）；历史快照提示语存在，不把历史快照当作当前组织结论（`:15`） |
| D3 | 仓库 / Issue / PR 内容 | 仓库内只有合成示例与不含真实数字的说明文件；Issue、PR、聊天记录、工单里只有路径 + sha256，没有真实数据正文（`README.md:76`） |
| D4 | 真实数据落点 | 真实数据只在 `<scenario-workdir>` 内；快照路径为仓库外绝对路径（`pipeline.md:164-172`）；审查者不得把快照正文抄进仓库内任何文件或提交信息 |

## 4. 审查结论模板

把下面内容抄进 `<scenario-workdir>/verification/review.md`（路径见 `README.md:67`）：

```markdown
# 组织诊断独立审查记录

- 审查者（执行器 / 模型）：<name>
- 独立性声明：非 DA、非 Builder 产出者
- 审查时间：<YYYY-MM-DD>
- 审查工件：
  - 任务书：<path>（sha256 <...>）
  - 诊断快照：<path>（sha256 <...>）
  - DA 口径说明：<path>
  - 站点产物：<path>
  - Builder 验证报告：<path>

## 复现命令与结果

| 命令 | 退出码 | 摘要 |
| --- | --- | --- |
| `python3 openbot/examples/org-health-local/validate.py "<snapshot>"` | <0/1/2> | <...> |
| `python3 openbot/examples/org-health-local/builder/check_site.py "<dist>"` | <0/1/2> | <...> |
| `python3 openbot/examples/org-health-local/review/cross_check.py --brief ... --data ... --dist ...` | <0/1/2> | <...> |
| `cd openbot/examples/org-health-local && python3 -m unittest test_serve -v` | <0/1> | <...> |
| `bun run test:org-health-demo` | <0/1> | <...> |

## 逐项结论

| 编号 | 检查项 | 结论（通过/不通过） | 证据（文件路径、字段或命令输出） |
| --- | --- | --- | --- |
| A1 | 目标 |  |  |
| A2 | 观察期间 |  |  |
| ... | ... |  |  |
| D4 | 真实数据落点 |  |  |

## 审查结论

- 结论：通过 / 有条件通过 / 不通过
- 阻断项（如有）：<编号 + 一句话说明>
- 条件项（如有）：<编号 + 修复要求 + 负责角色>
- 未决问题：<...>

## 证据路径

- <路径>（sha256 <...>）
```

### 4.1 三种结论的判据

| 结论 | 判据 | 后续动作 |
| --- | --- | --- |
| 通过 | A、B、C、D 组全部条目通过，复现命令退出码为 0，且无未解释的差异 | 交 Boss 按 `README.md:84` 的验收清单放行 |
| 有条件通过 | 无数据越界、无口径造假、无阻断项；仅剩可在本轮返工修复的次要项（例如口径说明缺一条、非关键字段顺序之外的展示瑕疵），且每条都写明修复要求与负责角色 | 结论中必须列出条件项清单；Boss 放行前逐条销项 |
| 不通过 | 出现下列任一情况：口径与任务书不符；数值不可回溯到来源；`null` 被当成缺数或被估算值替代；合成数据冒充真实数据；出现个人级 / 越权来源数据；站点外链或 sha256 不一致；关键命令无法复现且无人工对照结论 | 退回对应角色返工（DA 用 `openswarm run --rework-of <run-id>`，`pipeline.md:135`、`:153`）；返工后重跑本文全部条目 |

> 术语边界：**Reviewer 的「有条件通过」不等于放行。** Boss 的最终验收仍按 `README.md:84` 执行——验收清单任一项未通过即退回，不做部分接受；本文的三档结论只是审查记录的表达形式。

## 5. 引用脚本与落地状态

| 脚本 | 路径 | 状态 | 对应 issue | 本文引用处 |
| --- | --- | --- | --- | --- |
| `validate.py` | `openbot/examples/org-health-local/validate.py` | 已合入 | #52（PR #61） | B1-B4、B8 |
| `test_serve.py` | `openbot/examples/org-health-local/test_serve.py` | 已合入 | #53（PR #62） | C2 |
| `builder/check_site.py` | `openbot/examples/org-health-local/builder/check_site.py` | 已合入 | #76（PR #79） | C4-C6 |
| `review/cross_check.py` | `openbot/examples/org-health-local/review/cross_check.py` | **尚未合入** | **#80** | A2、A4、B10、D1、C7 |
| `da/build_diagnosis.py` | `openbot/examples/org-health-local/da/build_diagnosis.py` | **尚未合入** | **#67** | B11（DA 侧口径说明的对照物） |
| `bun run test:org-health-demo` | `openbot/package.json:194` | 已合入 | — | C 组回归 |

**未合入脚本的使用约定**：

- `review/cross_check.py`（issue #80）提供 `--brief / --data / --dist` 三个参数，逐条输出 `PASS`/`FAIL`，
  任一 FAIL 时退出码为 1，输入不可读或不是 JSON 对象时退出码为 2。**在它合入之前**，第 2 节的第 5 条命令不可执行，
  审查者改为人工按 A2、A4、B10、D1、C7 逐条对照，并把「脚本缺失」写进 `review.md` 的未决问题。
- `da/build_diagnosis.py`（issue #67）是 DA 侧从原始 CSV 生成快照的工具，不在仓库内时不影响审查动作；
  DA 的快照改由手工归并产出时，B11 的回溯核对按 `da-method.md` 逐条人工执行。

## 6. 未决问题（模板）

- `review/cross_check.py` 未合入前，三方一致性（期间、版本、必答问题覆盖、站点数据一致、隐私边界）只能人工核对：
  <填写本次实际核对方式与结果>。
- 快照观察月份与页面期望值不一致时（B10），由谁改 `app.js` 的期望月份：
  <填写>。
- Builder 站点产物的目录约定（`<scenario-workdir>/site/` 下是否固定放 `dist/`）尚未在 `README.md:54` 定死，
  本次以验证报告记录的实际路径为准：
  <填写>。
