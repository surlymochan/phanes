# 组织诊断场景主理人运行手册 runbook.md

Refs #6, Refs #156。

本文档面向组织健康诊断场景（Boss/DA/Builder 三角色）的主理人与运维人员，指导如何在本地或沙箱环境中以一条命令运行完整三 Bot 演示流水线（`run_e2e.py`）、核对每步产物路径、使用内置服务进行本地预览（`serve.py --root`），以及排查修复最常见的运行失败。

仓库内所有命令均基于当前 `main` 分支的实际代码编写；所有测试与演示仅使用合成样本数据，真实组织数据必须严格保存在仓库外，不得写入版本库。

---

## 1. 一条命令跑完三 Bot Demo

主理人可以使用编排脚本 `run_e2e.py` 一次性串联执行 Boss 任务书生成、DA 数据分析、Builder 静态建站与站点静态检查；加上 `--review` 时再执行一次独立三方审查：

### 1.1 执行命令

在仓库根目录下运行：

```bash
python3 openbot/examples/org-health-local/run_e2e.py \
  --workdir .openswarm-tmp/demo-run \
  --bundle openbot/examples/org-health-local/demo/vendor/app.bundle.js
```

或者进入示例目录 `openbot/examples/org-health-local` 运行：

```bash
cd openbot/examples/org-health-local
python3 run_e2e.py --workdir /tmp/org-diagnosis-demo --bundle demo/vendor/app.bundle.js
```

默认只跑 Boss → DA → Builder → 站点自检 4 步；追加 `--review` 会在站点自检之后再执行一次独立三方审查，并把审查记录写到 `<workdir>/review.md`。

### 1.2 参数说明

参数定义见 `openbot/examples/org-health-local/run_e2e.py:16-24`：

| 参数 | 必需/可选 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `--workdir` | **必需** | 无 | 本次运行的工作目录（若不存在会自动创建），用于存放流水线产生的所有工件（任务书、诊断快照、静态站点目录等）。推荐放在仓库外（如 `~/phanes-runs/demo`）或临时目录。 |
| `--bundle` | 可选 | `None` | 预编译好的前端浏览器脚本路径（`demo/vendor/app.bundle.js`，由 #121/#130 引入）。**强烈推荐在无 Bun/Node 环境或 fleet lane 中指定此参数**；指定后 Builder 阶段将直接复制该 bundle，无需在本地安装 Bun 或调用 `bun build`。 |
| `--raw` | 可选 | `openbot/examples/org-health-local/da/sample-raw` | 原始数据 CSV 所在目录，默认使用仓库内置的合成样本数据（包含 `cost.csv`、`heads.csv`、`revenue.csv`、`forecast.csv`、`fx.csv`）。若使用真实数据，需指向仓库外的绝对路径。 |
| `--boss` | 可选 | `openbot/examples/org-health-local/boss/make_brief.py` | Boss 角色脚本路径，用于生成标准任务书 `brief.json`。 |
| `--da` | 可选 | `openbot/examples/org-health-local/da/build_diagnosis.py` | DA 角色脚本路径，用于将原始 CSV 归并计算为 schema v4 诊断快照 `diagnosis.json`。 |
| `--builder` | 可选 | `openbot/examples/org-health-local/builder/build_site.py` | Builder 角色脚本路径，用于根据诊断快照构建可本地浏览的静态站点目录 `dist`。 |
| `--checker` | 可选 | `openbot/examples/org-health-local/builder/check_site.py` | 静态合规检查脚本路径，用于检查 `dist` 产物的完整性与安全性。 |
| `--reviewer` | 可选 | `openbot/examples/org-health-local/review/cross_check.py` | 独立审查脚本路径，核对任务书、诊断快照与站点产物的三方一致性与隐私边界。 |
| `--review` | 可选 | 关闭（不审查） | 站点自检通过后追加独立三方交叉审查，并把审查记录写入 `<workdir>/review.md`（`openbot/examples/org-health-local/run_e2e.py:24`、`:68-72`）。审查不通过时流水线返回非 0，但报告文件仍然保留。 |

---

## 2. 每步流水线执行与产物路径

`run_e2e.py:54-67` 固定按 Boss → DA → Builder → 站点自检 4 步执行；带上 `--review` 时才追加第 5 步独立审查（`run_e2e.py:68-72`）。每步均校验返回码，任一步非 0 立即中断流水线并报错退出（返回码 1，`run_e2e.py:34-36`、`:65-67`）：

```
[Boss] make_brief.py
   │
   ▼ 产物: <workdir>/brief.json
[DA] build_diagnosis.py
   │
   ▼ 产物: <workdir>/diagnosis.json
[Builder] build_site.py [--bundle ...]
   │
   ▼ 产物: <workdir>/dist/ (包含 index.html, app.bundle.js, styles.css, diagnosis.json, build-report.json)
[检查] check_site.py <workdir>/dist
   │
   ▼ (静态合规校验，不产出新文件)
[审查] cross_check.py --brief ... --data ... --dist ...   ← 仅 --review 时执行
   │
   ▼ 产物: <workdir>/review.md (审查记录；审查失败也照常写出)
完成，输出预览命令
```

- 站点自检失败时流水线在第 4 步中断，**不会**执行审查步骤，也不会生成 `<workdir>/review.md`。
- 审查不通过时 `<workdir>/review.md` **仍然写出**（内含 `FAIL` 行），`run_e2e.py` 返回非 0，并且不再打印成功路径才有的预览命令（`run_e2e.py:70-73`）。

### 任一步失败时的排查

`run_e2e.py` 任一步失败都会停止后续步骤并返回退出码 `1`；先打开 `<workdir>/timings.json`，按最后一条记录的 `name` 和 `exit_code` 定位失败步骤，再结合该步骤的终端日志。已完成步骤的产物会保留在工作目录：依次检查已生成的 `<workdir>/brief.json`、`<workdir>/diagnosis.json` 和 `<workdir>/dist/`，不会回滚或清理。只有带 `--review` 且流程已进入审查时才会写 `<workdir>/review.md`；审查失败也保留该文件，可查看其中的 `FAIL` 行。更早步骤失败时 `review.md` 不存在是正常情况（`contract.md:287`、`:292-295`；`run_e2e.py:145-189`）。

### 步骤 1：Boss 任务书生成
- **命令**：`python3 boss/make_brief.py --out <workdir>/brief.json`
- **产物路径**：`<workdir>/brief.json`
- **内容说明**：生成符合 `boss/brief.schema.json` 结构的 schema v4 任务书，内含：
  - `目标`：复核整体成本、人头、营收业务匹配情况与成本率差异。
  - `观察期间`：观察月份 `2026-09`、数据截止 `2026-09-30`，以及 5 个期次锚点（`2026-01实际`、`2026-05H1参考`、`2026-09实际`、`年末测算`、`年末目标`）。
  - `口径`：四个核心指标的单位、定义、期间及缺失值处理规则。
  - `必答问题`：6 条待回答的业务问题。
  - `数据需求`：6 项字段路径与映射说明。
  - `禁止事项`：禁止写入真实个人数据、禁止编造数字、禁止修改指标顺序等约束。

### 步骤 2：DA 诊断快照计算
- **命令**：`python3 da/build_diagnosis.py --brief <workdir>/brief.json --raw <raw> --out <workdir>/diagnosis.json`
- **产物路径**：`<workdir>/diagnosis.json`
- **内容说明**：符合 `schema/diagnosis.schema.json` 规范的 schema v4 诊断数据快照，包含：
  - `scope`：声明 `schema版本: 4`、观察月份与数据截止。
  - `conclusion`：组织健康核心诊断结论。
  - `origin`：数据来源（合成样本或外部授权数据源）与汇率说明。
  - `metrics`：严格按顺序排列的 4 项聚合指标数值序列（整体技术人力成本、整体折算人头、营收业务成本与折算人头、收入与成本率）。
  - `insights`：区分事实、推断与边界的洞察分析。
  - `definitions`、`risks` 与 `recommendations`：口径说明、风险点与建议清单。

### 步骤 3：Builder 静态站点构建
- **命令**：`python3 builder/build_site.py --data <workdir>/diagnosis.json --out <workdir>/dist [--bundle <bundle>]`
- **产物目录**：`<workdir>/dist/`（见 `builder/build_site.py:35-41`）：
  - `<workdir>/dist/index.html`：静态站点基础 HTML 骨架（从模板复制）。
  - `<workdir>/dist/app.bundle.js`：浏览器运行包；带 `--bundle` 时直接由预构建文件复制，未带 `--bundle` 时由 `bun build` 现场打包生成。
  - `<workdir>/dist/styles.css`：站点样式表文件。
  - `<workdir>/dist/data/diagnosis.json`：复制进站点的诊断数据快照副本，供前端同源 fetch 读取。
  - `<workdir>/dist/build-report.json`：构建报告，记录数据输入路径、输入 sha256 摘要、生成时间及文件清单。

### 步骤 4：Checker 站点静态合规检查
- **命令**：`python3 builder/check_site.py <workdir>/dist`
- **产物**：无新文件生成（只读检查）。
- **检查内容**（见 `builder/check_site.py:67-138`）：
  - 确认 `index.html`、`app.bundle.js`、`styles.css`、`data/diagnosis.json`、`build-report.json` 5 个文件均存在且非空。
  - 使用 `validate.py` 严格校验 `data/diagnosis.json` 是否满足 schema v4。
  - 静态检查 `index.html` 与 `styles.css`，确保**无任何 http/https 外部网络引用**。
  - 检查 `index.html` 内部引用的本地静态资源是否存在。
  - 核验 `build-report.json` 中记录的文件 sha256 哈希值与磁盘实际文件一致。

### 步骤 5：Reviewer 独立三方交叉审查（仅 `--review` 时执行）
- **命令**：`python3 review/cross_check.py --brief <workdir>/brief.json --data <workdir>/diagnosis.json --dist <workdir>/dist`
  （由 `run_e2e.py --review` 调用，见 `openbot/examples/org-health-local/run_e2e.py:68-72`；审查脚本本身只读输入、不写文件，`--out <报告.md>` 可另存一份 Markdown 报告）
- **产物路径**：`<workdir>/review.md`——由 `run_e2e.py` 捕获审查者的标准输出写入（`openbot/examples/org-health-local/run_e2e.py:11`、`:70`），内容是逐条 `PASS`/`FAIL` 行加一行汇总（例如「共 10 项检查：10 项通过，0 项失败」）。
- **失败行为**：审查不通过时审查者返回非 0，`run_e2e.py` 中断并返回 1，但 `<workdir>/review.md` **照常保留**（含 FAIL 行）便于核对失败原因；若在站点自检阶段就已中断，则本步骤不执行、也不生成该报告。
- **审查内容**（见 `review/cross_check.py:10-39`）：
  - **期间一致**：核对任务书 `观察期间` 与诊断快照 `scope.观察月份`、`scope.数据截止` 严格一致，并核对快照 `metrics[*].数值` 的期次键（全口径成本率的键去掉「整体」前缀）与任务书期次锚点（Boss 格式的 `期次` 列表，或起止/参考期次）一致；缺哪些键、多哪些键都在这一行里说明。
  - **版本一致**：核对任务书与快照的 `schema版本` 均为 4。
  - **必答问题覆盖**：检查任务书中的 6 个必答问题在快照的 `insights` 或 `recommendations` 中均有明确提及（通过 ID、原文或关键词匹配）。
  - **站点数据一致**：若指定 `--dist`，核验 `<workdir>/dist/data/diagnosis.json` 与 `<workdir>/diagnosis.json` 的字节 sha256 完全相同。
  - **隐私边界**：递归核验诊断快照的所有字段名，确保不包含 28 个敏感黑名单词条（如姓名、个人、员工、薪酬、工资、编制、职级等，见 `review/cross_check.py:108-137`）。

---

## 3. 如何预览（`serve.py --root`）

流水线运行成功后，终端会打印预览提示。主理人使用仓库内置的 `serve.py` 即可在本机安全启动静态预览服务。

### 3.1 预览命令

```bash
python3 openbot/examples/org-health-local/serve.py --root <workdir>/dist
```

或在 `openbot/examples/org-health-local` 目录下执行：

```bash
cd openbot/examples/org-health-local
python3 serve.py --root /path/to/workdir/dist
```

### 3.2 运行机制与参数选项

参数定义见 `openbot/examples/org-health-local/serve.py:49-66`：

- `--root <Path>`：指定站点静态根目录（通常指向流水线产出的 `<workdir>/dist`）。
  - **关键行为**：指定 `--root` 时，服务**直接从该目录提供文件，不再调用 Bun 打包**（`serve.py:110-115`）；数据文件默认读取 `<root>/data/diagnosis.json`。
- `--data <Path>`（可选）：若需要覆盖站点目录下的数据，可传入单独的诊断快照路径。
- `--port <int>`（可选）：指定服务监听端口，默认端口为 `8765`。
- **安全边界保护**：
  - 仅监听 IPv4 Loopback（`127.0.0.1`），拒绝任何非 loopback 的 Host 请求（返回 403 Forbidden，`serve.py:28-30`）。
  - 严格限制请求路径白名单（`/`、`/index.html`、`/app.bundle.js`、`/styles.css`、`/data/diagnosis.json`），禁止目录遍历与任意文件访问（返回 404 Not Found，`serve.py:31-33`）。
  - 启动时执行严格端口预检（`ensure_port_available`），端口冲突时报错退出，绝不静默换用其它端口。

### 3.3 启动输出与访问

服务正常启动后，终端将输出预览地址与白名单信息（`serve.py:94-100`）：

```text
预览地址: http://127.0.0.1:8765/
站点根目录: /path/to/workdir/dist
诊断数据文件: /path/to/workdir/dist/data/diagnosis.json
白名单路径: /, /index.html, /app.bundle.js, /styles.css, /data/diagnosis.json
```

- **访问**：在浏览器打开终端提示的地址 `http://127.0.0.1:8765/` 即可复核图表、指标趋势与洞察结论。
- **国际化**：默认展示中文界面；在 URL 附加 `?lang=en`（如 `http://127.0.0.1:8765/?lang=en`）可查看英文界面。
- **停止服务**：在终端按下 `Ctrl+C` 即可退出。

---

## 4. 常见失败信息与修复指南

以下摘录自各脚本源码中真实的错误字符串，注明其所在文件和行号，并给出原因与修复方法：

### ① 无图形界面或沙箱环境下核对站点摘要（`make_site_summary.py`）

无法打开浏览器时，可在示例目录中运行以下命令，把站点实际展示的内容整理为
`demo/site-summary.md`，快速核对指标卡片的数值、单位和口径说明：

```bash
cd openbot/examples/org-health-local
python3 demo/make_site_summary.py
```

该脚本只使用 Python 标准库，读取 `demo/site/index.html` 与
`demo/site/data/diagnosis.json`，不会修改站点产物。摘要还会列出页面结构、口径说明及洞察/建议等条目数量；
它用于核对页面展示与诊断快照的对应关系，不替代 `demo/diagnosis.json` 的诊断结论或
`demo/review.md` 的审查结论。

常见错误与处理方式：

- **缺少 `demo/site` 站点目录或其中的 `index.html`、`data/diagnosis.json`**：先通过 Builder 构建站点，
  再重新运行摘要命令：
  `python3 builder/build_site.py --data demo/diagnosis.json --out demo/site --bundle demo/vendor/app.bundle.js`。
- **站点文件无法读取或诊断快照不是有效 JSON**：核对 Builder 是否成功完成，并检查上述两个输入文件的路径、权限和内容。
- **快照结构与页面预期不符**：确认站点中的 `data/diagnosis.json` 与当前页面由同一次 Builder 构建产生，修正后重新构建并生成摘要。

### ② 端口已被占用（`serve.py`）
- **错误字符串**：`端口 {port} 已被占用，请先停止占用进程，或用 --port 指定其他端口`
- **文件与行号**：
  - 抛出位置：`openbot/examples/org-health-local/serve.py:187`
  - 捕获输出：`openbot/examples/org-health-local/serve.py:213`（终端打印 `错误: 端口 8765 已被占用，请先停止占用进程，或用 --port 指定其他端口`，退出码非 0，不打印 traceback）
- **原因**：默认端口 `8765` 已被之前启动的 `serve.py` 实例或其他本地服务占用。`serve.py` 的端口预检机制禁止静默更换端口。
- **修复**：
  1. 使用命令查找并杀掉占用该端口的残留进程：
     ```bash
     lsof -ti :8765 | xargs kill -9
     # 或
     fuser -k 8765/tcp
     ```
  2. 或者在启动服务时通过 `--port` 指定一个未占用的端口：
     ```bash
     python3 serve.py --root <workdir>/dist --port 8766
     ```

### ③ 未指定 bundle 且环境无 Bun 导致构建失败（`build_site.py`）
- **错误字符串**：`构建失败：{cause}`
- **文件与行号**：
  - 抛出位置：`openbot/examples/org-health-local/builder/build_site.py:74`（`subprocess.run(bun_command(...), check=True)`）
  - 捕获输出：`openbot/examples/org-health-local/builder/build_site.py:174`（终端打印 `构建失败：[Errno 2] No such file or directory: 'bun'`）
- **原因**：运行环境未安装 Bun，且在执行 `run_e2e.py` 或 `build_site.py` 时未传入 `--bundle` 参数。Builder 默认尝试调用 `bun build app.js ...` 打包前端，因找不到可执行程序而失败。
- **修复**：
  1. 向 `run_e2e.py` 显式传入预编译的 bundle 路径（免去现场构建）：
     ```bash
     --bundle openbot/examples/org-health-local/demo/vendor/app.bundle.js
     ```
  2. 或在宿主机器上安装 Bun 并安装依赖：
     ```bash
     cd openbot && bun install
     ```

### ④ 原始数据目录不存在（`build_diagnosis.py`）
- **错误字符串**：`原始数据目录不存在：{raw_dir}`
- **文件与行号**：
  - 抛出位置：`openbot/examples/org-health-local/da/build_diagnosis.py:316`（`raise RawDataError(...)`）
  - 捕获输出：`openbot/examples/org-health-local/da/build_diagnosis.py:1014`（输出到 `sys.stderr`）
- **原因**：DA 执行计算时，`--raw` 参数指向的路径不存在，或在不同工作目录下使用了无效的相对路径。
- **修复**：
  1. 运行合成数据演示时，使用有效路径或绝对路径指向合成样本：
     ```bash
     --raw openbot/examples/org-health-local/da/sample-raw
     ```
  2. 运行真实数据时，确保外部目录已正确创建且路径拼写无误。

### ⑤ 原始 CSV 数据缺失必需列（`build_diagnosis.py`）
- **错误字符串**：`原始数据 {path.name} 缺少必需列：{'、'.join(missing)}`
- **文件与行号**：
  - 抛出位置：`openbot/examples/org-health-local/da/build_diagnosis.py:274`（`raise RawDataError(...)`）
  - 捕获输出：`openbot/examples/org-health-local/da/build_diagnosis.py:1014`（输出到 `sys.stderr`）
- **原因**：原始数据表头列名与 DA 规则不匹配。例如 `cost.csv` 缺少 `技术人力成本_万元` 或 `heads.csv` 缺少 `折算人头` 等字段。
- **修复**：对照 `openbot/examples/org-health-local/da/build_diagnosis.py:31-48` 定义的表头规范，核对并修改原始 CSV 的首行表头：
  - `cost.csv` 必须包含：`期次`、`技术人力成本_万元`、`营收业务技术人力成本_万元`、`运维与设施成本_万元`。
  - `heads.csv` 必须包含：`期次`、`折算人头`、`营收业务折算人头`。
  - `revenue.csv` 必须包含：`期次`、`营收业务收入_万美元`。
  - `forecast.csv` 必须包含：`项目`、`整体技术人力成本_万元`、`整体折算人头`、`营收业务技术人力成本_万元`、`营收业务折算人头`、`营收业务收入_万美元`、`运维与设施成本_万元`。
  - `fx.csv` 必须包含：`币种对`、`人民币每美元`。

### ⑥ 构建报告结构不符合检查契约（`check_site.py`）
- **错误字符串**：`失败：无法读取 build-report.json 中的 sha256（build-report.json 缺少 files 或 sha256 对象）`
- **文件与行号**：
  - 抛出位置：`openbot/examples/org-health-local/builder/check_site.py:56`（`raise ValueError("build-report.json 缺少 files 或 sha256 对象")`）
  - 捕获输出：`openbot/examples/org-health-local/builder/check_site.py:136`（添加到 `failures` 列表并输出）
- **原因**：`check_site.py` 的哈希核验函数 `report_hashes()` 期望读取 `build-report.json` 中以 `"files"` 或顶层 `"sha256"` 为键的对象映射（`dict`）。如果报告文件结构不匹配或缺少该对象，站点检查将判定失败。
- **修复**：核查 `<workdir>/dist/build-report.json`，确保包含符合 `check_site.py:54-64` 规范的哈希映射对象，例如：
  ```json
  {
    "files": {
      "index.html": { "sha256": "..." },
      "app.bundle.js": { "sha256": "..." },
      "styles.css": { "sha256": "..." },
      "data/diagnosis.json": { "sha256": "..." }
    }
  }
  ```
  若由自定义 Builder 脚本产出，请确保生成的构建报告包含 `"files"` 映射字典。

### ⑦ 独立审查未通过（`run_e2e.py --review`）
- **错误字符串**：`步骤失败: 审查 (退出码 1)`
- **文件与行号**：
  - 打印位置：`openbot/examples/org-health-local/run_e2e.py:35`（`print(f'步骤失败: {name} (退出码 {result.returncode})', file=sys.stderr)`）
  - 中断退出：`openbot/examples/org-health-local/run_e2e.py:71-72`（审查返回非 0 时 `return 1`）
- **原因**：三方交叉审查存在未通过项（期间一致、版本一致、必答问题覆盖、站点数据一致、隐私边界），或审查输入不可读、不是 JSON 对象（审查脚本退出码 2，见 `review/cross_check.py:461-481`）。
- **排查**：
  1. 打开 `<workdir>/review.md`：其中的 `FAIL <检查项>：<原因>` 行即为未通过项，末行给出「共 N 项检查：X 项通过，Y 项失败」汇总。
  2. 该报告在审查失败时**依然写出**（`run_e2e.py:10-11`、`:70-72`），不会被删除，可据此逐项修正后重跑流水线。
  3. 若流水线在站点自检阶段就已中断，则审查步骤未执行，`<workdir>/review.md` 不会生成——先解决自检失败再回来看审查结论。

---

## 5. 补充故障排查速查

| 故障现象 | 源码引用 | 原因与处理方式 |
| --- | --- | --- |
| `FAIL 隐私边界：出现个人/薪酬/编制粒度字段名：...` | `review/cross_check.py:427` / `line 473` | 快照 JSON 键名命中了 `FORBIDDEN_FIELD_TOKENS`（包含姓名、员工、工资、编制、职级等敏感词）。在 DA 归并阶段清除明细粒度字段，只保留聚合指标。 |
| `任务书 \`口径.汇率\` 缺失或不是正数，无法计算成本率` | `da/build_diagnosis.py:207` | 任务书中未声明汇率且原始数据目录下缺少 `fx.csv`。在 raw 目录下提供包含合成汇率（如 7.2）的 `fx.csv` 文件。 |
| `数据文件不存在：{data_path}` / `bundle 文件不存在：{bundle_path}` | `builder/build_site.py:145, 148` | 构建站点时传入的数据快照或 bundle 文件路径不存在。核对前置步骤是否成功生成快照，或检查 bundle 路径是否正确。 |
| `失败：{relative} 引用了外部 http(s) 资源` | `builder/check_site.py:102` | `index.html` 或 `styles.css` 中引入了 CDN、外部字体或远程样式。移除所有外部链接，确保所有资源均为本地静态文件。 |
| `步骤失败: 审查 (退出码 1)` | `run_e2e.py:35`、`:71-72` | 独立三方审查未通过（仅 `--review` 时才执行）。查看 `<workdir>/review.md` 的 `FAIL` 行定位不一致项；该报告在审查失败时仍然保留，修正后重跑即可。 |
