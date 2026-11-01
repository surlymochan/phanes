# 组织诊断场景：角色接口契约（Boss / DA / Builder / Reviewer / 编排）

Refs #6、#142（本文由 #142 新增）。

本文按**当前 main 上的代码**（不是按计划）写出组织诊断场景五个角色的接口契约：每个脚本的命令行
（参数、默认值）、输入文件、输出文件、退出码语义，以及 `brief.json`、`diagnosis.json`、`dist/`
的关键字段与「谁写谁读」。行号引用均为写作时 main 上的实际行号；命令与退出码是本地实测结果
（复现命令见第 9 节）。

与已有文档的关系：

- `docs/scenario/org-diagnosis/README.md` 规定 Boss/DA/Builder 的**职责与验收清单**；
- `docs/scenario/org-diagnosis/pipeline.md` 与 `openswarm/examples/org-diagnosis-pipeline.json`
  规定**编排方式**；
- `docs/scenario/org-diagnosis/review-checklist.md` 规定**人工审查清单**；
- `docs/scenario/org-diagnosis/data-definitions.md` 规定**diagnosis.json 的指标键名口径**
  （含义、单位、计算口径、来源角色）；
- 本文只补齐**接口级契约**：命令行、输入输出、退出码、字段归属，以及已知契约缺口。

边界沿用 `docs/scenario/org-diagnosis/README.md:5`：真实组织数据只经仓库外的绝对路径进入，
仓库内只保留合成示例 `openbot/examples/org-health-local/data/diagnosis.json`。

## 1. 角色与工件总览

| 角色 | 脚本 | 输入 | 输出 | 谁读 |
| --- | --- | --- | --- | --- |
| Boss | `openbot/examples/org-health-local/boss/make_brief.py` | 无（内置默认口径与问题） | `brief.json`（任务书） | DA、Reviewer |
| DA | `openbot/examples/org-health-local/da/build_diagnosis.py` | `brief.json` + `--raw` 目录的四张 CSV | `diagnosis.json`（schema v4 快照） | Builder、Reviewer |
| DA（口径说明） | `openbot/examples/org-health-local/da/write_method.py` | `brief.json` + `diagnosis.json` | `da-method.md`（不含数值的口径说明） | Boss、Reviewer |
| Builder | `openbot/examples/org-health-local/builder/build_site.py` | `diagnosis.json` | `dist/`（静态站点 + `build-report.json`） | Reviewer、预览服务 |
| Builder（自检） | `openbot/examples/org-health-local/builder/check_site.py` | `dist/` | 逐条 PASS/FAIL | Builder、Boss |
| Reviewer | `openbot/examples/org-health-local/review/cross_check.py` | `brief.json` + `diagnosis.json`（+ `dist/`） | 逐条 PASS/FAIL（只读，不写文件） | Boss、编排 |
| 编排 | `openbot/examples/org-health-local/run_e2e.py`、`openswarm run/review` | 上面各角色的产物 | 分支提交、`result.json`、`review.json` | 控制面（人） |

同一条流水线的两种入口：

- **仓库内一键跑通（合成数据）**：`python3 openbot/examples/org-health-local/run_e2e.py --workdir <目录>`
  （`run_e2e.py:36-57`），依次跑 Boss → DA → Builder → 自检。
- **OpenSwarm 三阶段编排（真实数据）**：`openswarm run` 每阶段一个独立 worktree，
  下一阶段 `--base` 取上一阶段提交，阶段后 `openswarm review` 做跨族审查
  （`openswarm/examples/org-diagnosis-pipeline.json`）。

## 2. Boss 契约：`boss/make_brief.py`

### 2.1 命令行

```
python3 boss/make_brief.py --out <brief.json> [--goal 文本 ...] [--month YYYY-MM]
```

| 参数 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- |
| `--out` | 是 | 无 | 任务书输出路径；父目录必须已存在（不自动创建），写出时先写同目录临时文件再替换（`boss/make_brief.py:364-397`） |
| `--goal` | 否 | `DEFAULT_GOALS` 四条（`boss/make_brief.py:47-53`） | 覆盖「目标」；可重复给出，按出现顺序写入 |
| `--month` | 否 | `2026-09`（`boss/make_brief.py:36`） | 覆盖观察月份，格式 `YYYY-MM`；数据截止与期次锚点随之推算（`boss/make_brief.py:113-143`） |

### 2.2 输入 / 输出

- **输入**：无文件输入；口径、问题、字段映射与禁止事项全部内置（`boss/make_brief.py:40-110`）。
- **输出**：`brief.json`，`ensure_ascii=False`、`indent=2`、键序固定（`boss/make_brief.py:346-349`）。
- **自检**：写出前按 `boss/brief.schema.json` 校验，不通过则不写文件（`boss/make_brief.py:206-208`、`:246-251`）。

### 2.3 退出码

| 退出码 | 含义 | 触发条件（引用） |
| --- | --- | --- |
| `0` | 成功 | 已写出任务书（`boss/make_brief.py:257-258`） |
| `1` | 任务书不符合 `brief.schema.json` | `validate_brief` 返回错误（`boss/make_brief.py:31-32`、`:246-251`） |
| `2` | 用法错误 | `--month` 不是 `YYYY-MM`（`boss/make_brief.py:460-464`）或写文件失败（`:471-476`） |

### 2.4 `brief.json` 关键字段

结构定义见 `boss/brief.schema.json`（顶层 `additionalProperties: false`，必需键见
`boss/brief.schema.json:9`）：

| 字段 | 类型 | 必填 | 谁写 | 谁读 |
| --- | --- | --- | --- | --- |
| `目标` | 字符串数组（1-5 条） | 是 | Boss | DA 写进快照 `口径说明`（`da/build_diagnosis.py:868`） |
| `观察期间` | 对象：`观察月份`/`数据截止`/`期次`/`说明` | 是 | Boss | Reviewer 核期间（`review/cross_check.py:179-198`）；DA 解析 Boss 格式 `期次` 锚点（`da/build_diagnosis.py:141-186`） |
| `口径` | 对象：四个指标标题 → `{单位,定义,期间,缺失处理}` | 是 | Boss | DA 读取显式汇率，缺失时从原始目录 `fx.csv` 读取（`da/build_diagnosis.py:189-207`）；Reviewer 不校验口径内容 |
| `必答问题` | 字符串数组 | 是 | Boss | DA 逐条生成洞察（`da/build_diagnosis.py:649-652`）；Reviewer 逐条核对是否被引用（`review/cross_check.py:365-378`） |
| `数据需求` | 对象数组：`字段路径`/`单位`/`口径`/`对应指标` | 是 | Boss | DA 按实际来源映射原始列名（脚本不读此字段，只作为交付要求） |
| `禁止事项` | 字符串数组（≥2 条） | 是 | Boss | DA 写进快照 `definitions` 末尾（`da/build_diagnosis.py:710-711`） |
| `schema版本` | 常量 `4` | 是 | Boss | DA 校验版本（`da/build_diagnosis.py:221-223`）；Reviewer 核版本（`review/cross_check.py:226-243`） |

## 3. DA 契约：`da/build_diagnosis.py`

### 3.1 命令行

```
python3 da/build_diagnosis.py --brief <brief.json> --raw <dir> --out <diagnosis.json>
```

| 参数 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- |
| `--brief` | 是 | 无 | 任务书 JSON（`da/build_diagnosis.py:953`） |
| `--raw` | 是 | 无 | 原始数据目录，必须含四张 CSV（`da/build_diagnosis.py:954`） |
| `--out` | 是 | 无 | 诊断快照输出路径；父目录不存在时自动创建（`da/build_diagnosis.py:942-948`） |

### 3.2 输入

- **任务书**：`目标`、`观察期间`、`口径`、`必答问题`、`数据需求`、`禁止事项`、`schema版本`。
- **原始数据目录**（表头固定，`da/build_diagnosis.py:54-64`）：

  | 文件 | 必需列 |
  | --- | --- |
  | `cost.csv` | `月份`、`整体技术人力成本万元`、`营收业务技术人力成本万元`、`运维设施成本万元` |
  | `heads.csv` | `月份`、`整体折算人头`、`营收业务折算人头` |
  | `revenue.csv` | `月份`、`收入万美元` |
  | `forecast.csv` | `期次`、`整体技术人力成本万元`、`营收业务技术人力成本万元`、`整体折算人头`、`营收业务折算人头`、`运维设施成本万元`、`收入万美元` |

  合成样本见 `openbot/examples/org-health-local/da/sample-raw/`。

- **期次与汇率**：DA 可直接消费 Boss 格式的 `观察期间.期次`，按首个实际、首个参考及
  `观察月份`对应的实际期次解析为锚点（`da/build_diagnosis.py:141-186`）；若任务书未提供
  `口径.汇率`，则从原始目录的 `fx.csv`（列 `人民币每美元`）读取（`:189-207`）。

### 3.3 输出

`diagnosis.json`，schema v4，键序固定（`da/build_diagnosis.py:835-898`、`:942-948`）。
写盘前用 `validate.py` 自检，不通过不写文件（`da/build_diagnosis.py:921-940`）。

| 字段 | 含义 | 谁读 |
| --- | --- | --- |
| `scope` | `数据截止`/`观察月份`/`来源`/`schema版本`（`da/build_diagnosis.py:884-889`） | Reviewer 核期间与版本（`review/cross_check.py:200-243`）；Builder 校验（`builder/build_site.py:158-168`） |
| `口径说明` | 可选面板数组：期间、单位、汇率、null 含义（`da/build_diagnosis.py:866-881`） | 页面（`app.js:257`） |
| `conclusion` | 结论摘要文本（`da/build_diagnosis.py:821-832`） | 页面、Reviewer（仅字符串检索） |
| `origin` | 快照来源说明，含数据性质（合成示例 / 未标注）（`da/build_diagnosis.py:207-218`、`:891`） | 人、审查 |
| `metrics` | 四个指标，标题与顺序固定（`da/build_diagnosis.py:85-90`、`:840-864`） | 页面按位置与标题校验（`app.js:71-82`）、Builder、Reviewer |
| `insights` | 洞察数组，`类型` ∈ {事实, 推断, 边界}（`da/build_diagnosis.py:505-652`） | 页面、Reviewer 引用检索 |
| `definitions` | 口径说明字符串数组（`da/build_diagnosis.py:694-712`） | 页面、Reviewer 引用检索 |
| `risks` | 证据缺口数组，`标题`/`内容`（`da/build_diagnosis.py:715-769`） | 页面、Reviewer 引用检索 |
| `recommendations` | 建议数组，`优先级`/`标题`/`依据`/`缺失证据`/`验证方式`（`da/build_diagnosis.py:771-818`） | 页面、Reviewer 引用检索 |

结构定义：`openbot/examples/org-health-local/schema/diagnosis.schema.json`
（顶层必需键 `:9`；`scope` `:63-86`；期次键名写死在 `actualSeries` `:184-196`、
`series` `:197-204`、`revenueSeries` `:205-212`、`ratioSeries` `:213-220`、
`fullRateSeries` `:221-228`；`insight` `:229-247`、`risk` `:248-262`、
`recommendation` `:263-289`）。

### 3.4 退出码

| 退出码 | 含义 | 触发条件（引用） |
| --- | --- | --- |
| `0` | 成功 | 「已生成诊断快照」（`da/build_diagnosis.py:963-964`） |
| `1` | 任务书、原始数据或生成的快照不合法 | `BriefError`/`RawDataError`/`SnapshotError`（`da/build_diagnosis.py:40-41`、`:958-961`） |

细分错误信息（都走退出码 1）：

- 任务书读不到 / 不是 JSON 对象：`da/build_diagnosis.py:109-121`；
- `观察期间` 缺期次锚点或格式错：`da/build_diagnosis.py:159`、`:161-165`；
- `口径.汇率` 缺失或非正数：`da/build_diagnosis.py:189`；
- CSV 缺文件 / 缺列 / 重复期次 / 非数值：`da/build_diagnosis.py:246-263`、`:281-293`、`:295-308`；
- 原始数据含整体口径参考期次值（schema v4 要求固定 `null`）：`da/build_diagnosis.py:398-402`；
- 生成的快照未过结构校验：`da/build_diagnosis.py:921-928`、`:938`。

### 3.5 DA 口径说明：`da/write_method.py`

```
python3 da/write_method.py --brief <brief.json> --data <diagnosis.json> --out <da-method.md>
```

| 参数 | 必填 | 说明（引用） |
| --- | --- | --- |
| `--brief` | 是 | `da/write_method.py:155` |
| `--data` | 是 | `da/write_method.py:156` |
| `--out` | 是 | `da/write_method.py:157` |

| 退出码 | 含义 | 触发条件（引用） |
| --- | --- | --- |
| `0` | 成功 | 文档已写出（`da/write_method.py:184`） |
| `1` | 自检判定「文档包含诊断数值」，不写文件 | `da/write_method.py:172-181`（实测对 Boss 产出的任务书误报，见第 8 节缺口 3） |

## 4. Builder 契约：`builder/build_site.py` + `builder/check_site.py` + `serve.py`

### 4.1 构建：`builder/build_site.py`

```
python3 builder/build_site.py --data <diagnosis.json> --out <dist> [--bundle <app.bundle.js>]
```

| 参数 | 必填 | 默认 | 说明（引用） |
| --- | --- | --- | --- |
| `--data` | 是 | 无 | 诊断快照路径（`builder/build_site.py:126`） |
| `--out` | 是 | 无 | 输出目录 `dist`（`builder/build_site.py:127`） |
| `--bundle` | 否 | 无 → 调 `bun build app.js --target=browser --outfile=<dist>/app.bundle.js`（`builder/build_site.py:67-74`、`:116-119`） | 已编译好的浏览器包；给了就整包复制 |

产物（`builder/build_site.py:36-40`、`:107-121`）：

```
<dist>/index.html            # 从示例目录复制
<dist>/styles.css            # 从示例目录复制
<dist>/app.bundle.js         # 复制或 bun 打包
<dist>/data/diagnosis.json   # 快照字节原样复制
<dist>/build-report.json     # 构建报告
```

`build-report.json` 字段（`builder/build_site.py:83-105`）：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `输入路径` | 字符串 | 快照绝对路径 |
| `sha256` | 字符串 | **快照**的 sha256（不是文件清单） |
| `文件清单` | 数组 | 每项 `{路径, 字节数, sha256}`，顺序即 `SITE_FILES` |
| `生成时间` | 字符串 | UTC `YYYY-MM-DDTHH:MM:SSZ` |

| 退出码 | 含义 | 触发条件（引用） |
| --- | --- | --- |
| `0` | 成功 | 打印产物清单（`builder/build_site.py:179-183`） |
| `1` | 快照未通过 `validate.py` | 校验失败；**不生成 `dist`**（`builder/build_site.py:158-168`） |
| `2` | 用法错误 | 数据文件或 `--bundle` 不存在 / 结构定义不可用（`builder/build_site.py:144-156`） |
| `3` | 构建失败 | bun 打包失败或写盘失败；本次新建的 `dist` 会被删除（`builder/build_site.py:170-177`） |

### 4.2 自检：`builder/check_site.py`

```
python3 builder/check_site.py <dist>
```

- 位置参数 `dist`（`builder/check_site.py:108-111`），检查项见 `builder/check_site.py:50-106`：
  1. `index.html`、`app.bundle.js`、`styles.css`、`data/diagnosis.json`、`build-report.json`
     五个文件存在且非空（`builder/check_site.py:13-19`、`:53-64`）；
  2. `data/diagnosis.json` 通过 `validate.py`（`:66-79`）；
  3. `index.html`、`styles.css` 不含外部 `http(s)` 引用（`:81-89`）；
  4. `build-report.json` 里每个产物的 sha256 与磁盘一致（`:91-104`）。
- 退出码：`0` 全部通过（`builder/check_site.py:119`）；`1` 有失败项（`builder/check_site.py:117`）。
- **注意**：第 4 项兼容 `build_site.py` 写出的 `文件清单` 数组，也兼容旧的 `files` 或
  `sha256`「文件名 → sha256」对象（`builder/check_site.py:50-72`）。

### 4.3 预览：`serve.py`

```
python3 serve.py [--port 8765] [--root <dist>] [--data <diagnosis.json>]
```

| 参数 | 默认 | 说明（引用） |
| --- | --- | --- |
| `--port` | `8765` | `serve.py:46` |
| `--root` | 无 | 指定后只服务该目录的白名单文件，不再调 bun 打包（`serve.py:47`、`:72-82`） |
| `--data` | 无 → `<root>/data/diagnosis.json`；两者都没有时用示例目录内的合成数据（`serve.py:48-57`） | `serve.py:48` |

- 只绑定回环地址；Host 不在 `127.0.0.1:<port>`/`localhost:<port>` 返回 403，路径不在白名单
  （`/`、`/index.html`、`/app.bundle.js`、`/styles.css`、`/data/diagnosis.json`）返回 404，
  目录列表关闭（`serve.py:12`、`:21-38`）。
- 页面固定请求同源 `./data/diagnosis.json`（`app.js:297`）。
- 退出码：正常运行时长驻；未指定 `--root` 且本机没有 bun 时，bun 打包失败抛出异常退出。

## 5. Reviewer 契约：`review/cross_check.py`

```
python3 review/cross_check.py --brief <brief.json> --data <diagnosis.json> [--dist <dist>]
```

| 参数 | 必填 | 说明（引用） |
| --- | --- | --- |
| `--brief` | 是 | `review/cross_check.py:451` |
| `--data` | 是 | `review/cross_check.py:452` |
| `--dist` | 否 | 给了就核对 `dist/data/diagnosis.json` 与 `--data` 字节是否相同（`review/cross_check.py:453-458`） |

检查项与判定：

| 检查 | 规则 | 引用 |
| --- | --- | --- |
| 期间一致 | `scope.观察月份` 必须落在任务书 `观察期间` 的 `YYYY-MM` 集合内；显式 `截止期次`/`数据截止` 必须与 `scope` 相同；快照 `metrics[*].数值` 的期次键（全口径成本率的键去掉「整体」前缀）必须与任务书期次锚点（Boss 格式的 `期次` 列表，或起止/参考期次）一致，缺哪些键、多哪些键在本项里说明 | `review/cross_check.py:259-300`、`:358-393`、`:227-247` |
| 版本一致 | 任务书 `schema版本` == `scope.schema版本` == 4 | `review/cross_check.py:226-243` |
| 必答问题覆盖 | 每条必答问题按 id / 原文 / 关键词命中 `insights`+`recommendations` 的全部字符串即算覆盖 | `review/cross_check.py:249-363`、`:365-378` |
| 站点数据一致 | `dist/data/diagnosis.json` 与 `--data` 字节相同（打印 sha256 前 12 位） | `review/cross_check.py:380-397` |
| 隐私边界 | 快照所有键名（含嵌套）不含个人/薪酬/编制粒度黑名单词条 | `review/cross_check.py:108-137`、`:399-429` |

| 退出码 | 含义 | 触发条件（引用） |
| --- | --- | --- |
| `0` | 全部 PASS | `review/cross_check.py:475` |
| `1` | 至少一项 FAIL | `review/cross_check.py:475` |
| `2` | 输入不可读或不是 JSON 对象 | `review/cross_check.py:461-465` |

只读脚本：不写任何文件（`review/cross_check.py:1-4`）。

## 6. 编排契约

### 6.1 仓库内编排：`run_e2e.py`

```
python3 openbot/examples/org-health-local/run_e2e.py --workdir <dir> [--raw <dir>] [--bundle <file>]
        [--boss <script>] [--da <script>] [--builder <script>] [--checker <script>]
```

| 参数 | 默认 | 说明（引用） |
| --- | --- | --- |
| `--workdir` | 必填 | 工作目录；内含 `brief.json`、`diagnosis.json`、`dist/`（`run_e2e.py:14`、`:39-42`） |
| `--raw` | `da/sample-raw` | 原始 CSV 目录（`run_e2e.py:15`） |
| `--bundle` | 无 | 传给 `build_site.py --bundle`（`run_e2e.py:16`、`:47-48`） |
| `--boss` / `--da` / `--builder` / `--checker` | `boss/make_brief.py`、`da/build_diagnosis.py`、`builder/build_site.py`、`builder/check_site.py` | 允许替换各角色脚本（`run_e2e.py:17-20`） |

步骤与退出码：Boss → DA → Builder → 自检，任一步非 0 即停并返回 `1`
（`run_e2e.py:24-34`、`:43-55`）；成功时打印预览命令 `python3 serve.py --root <dist>`（`run_e2e.py:56`）。
传入 `--review` 后还会运行 Reviewer 并写出 `review.md`（`run_e2e.py:23-24`、`:79-83`）；默认不运行 Reviewer，
也不产出 `da-method.md`。

### 6.2 OpenSwarm 编排：`openswarm run` / `openswarm review`

示例编排：`openswarm/examples/org-diagnosis-pipeline.json`；说明：
`docs/scenario/org-diagnosis/pipeline.md`。

- `openswarm run` 的关键参数（`openswarm/bin/openswarm:11489-11531`）：
  `--cwd`、`--base`、`--backend`、`--type`（默认 `impl`）、`--difficulty`（默认 `3`）、
  `--budget-minutes`、`--title`、`--task-file`/`--prompt`、`--verify`、`--card-type`、`--rework-of`。
- 每次运行在独立 worktree 中进行，分支名 `openswarm/<run-id>`
  （`openswarm/bin/openswarm:4854`、`:4862`）；阶段产物提交在自己的分支上（`:5055-5059`）；
  结果写 `$OPENSWARM_HOME/runs/<run-id>/result.json`（`:5383-5394`）。
- 退出码：`0` = done，`1` = 未通过，`3` = 预算拒绝（`openswarm/bin/openswarm:5414`）。
- `--verify` 在执行器结束、提交之后于宿主运行，超时上限 600 秒，其退出码覆盖执行器自报结果
  （`openswarm/bin/openswarm:4996-5004`、`:5339`）。
- verify 命令里仓库内路径用相对路径、仓库外路径用绝对路径：openswarm 只改写 base 仓库的绝对路径前缀
  （`openswarm/bin/openswarm:4923-4926`）。
- `openswarm review <run-id> --by <backend> [--json]`（`openswarm/bin/openswarm:11593-11597`）：
  跨族审查，结果写 `$OPENSWARM_HOME/runs/<run-id>/review.json`
  （`openswarm/bin/openswarm:5853-5856`）。**退出码与判定分离**：审查成功即 `0`，
  `verdict=changes` 或 P0/P1 不会体现在退出码里（`openswarm/bin/openswarm:5916-5925`），
  门禁必须解析 `--json` 输出或 `review.json`；仅在出错（审查器不可用、跨族违规、无 diff）时退出 `2`
  （`openswarm/bin/openswarm:11841-11845`）。
- 交接：下一阶段 `--base` 取上一阶段 `result.json` 的 `commit`；上一阶段没有仓库内提交时回退到
  `workspace.base`（`openswarm/examples/org-diagnosis-pipeline.json` 的 `handoff` 段）。

## 7. 关键字段与「谁写谁读」

### 7.1 `brief.json`

| 字段 | 谁写 | 谁读 | 读它做什么 |
| --- | --- | --- | --- |
| `目标` | Boss（`boss/make_brief.py:181-204`） | DA | 写进快照 `口径说明` 首行（`da/build_diagnosis.py:868`） |
| `观察期间` | Boss（`boss/make_brief.py:113-143`） | DA、Reviewer | DA 解析 Boss 格式期次锚点（`da/build_diagnosis.py:141-186`）；Reviewer 核 `scope`（`review/cross_check.py:200-224`） |
| `口径` | Boss（`boss/make_brief.py:145-179`） | DA | 取汇率与口径文字（`da/build_diagnosis.py:175-197`） |
| `必答问题` | Boss（`boss/make_brief.py:54-62`） | DA、Reviewer | DA 逐条生成洞察（`da/build_diagnosis.py:649-652`）；Reviewer 核引用覆盖 |
| `数据需求` | Boss（`boss/make_brief.py:64-101`） | DA（人工映射） | 把真实数据源字段映射到四个指标 |
| `禁止事项` | Boss（`boss/make_brief.py:103-110`） | DA | 写进快照 `definitions`（`da/build_diagnosis.py:710-711`） |
| `schema版本` | Boss（`boss/make_brief.py:35`） | DA、Reviewer | 版本一致性校验 |

### 7.2 `diagnosis.json`

| 字段 | 谁写 | 谁读 | 读它做什么 |
| --- | --- | --- | --- |
| `scope` | DA（`da/build_diagnosis.py:884-889`） | Builder（校验）、Reviewer（期间/版本）、页面 | 结构与期间校验 |
| `口径说明` | DA（`da/build_diagnosis.py:866-881`、`:883`） | 页面（`app.js:257`） | 折叠面板展示口径 |
| `conclusion` | DA（`da/build_diagnosis.py:821-832`、`:890`） | 页面 | 结论摘要 |
| `origin` | DA（`da/build_diagnosis.py:891`） | 人、审查 | 标注数据性质 |
| `metrics` | DA（`da/build_diagnosis.py:840-864`、`:892`） | 页面（标题与顺序校验，`app.js:71-82`）、Builder | 四个指标数值序列 |
| `insights` | DA（`da/build_diagnosis.py:505-652`、`:893`） | 页面、Reviewer（引用检索） | 事实/推断/边界 |
| `definitions` | DA（`da/build_diagnosis.py:694-712`、`:894`） | 页面、Reviewer | 口径与 null 含义 |
| `risks` | DA（`da/build_diagnosis.py:715-769`、`:895`） | 页面、Reviewer | 证据缺口 |
| `recommendations` | DA（`da/build_diagnosis.py:771-818`、`:896`） | 页面、Reviewer | 建议与验证方式 |

### 7.3 `dist/`

| 文件 | 谁写 | 谁读 | 读它做什么 |
| --- | --- | --- | --- |
| `dist/index.html`、`dist/styles.css` | Builder（从示例目录复制，`builder/build_site.py:78-81`） | `serve.py`、页面 | 页面骨架与样式 |
| `dist/app.bundle.js` | Builder（复制或 bun 打包，`builder/build_site.py:116-119`） | `serve.py`、页面 | 浏览器脚本 |
| `dist/data/diagnosis.json` | Builder（快照字节原样复制，`builder/build_site.py:115`） | `serve.py`（映射到 `/data/diagnosis.json`）、Reviewer（字节比对） | 页面数据源 |
| `dist/build-report.json` | Builder（`builder/build_site.py:83-105`） | 自检、审查 | 产物清单与 sha256 |
| 快照原件 `--data` | DA | Builder、Reviewer | 校验与字节比对基准 |

## 8. 已知契约缺口

以下仍成立的缺口都在当前 main 上本地复现过（命令见第 9 节）；第 1、2、4 条已由当前代码修复，
其行为也在第 9 节记录。issue 编号按本 issue 的说明给出；
离线环境无法读取 GitHub issue 正文，**编号与缺口的对应以本文的代码证据为准**，
若与 issue 标题不一致，以 issue 为准并修订本文。

| # | 缺口 | 证据 | 影响 | 对应 issue |
| --- | --- | --- | --- | --- |
| 3 | **DA 口径说明脚本把口径文字里的数字误判为泄漏**：`write_method.py` 会把任务书 `口径` 原文写进「统一口径」段（`da/write_method.py:20-31`、`:96-140`），再拿快照里的数值做泄漏比对（`da/write_method.py:163-181`） | 对 Boss 自己产出的任务书（含 `demo/brief.json`）执行即报「生成文档包含诊断数值：0、2026」，退出码 1，不产出 `da-method.md` | 编排里 DA 阶段的仓库内产物无法生成 | 本 issue #142 记录，尚未单独立项 |
| 6 | **期次键名与观察月份写死在结构定义和页面上**：schema 的期次键是 `2026-01实际`/`2026-05H1参考`/`2026-09实际`/`年末测算`/`年末目标`（`schema/diagnosis.schema.json:184-228`），页面要求 `scope.观察月份 === "2026-09"`（`app.js:73-82`、`:110`） | `--month 2026-06` 生成的任务书即使补齐 DA 所需字段，DA 也以「生成的快照未通过结构校验」退出 1（缺 `2026-09实际`） | 除 2026-09 外的观察月份不可用 | 本 issue #142 记录，尚未单立项 |

### 已修复的缺口

以下缺口已不再成立，保留在此记录修复证据：

| # | 缺口 | 修复证据 | 对应 issue |
| --- | --- | --- | --- |
| 5 | **编排 spec 的 `--verify` 命令曾与实际 CLI 不符** | DA 阶段已改为 `python3 openbot/examples/org-health-local/validate.py "${DIAGNOSIS}" && test -s ...`，使用位置参数（`openswarm/examples/org-diagnosis-pipeline.json:156-158`）；Builder 阶段已改为 `python3 -m unittest discover -s openbot/examples/org-health-local -p 'test_*.py'`（`:210-212`）。 | 本 issue #142 记录 |
| 7 | **文档与仓库内容曾发生漂移** | `pipeline.md` 已说明 `validate.py`、`test_serve.py` 已在仓库内（`docs/scenario/org-diagnosis/pipeline.md:80-84`、`:102-105`）；`review-checklist.md` 已说明三个脚本均已在仓库内（`docs/scenario/org-diagnosis/review-checklist.md:51`）；编排 spec 的 `pending` 条目也写明仓库已有（`openswarm/examples/org-diagnosis-pipeline.json:268-275`）。 | 本 issue #142 记录 |

## 9. 复现命令与本地实测

以下命令在 `openbot/examples/org-health-local/` 下执行，全部只读仓库内文件，
输出写到仓库外的工作目录。

```bash
# 1. Boss 产物：成功（退出码 0）
python3 boss/make_brief.py --out "$W/brief.json"

# 2. DA 直接消费 Boss 产物：成功（退出码 0；Boss 期次格式与 fx.csv 均被消费）
python3 da/build_diagnosis.py --brief "$W/brief.json" --raw da/sample-raw --out "$W/diagnosis.json"

# 3. Builder 构建 + 自检：构建 0，自检 0（文件清单数组兼容）
python3 builder/build_site.py --data "$W/diagnosis.json" --out "$W/dist" --bundle "$W/app.bundle.js"
python3 builder/check_site.py "$W/dist"

# 4. DA 口径说明：对 Boss 任务书失败（退出码 1）→ 缺口 3
python3 da/write_method.py --brief "$W/brief.json" --data "$W/diagnosis.json" --out "$W/da-method.md"

# 5. Reviewer 三方核对：10 项全 PASS（退出码 0）
python3 review/cross_check.py --brief "$W/brief.json" --data "$W/diagnosis.json" --dist "$W/dist"

# 6. validate.py 当前使用位置参数；以下两条旧写法仍失败（仅作旧写法演示，编排 spec 已不再使用）
python3 validate.py "$W/diagnosis.json"
python3 validate.py --data "$W/diagnosis.json"
python3 test_serve.py --data "$W/diagnosis.json"

# 7. 仓库内一键流程：Boss → DA → Builder → 自检 → Reviewer，成功（退出码 0）
python3 run_e2e.py --workdir "$W/e2e" --bundle "$W/app.bundle.js" --review
```

本地实测结果（Python 3.12.3，只用标准库）：

| 命令 | 退出码 | 关键输出 |
| --- | --- | --- |
| `boss/make_brief.py --out …` | 0 | 已写出任务书 |
| `da/build_diagnosis.py`（Boss 产物 + `fx.csv`） | 0 | 已生成诊断快照，`观察期间.期次` 与 `fx.csv` 汇率均被消费 |
| `da/build_diagnosis.py --month 2026-06` | 1 | 生成的快照未通过结构校验（缺 `2026-09实际`） |
| `da/write_method.py`（Boss 任务书） | 1 | 生成文档包含诊断数值：0、2026 |
| `builder/build_site.py`（合法快照） | 0 | 已生成站点，5 个产物 |
| `builder/build_site.py`（`schema版本=3`） | 1 | 校验失败，且不生成 `dist` |
| `builder/check_site.py`（build_site 产物） | 0 | 站点检查通过，文件清单 sha256 全部一致 |
| `review/cross_check.py` | 0 | 10 项检查全部通过 |
| `validate.py …` | 0 | 校验通过（位置参数） |
| `validate.py --data …` / `test_serve.py --data …` | 2 | unrecognized arguments: --data；这是旧写法的演示，编排 spec 已不再使用这两条命令 |
| `run_e2e.py --workdir … --review` | 0 | 审查完成并写出 `review.md` |

仓库现有单测（CI 口径见 `.github/workflows/phanes.yml:77-87`）的验收命令为：
`boss/test_make_brief.py`、`da/test_build_diagnosis.py`、`da/test_write_method.py`、
`builder/test_build_site.py`、`builder/test_check_site.py`、`review/test_cross_check.py`、
`test_serve.py`、`test_validate.py`、`test_run_e2e.py`。这些单测都自造夹具，
**不覆盖角色之间的完整交接**。本机沙箱运行时 `test_serve.py` 的回环 socket 被环境策略拒绝；
宿主验收需按上面的命令重新执行。

## 10. 未决事项

- 缺口 6 涉及结构定义与页面硬编码，改动面覆盖 `schema/diagnosis.schema.json`、`app.js` 与
  `openbot/packages/i18n/src/messages/zh/orgHealthDemo.ts`，需要单独评估。
- `write_method.py` 的泄漏自检规则（哪些数字算「诊断数值」）目前按字符串匹配判定，
  与「口径文字里允许出现期间与单位」的规则冲突，需要先定义白名单再改实现。
