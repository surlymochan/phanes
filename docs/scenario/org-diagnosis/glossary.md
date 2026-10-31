# 组织诊断口径术语表 (glossary.md)

Refs #6, Refs #173, Refs #416。

本文档汇总组织诊断场景（Boss 规划 / DA 分析 / Builder 建站与审阅）中的核心口径术语、权威定义与出处。口径类术语严格提取自以下两份标准文件，不编造定义：

1. `openbot/examples/org-health-local/data/diagnosis.json` 的 `口径说明` 字段
2. `openbot/examples/org-health-local/boss/brief.schema.json`（即 `boss/brief.schema.json`）各级字段的 `description` 声明

构建与运行类术语（第 4 节）提取自示例目录的说明与实现文件：

3. [`openbot/examples/org-health-local/demo/vendor/README.md`](../../../openbot/examples/org-health-local/demo/vendor/README.md) 与 `openbot/examples/org-health-local/run_e2e.py`、`openbot/examples/org-health-local/builder/build_site.py`、`openbot/examples/org-health-local/serve.py`

---

## 1. 术语与定义汇总总览

| 术语 | 定义 | 出处（文件:字段） |
| --- | --- | --- |
| **已发生实际期次** | 2026-01 与 2026-09 为已发生实际期次。 | `openbot/examples/org-health-local/data/diagnosis.json:口径说明[0]` |
| **H1 参考期次** | 2026-05 为 H1 参考期次；为上半年冻结对照基准。 | `openbot/examples/org-health-local/data/diagnosis.json:口径说明[0], [2]` |
| **年末测算与年末目标** | 年末为测算或目标值；年末测算与年末目标为前瞻预算与目标估算，不代表已发生实际。 | `openbot/examples/org-health-local/data/diagnosis.json:口径说明[0], [2]` |
| **技术人力成本** | 单位为万元/月，不含运维与设施。 | `openbot/examples/org-health-local/data/diagnosis.json:口径说明[1]` |
| **折算人头** | 当量折算值，非自然人数。 | `openbot/examples/org-health-local/data/diagnosis.json:口径说明[1]` |
| **收入** | 单位为万美元/月。 | `openbot/examples/org-health-local/data/diagnosis.json:口径说明[1]` |
| **成本率** | 成本占同期收入折人民币比例。 | `openbot/examples/org-health-local/data/diagnosis.json:口径说明[1]` |
| **null 含义** | 表示该期次未统计、未提供或无同口径对比数据（如 5 月未单独拆分整体成本与人头），界面显示为未提供，不以 0 或其他推测值替代。 | `openbot/examples/org-health-local/data/diagnosis.json:口径说明[3]` |
| **组织诊断任务书 (brief.json)** | brief.json 的结构定义：Boss 产出的任务书，只含口径、问题与字段映射，不含真实组织数据或真实数字。口径的键与 data/diagnosis.json 的 metrics 标题一一对应。 | `openbot/examples/org-health-local/boss/brief.schema.json:description` |
| **目标** | 本次诊断要回答的问题，1-5 条。 | `openbot/examples/org-health-local/boss/brief.schema.json:properties.目标.description` |
| **必答问题** | DA 必须逐条回答的问题。 | `openbot/examples/org-health-local/boss/brief.schema.json:properties.必答问题.description` |
| **数据需求** | 要从真实数据文件读取的字段路径与口径映射。 | `openbot/examples/org-health-local/boss/brief.schema.json:properties.数据需求.description` |
| **禁止事项** | 取数与写结论时的禁止项。至少含“不得写入真实组织数据”与“不作个人/薪酬/编制判断”。 | `openbot/examples/org-health-local/boss/brief.schema.json:properties.禁止事项.description` |
| **schema版本** | 任务书结构版本，与诊断快照的 schema 版本一致，固定为 4。 | `openbot/examples/org-health-local/boss/brief.schema.json:properties.schema版本.description` |
| **观察期间** | 期间口径，与 scope 的“观察月份”“数据截止”对齐。 | `openbot/examples/org-health-local/boss/brief.schema.json:$defs.观察期间.description` |
| **观察月份** | 格式 YYYY-MM。 | `openbot/examples/org-health-local/boss/brief.schema.json:$defs.观察期间.properties.观察月份.description` |
| **数据截止** | 数据截止日期，格式 YYYY-MM-DD。 | `openbot/examples/org-health-local/boss/brief.schema.json:$defs.观察期间.properties.数据截止.description` |
| **期次** | 期次锚点，键名与站点数值序列一致。 | `openbot/examples/org-health-local/boss/brief.schema.json:$defs.观察期间.properties.期次.description` |
| **口径** | 每个数字的口径定义，键与示例 metrics 的标题一一对应。 | `openbot/examples/org-health-local/boss/brief.schema.json:$defs.口径.description` |
| **汇率** | 人民币/美元，表示每美元折合人民币数。 | `openbot/examples/org-health-local/boss/brief.schema.json:$defs.口径.properties.汇率.description` |
| **口径项** | 一个指标的口径：单位、定义、期间与缺失处理。 | `openbot/examples/org-health-local/boss/brief.schema.json:$defs.口径项.description` |
| **数据需求项** | 一条数据需求：逻辑字段路径、单位、口径与对应指标。 | `openbot/examples/org-health-local/boss/brief.schema.json:$defs.数据需求.description` |
| **对应指标** | 该字段供给的指标标题，必须是站点期望的四个标题之一。 | `openbot/examples/org-health-local/boss/brief.schema.json:$defs.数据需求.properties.对应指标.description` |
| **预构建浏览器包 (vendor bundle)** | `app.js` 的预构建浏览器包，路径 `openbot/examples/org-health-local/demo/vendor/app.bundle.js`；供没有 Bun/Node 的环境在 Builder 棒直接复用，约 7.5 MB，只含前端代码与 i18n 文案，不含任何数据。 | `openbot/examples/org-health-local/demo/vendor/README.md:3, 10-11` |

---

## 2. 诊断快照口径说明（出处：diagnosis.json）

源文件：`openbot/examples/org-health-local/data/diagnosis.json`（字段：`口径说明`，第 8-13 行）

### 2.1 期间口径

- **期间分类与定义**（`diagnosis.json:口径说明[0]`）：
  - **实际期次**：`2026-01` 与 `2026-09` 为已发生实际期次。
  - **H1 参考期次**：`2026-05` 为 H1 参考期次。
  - **年末值**：年末为测算或目标值。
- **基准与前瞻性质**（`diagnosis.json:口径说明[2]`）：
  - **H1 参考**：上半年冻结对照基准。
  - **年末测算与年末目标**：前瞻预算与目标估算，不代表已发生实际。

### 2.2 单位与比率口径

源自 `diagnosis.json:口径说明[1]`：

- **技术人力成本**：单位为万元/月，不含运维与设施。
- **折算人头**：当量折算值，非自然人数。
- **收入**：单位为万美元/月。
- **成本率**：成本占同期收入折人民币比例。

### 2.3 缺失值处理口径

源自 `diagnosis.json:口径说明[3]`：

- **null 含义**：表示该期次未统计、未提供或无同口径对比数据（如 5 月未单独拆分整体成本与人头），界面显示为未提供，不以 0 或其他推测值替代。

---

## 3. 任务书结构与口径声明（出处：brief.schema.json）

源文件：`openbot/examples/org-health-local/boss/brief.schema.json`（字段：各级 `description` 声明）

### 3.1 顶层定义与禁止规则

- **brief.json / 组织诊断任务书**（`brief.schema.json:description`，第 5 行）：
  - Boss 产出的任务书，只含口径、问题与字段映射，不含真实组织数据或真实数字。口径的键与 `data/diagnosis.json` 的 metrics 标题一一对应。
- **目标**（`brief.schema.json:properties.目标.description`，第 12 行）：
  - 本次诊断要回答的问题，1-5 条。
- **必答问题**（`brief.schema.json:properties.必答问题.description`，第 28 行）：
  - DA 必须逐条回答的问题。
- **数据需求**（`brief.schema.json:properties.数据需求.description`，第 37 行）：
  - 要从真实数据文件读取的字段路径与口径映射。
- **禁止事项**（`brief.schema.json:properties.禁止事项.description`，第 45 行）：
  - 取数与写结论时的禁止项。至少含“不得写入真实组织数据”与“不作个人/薪酬/编制判断”。
- **schema版本**（`brief.schema.json:properties.schema版本.description`，第 54 行）：
  - 任务书结构版本，与诊断快照的 schema 版本一致，固定为 4。

### 3.2 期间定义 ($defs/观察期间)

- **观察期间**（`brief.schema.json:$defs.观察期间.description`，第 60 行）：
  - 期间口径，与 scope 的“观察月份”“数据截止”对齐。
- **观察月份**（`brief.schema.json:$defs.观察期间.properties.观察月份.description`，第 66 行）：
  - 观察月份，格式 YYYY-MM。
- **数据截止**（`brief.schema.json:$defs.观察期间.properties.数据截止.description`，第 71 行）：
  - 数据截止日期，格式 YYYY-MM-DD。
- **期次**（`brief.schema.json:$defs.观察期间.properties.期次.description`，第 76 行）：
  - 期次锚点，键名与站点数值序列一致。

### 3.3 指标口径定义 ($defs/口径 与 $defs/口径项)

- **口径**（`brief.schema.json:$defs.口径.description`，第 103 行）：
  - 每个数字的口径定义，键与示例 metrics 的标题一一对应。
- **汇率**（`brief.schema.json:$defs.口径.properties.汇率.description`，第 121 行）：
  - 人民币/美元，表示每美元折合人民币数。
- **口径项**（`brief.schema.json:$defs.口径项.description`，第 128 行）：
  - 一个指标的口径：单位、定义、期间与缺失处理。
  - 所属四个固定指标键（`$defs.口径.required`）：
    1. `整体技术人力成本`
    2. `整体折算人头`
    3. `营收业务（含共享平台）技术人力成本与折算人头`
    4. `收入与成本率`

### 3.4 字段需求与指标对应 ($defs/数据需求)

- **数据需求项**（`brief.schema.json:$defs.数据需求.description`，第 152 行）：
  - 一条数据需求：逻辑字段路径、单位、口径与对应指标。
- **对应指标**（`brief.schema.json:$defs.数据需求.properties.对应指标.description`，第 170 行）：
  - 该字段供给的指标标题，必须是站点期望的四个标题之一（`整体技术人力成本`、`整体折算人头`、`营收业务（含共享平台）技术人力成本与折算人头`、`收入与成本率`）。

---

## 4. 构建与运行术语：预构建浏览器包 (vendor bundle)

### 4.1 权威定义

- **路径**：`openbot/examples/org-health-local/demo/vendor/app.bundle.js`（随仓库提交，来源说明见 [`openbot/examples/org-health-local/demo/vendor/README.md`](../../../openbot/examples/org-health-local/demo/vendor/README.md)）。
- **是什么**：`app.js` 的浏览器包，即 `bun build app.js --target=browser --minify` 的产物（`openbot/examples/org-health-local/demo/vendor/README.md:3`、`:9`）。
- **用途**：供没有 `openbot/node_modules` 的 fleet lane 与没有 Bun 的本机环境在 Builder 棒直接产出可预览的静态站点——`builder/build_site.py --bundle <file>` 把该文件整包复制为 `<dist>/app.bundle.js`，不再现场打包（`openbot/examples/org-health-local/builder/build_site.py:107-119`）。
- **体积与内容**：约 7.5 MB，主要是 `@openbot/i18n` 的全部语言文案；`app.js` 或 i18n 文案改动后，需按 `openbot/examples/org-health-local/demo/vendor/README.md:9` 的命令重建（`openbot/examples/org-health-local/demo/vendor/README.md:10`）。
- **数据边界**：只用于合成数据演示，**不包含任何数据**（`openbot/examples/org-health-local/demo/vendor/README.md:11`）；页面数据仍由站点目录里的 `data/diagnosis.json` 提供，真实组织数据必须在仓库外（`openbot/examples/org-health-local/builder/build_site.py:107-119`、`docs/scenario/org-diagnosis/README.md` 第 3.3 节）。

### 4.2 免 Bun 运行边界

| 场景 | 是否需要 Bun | 出处（文件:行号） |
| --- | --- | --- |
| `run_e2e.py` 未传 `--bundle`，且 `demo/vendor/app.bundle.js` 存在 | 否：自动复用预构建包 | `openbot/examples/org-health-local/run_e2e.py:53-59` |
| `run_e2e.py --bundle <file>` | 否：把该路径透传给 Builder | `openbot/examples/org-health-local/run_e2e.py:70-71` |
| `builder/build_site.py --bundle <file>` | 否：整包复制为 `<dist>/app.bundle.js` | `openbot/examples/org-health-local/builder/build_site.py:107-119` |
| `builder/build_site.py` 不带 `--bundle` | 是：现场调用 `bun build` | `openbot/examples/org-health-local/builder/build_site.py:15`、`:67-75`、`:116-117` |
| `serve.py --root <dist>` | 否：只服务该目录，不现场打包 | `openbot/examples/org-health-local/serve.py:110-115` |
| `serve.py` 不带 `--root` | 是：现场调用 `bun build` | `openbot/examples/org-health-local/serve.py:89-92`、`:112` |

- 免 Bun 只覆盖**前端打包**这一步：Boss、DA、Builder 的 Python 脚本只用标准库，与 Bun 无关（`openbot/examples/org-health-local/builder/build_site.py:16`、`openbot/examples/org-health-local/README.md:186-193`）。
- 预构建包不是数据来源：诊断数据仍只来自 `--data` 指向的快照；bundle 路径不存在时 `build_site.py` 退出码非 0（`openbot/examples/org-health-local/builder/build_site.py:147-152`）。
- 重建站点与预览（两条命令都不需要 Bun）：
  ```bash
  cd openbot/examples/org-health-local
  python3 builder/build_site.py --data demo/diagnosis.json --out demo/site --bundle demo/vendor/app.bundle.js
  python3 serve.py --root demo/site
  ```
  `demo/site/` 只提交 `build-report.json`，站点其余产物需本地重建（`openbot/examples/org-health-local/README.md:26`、`openbot/examples/org-health-local/demo/README.md:1`）。
- 运行与排障见 [runbook.md](runbook.md) 的「步骤 3：Builder 静态站点构建」与「未指定 bundle 且环境无 Bun 导致构建失败」；主理人问答见 [faq.md](faq.md) 的 Q3。
