# 组织诊断场景主理人常见问题（faq.md）

Refs #6, Refs #185。

本文回答组织健康诊断场景（Boss/DA/Builder）的**主理人**在动手操作时最常问的 10 个问题：数据从哪来、
怎么一条命令重新生成、怎么预览、怎么换观察月份、怎么换汇率、真实数据怎么接进来、怎么校验、
怎么跑测试、谁做独立审查与最终放行。

所有答案都基于当前 `main` 上的真实脚本与命令，命令可在仓库内复现；每题的「实测」是按文中命令在本机
跑出来的结果（Python 3.13，只用标准库）。配套阅读：术语口径见 [glossary.md](glossary.md)，
命令行与退出码契约见 [contract.md](contract.md)，人工审查项见 [review-checklist.md](review-checklist.md)，
运行与排障见 [runbook.md](runbook.md)，角色职责与验收清单见 [README.md](README.md)。

下文命令默认在 `openbot/examples/org-health-local/` 目录下执行；文中给出的路径都相对仓库根。

---

## Q1 诊断数据从哪来？能直接换成我们公司的真实数据吗？

**答：**

- 仓库内的诊断数据都是人工构造的合成示例：`openbot/examples/org-health-local/data/diagnosis.json`（页面默认读它，
  `openbot/examples/org-health-local/serve.py:12`）与 `openbot/examples/org-health-local/demo/diagnosis.json`（demo 产物）；
  仓库内不允许出现真实组织数据（`openbot/examples/org-health-local/README.md:45`、`openbot/examples/org-health-local/README.md:47`）。
- DA 的输入是「任务书 + 原始数据目录」。原始数据目录放四张 CSV，表头固定
  （`openbot/examples/org-health-local/da/build_diagnosis.py:55-68`）：

  | 文件 | 必需列 |
  | --- | --- |
  | `cost.csv` | `月份`、`整体技术人力成本万元`、`营收业务技术人力成本万元`、`运维设施成本万元` |
  | `heads.csv` | `月份`、`整体折算人头`、`营收业务折算人头` |
  | `revenue.csv` | `月份`、`收入万美元` |
  | `forecast.csv` | `期次`（`年末测算` / `年末目标`）+ 上面三张表的成本、人头、收入列 |
  | `fx.csv`（可选） | `人民币每美元`；任务书没写汇率时才读它（`openbot/examples/org-health-local/da/build_diagnosis.py:194-198`） |

  合成样本在 `openbot/examples/org-health-local/da/sample-raw/`，其中 `fx.csv` 的合成汇率为 7.2。
- 换成真实数据 = 把 `--raw` 指向**仓库外**的绝对路径（`openbot/examples/org-health-local/da/build_diagnosis.py:1008`），
  产出的快照也留在仓库外；交接只给「路径 + sha256」，不贴数据正文
  （`docs/scenario/org-diagnosis/README.md` 第 3.3 节）。

**实测：**

```bash
python3 boss/make_brief.py --out "$W/brief.json"
python3 da/build_diagnosis.py --brief "$W/brief.json" --raw da/sample-raw --out "$W/diagnosis.json"
python3 validate.py "$W/diagnosis.json"
```

退出码 0；快照 `scope` 为 `观察月份=2026-09`、`数据截止=2026-09-30`、`来源=合成示例数据`、`schema版本=4`。

---

## Q2 一条命令能把 brief → 快照 → 站点 → 审查 全跑一遍吗？

**答：** 可以，用编排脚本 `run_e2e.py`：

```bash
python3 run_e2e.py --workdir <工作目录> [--review]
```

- `--workdir` 必填；产物写在它下面：`brief.json`、`diagnosis.json`、`dist/`、`review.md`
  （`openbot/examples/org-health-local/run_e2e.py:16-24`、`:54-64`、`:11`）。
- 步骤固定为 Boss → DA → Builder → 站点自检（`--review` 时再加独立审查）；**任一步非 0 立即中断并返回 1**
  （`openbot/examples/org-health-local/run_e2e.py:65-68`）。
- 结束时打印预览命令 `python3 serve.py --root <dist>`（`openbot/examples/org-health-local/run_e2e.py:73`）。
- 默认 `--raw da/sample-raw`（`openbot/examples/org-health-local/run_e2e.py:17`）；`--boss/--da/--builder/--checker/--reviewer`
  可替换各角色脚本（`openbot/examples/org-health-local/run_e2e.py:19-23`）。
- 想单步跑就各跑各的，命令见本文 Q8 与各题的「命令」段。

**实测：** `python3 run_e2e.py --workdir <目录> --review` 退出码 0，产出 `brief.json`、`diagnosis.json`、`dist/`、`review.md`；
`review.md` 写「共 10 项检查：10 项通过，0 项失败」。

---

## Q3 本机没有 Bun，跑不起来怎么办？

**答：** 用仓库内置的预编译浏览器包，不用装 Bun、不用联网：

```bash
python3 run_e2e.py --workdir <工作目录> --bundle demo/vendor/app.bundle.js
```

- 不传 `--bundle` 时，`run_e2e.py` 会自动用 `demo/vendor/app.bundle.js`
  （`openbot/examples/org-health-local/run_e2e.py:42-48`）。
- `builder/build_site.py --bundle <file>` 是整包复制；不给 `--bundle` 才现场调用
  `bun build app.js --target=browser --outfile=...`（`openbot/examples/org-health-local/builder/build_site.py:67-74`、`:117-119`）。
- `serve.py` 不带 `--root` 时同样要现场打包（`openbot/examples/org-health-local/serve.py:89-92`、`:111-112`）；
  预览 Builder 产物请用 `--root`（见本文 Q4）。
- 只想验证 Builder → 自检链路、不装 Bun 也行：按
  `openbot/examples/org-health-local/README.md:172-188` 的「无 Bun 的单角色本机复现」用假 bundle 跑一遍。

**实测：** 本机无 `bun`，`run_e2e.py --workdir <目录> --review` 自动使用 `demo/vendor/app.bundle.js`，退出码 0。

---

## Q4 怎么在本机预览？换成别的数据怎么预览？

**答：**

```bash
# 预览 Builder 产出的站点目录（不再现场打包）
python3 serve.py --root <工作目录>/dist

# 直接预览某一份诊断快照（真实数据用仓库外绝对路径）
python3 serve.py --data /absolute/path/to/diagnosis.json
```

- 参数：`--port`（默认 8765）、`--root`、`--data`（`openbot/examples/org-health-local/serve.py:52-57`）。
  指定 `--root` 时数据默认取 `<root>/data/diagnosis.json`，可用 `--data` 单独覆盖。
- 启动后打印预览地址、站点根目录、诊断数据文件与路径白名单
  （`openbot/examples/org-health-local/serve.py:95-100`），照着打印出来的地址访问即可。
- 语言：默认中文，URL 追加 `?lang=en` 看英文界面（`openbot/examples/org-health-local/README.md:45`）。
- 安全边界：只监听回环地址；`Host` 不是 `127.0.0.1:<port>`/`localhost:<port>` 返回 403，路径不在白名单
  （`/`、`/index.html`、`/app.bundle.js`、`/styles.css`、`/data/diagnosis.json`）返回 404，目录列表关闭、请求日志不落盘
  （`openbot/examples/org-health-local/serve.py:14`、`:28-33`、`:41-47`）。
- 端口被占用时打印明确错误并以非 0 退出，**不会**静默换端口（`openbot/examples/org-health-local/serve.py:81`、`:106-110`）；
  先停掉旧进程，或用 `--port 8766` 换端口。

**实测：** `python3 serve.py --root <dist> --port 8791` 后，`/` 返回 200、`/data/diagnosis.json` 返回 200、
非本机 `Host` 返回 403。

---

## Q5 想换观察月份，怎么做？

**答：** Boss 阶段有开关：

```bash
python3 boss/make_brief.py --out <brief.json> --month YYYY-MM
```

- `--month` 覆盖观察月份（格式 `YYYY-MM`，默认 `2026-09`，`openbot/examples/org-health-local/boss/make_brief.py:37`、`:262-265`）；
  数据截止按月末推算（`openbot/examples/org-health-local/boss/make_brief.py:129-133`），
  期次锚点按 `<年>-01实际`、`<年>-05H1参考`、`<观察月>实际`、`年末测算`、`年末目标` 生成
  （`openbot/examples/org-health-local/boss/make_brief.py:114-126`）。
- **但当前 `main` 只支持 2026-09**：结构定义把期次键写死为 `2026-01实际`/`2026-05H1参考`/`2026-09实际`
  （`openbot/examples/org-health-local/schema/diagnosis.schema.json:187`、`:200`、`:208`、`:216`、`:224`），
  换月份生成的快照过不了结构校验，DA 直接退出 1。
- 换月份不是改配置，而是改结构：结构定义的期次键、页面与文案里写死的观察月份与时间条
  （`openbot/examples/org-health-local/app.js:23-28`）、合成样本 CSV 的月份行都要一起改。
- 同理，**改动四个指标的标题或顺序，页面直接判结构错误**，不做静默降级
  （`openbot/examples/org-health-local/app.js:94-107`）；顺序见
  `openbot/examples/org-health-local/boss/make_brief.py:41-46`。

**实测：**

```bash
python3 boss/make_brief.py --out "$W/brief-06.json" --month 2026-06
python3 da/build_diagnosis.py --brief "$W/brief-06.json" --raw da/sample-raw --out "$W/diagnosis-06.json"
```

退出码 1，报「生成的快照未通过结构校验」，逐条列出 8 处
`/metrics/0/数值/2026-09实际: 缺少必需字段` 一类的错误。

---

## Q6 汇率怎么换？换了会影响哪些数字？

**答：** 两条路径，任务书优先：

1. 写进任务书：`python3 boss/make_brief.py --out <brief.json> --fx 7.0`
   （`openbot/examples/org-health-local/boss/make_brief.py:266`）；必须是正数（人民币/美元），
   否则退出码 2（`openbot/examples/org-health-local/boss/make_brief.py:191-194`、`:289-293`）。
2. 任务书没写时：DA 从 `--raw` 目录的 `fx.csv` 首行 `人民币每美元` 读取
   （`openbot/examples/org-health-local/da/build_diagnosis.py:194-198`）。

两处都没有就报错退出 1：「任务书 `口径.汇率` 缺失或不是正数，无法计算成本率」
（`openbot/examples/org-health-local/da/build_diagnosis.py:207`）。

- 汇率只影响成本率：`成本率 = 成本 ÷ (收入 × 汇率)`，成本与收入折成同一币种后计算
  （`openbot/examples/org-health-local/da/build_diagnosis.py:336-340`）；成本、人头、收入本身不随汇率变。
- 快照的「口径说明」里会写明用了哪个汇率（`openbot/examples/org-health-local/da/build_diagnosis.py:930`）。

**实测：** 同一份合成样本、其余输入不变：

| 汇率来源 | 汇率 | 整体技术人力成本率 2026-01 实际 | 2026-09 实际 |
| --- | --- | --- | --- |
| `da/sample-raw/fx.csv`（缺省） | 7.2 | 约12.9% | 约12.7% |
| `make_brief.py --fx 7.0` | 7.0 | 约13.3% | 约13.0% |

把 `fx.csv` 从原始目录移走、任务书又不写汇率时，DA 退出码 1 并报上面那句错误。

---

## Q7 真实组织数据怎么接进来？边界在哪里？

**答：** 三处入口按「仓库外绝对路径」使用真实数据（脚本本身也接受相对路径，但相对路径只在示例目录内可用）：

| 环节 | 参数 | 约束 |
| --- | --- | --- |
| DA 取数 | `da/build_diagnosis.py --raw /abs/path/to/raw --out /abs/path/to/diagnosis.json` | 原始 CSV 与快照都在仓库外（`openbot/examples/org-health-local/da/build_diagnosis.py:1007-1010`） |
| Builder 建站 | `builder/build_site.py --data /abs/path/to/diagnosis.json --out <dist>` | 快照不进仓库；构建报告记录输入路径与 sha256（`openbot/examples/org-health-local/builder/build_site.py:83-105`） |
| 预览 | `serve.py --data /abs/path/to/diagnosis.json` | 同源 `/data/diagnosis.json` 由服务端映射到该文件（`openbot/examples/org-health-local/serve.py:36-39`） |

- 交接用「路径 + sha256」：构建报告里记快照摘要与每个产物的摘要
  （`openbot/examples/org-health-local/builder/build_site.py:83-105`），审查脚本比对
  `dist/data/diagnosis.json` 与 `--data` 是否字节相同并打印 sha256 前 12 位
  （`openbot/examples/org-health-local/review/cross_check.py:385-397`）。
- 隐私边界：快照字段名（含嵌套）不得命中 27 个个人/薪酬/编制粒度黑名单词条
  （`openbot/examples/org-health-local/review/cross_check.py:108-137`），命中即判 FAIL。
- 真实数据不进仓库、Issue、PR、聊天与工单，仓库内只留合成示例与不含数值的说明
  （`docs/scenario/org-diagnosis/README.md` 第 3.3 节、第 5.3 节）。

**实测：** 用 `--raw da/sample-raw`（仓库内合成样本）跑通全链路；快照 `scope.来源` 如实写「合成示例数据」。

---

## Q8 怎么校验快照和站点产物？

**答：** 两个校验器，各自有退出码：

```bash
# 1) 快照：只接受位置参数（不支持 --data）
python3 validate.py                       # 默认校验 data/diagnosis.json
python3 validate.py /abs/path/to/diagnosis.json
python3 validate.py --json /abs/path/to/diagnosis.json   # 机器可读结果

# 2) 站点产物
python3 builder/check_site.py <dist>
```

- `validate.py`：退出码 `0` 通过 / `1` 校验失败 / `2` 输入不可用（文件不可读或结构定义不可用）
  （`openbot/examples/org-health-local/validate.py:28-30`、`:304-316`）；
  失败时逐条打印出错路径（如 `/scope/schema版本`）。
  **注意**：它没有 `--data` 选项，只有位置参数与 `--schema`、`--json`
  （`docs/scenario/org-diagnosis/contract.md` 第 8 节缺口 5 记录了这一点）。
- `check_site.py`：检查 5 个产物存在且非空、快照过 `validate.py`、`index.html`/`styles.css` 无外部
  `http(s)` 引用、`build-report.json` 里的 sha256 与磁盘一致
  （`openbot/examples/org-health-local/builder/check_site.py:15-21`、`:75-153`）；有失败项退出码 1。
- `builder/build_site.py` 先校验再构建：数据不合格不生成 `dist`、退出码 1
  （`openbot/examples/org-health-local/builder/build_site.py:165-171`）；打包或写盘失败退出码 3，
  并删掉本次新建的 `dist`（`openbot/examples/org-health-local/builder/build_site.py:174-181`）。

**实测：** 对 `run_e2e.py` 产出的 `dist` 跑 `check_site.py`，14 条「通过」全部命中，退出码 0。

---

## Q9 怎么跑测试？不打开浏览器怎么核对页面内容？

**答：**

- 单元测试（只用 Python 标准库，不需要 Bun、不需要浏览器）：

  ```bash
  cd openbot/examples/org-health-local && python3 run_tests.py
  ```

  覆盖根目录与 `boss/`、`da/`、`builder/`、`review/` 子包的全部 `test_*.py`
  （`openbot/examples/org-health-local/run_tests.py:14-19`）。
- 浏览器端到端（需要 Bun 与 Chrome）：在**仓库根目录**跑 `bun run test:org-health-demo`
  （`openbot/package.json:194`），覆盖中英文、回退语言、无效数据、无效 Host 与私有路径，
  结果写 `.openbot-build/`（`openbot/examples/org-health-local/README.md:168`）。
- 不打开浏览器核对页面会展示什么：`python3 demo/make_site_summary.py`
  （`openbot/examples/org-health-local/demo/make_site_summary.py:422-423`），生成
  `demo/site-summary.md`：指标卡片的标题与各期次数值、口径说明条目、洞察/建议条数，
  以及页面字段与快照字段的对应关系。

**实测：** `python3 run_tests.py` 输出 `Ran 165 tests: 0 failures, 0 errors`，退出码 0。

---

## Q10 独立审查怎么做？谁最终放行？

**答：** 机器审查 + 人工审查两道，最终放行由人做。

```bash
python3 review/cross_check.py --brief <brief.json> --data <diagnosis.json> [--dist <dist>] [--out <报告.md>]
```

- 逐条输出 `PASS`/`FAIL`，退出码 `0` 全部通过 / `1` 至少一项失败 / `2` 输入不可读或不是 JSON 对象
  （`openbot/examples/org-health-local/review/cross_check.py:50-52`、`:466-474`）；脚本只读，不改任何文件。
- 10 项检查：期间一致、版本一致、6 条必答问题覆盖、站点数据一致、隐私边界
  （`openbot/examples/org-health-local/review/cross_check.py:200-243`、`:345-397`、`:422-437`）。
- `run_e2e.py --review` 会把审查记录写到 `<workdir>/review.md`，**失败路径也照常写**，
  便于核对失败原因（`openbot/examples/org-health-local/run_e2e.py:11`、`:69-72`）。
- 人工审查按 `docs/scenario/org-diagnosis/review-checklist.md` 逐项复核，结论模板在该文件末尾；
  审查者不得是证据产出者或站点实现者。
- 放行：机器审查只是第二意见，最终验收属控制面、由人做出，验收要引用独立复核记录与可复现命令
  （`docs/scenario/org-diagnosis/README.md` 第 5.4 节）。

**实测：** `run_e2e.py --review` 产出的 `review.md` 记录「共 10 项检查：10 项通过，0 项失败」，退出码 0。

---

## 复现记录

本文所有「实测」来自同一次本机运行（Python 3.13，只用标准库，无 Bun、无网络），
工作目录取仓库外的临时目录：

```bash
cd openbot/examples/org-health-local
W=<仓库外临时目录>

# Q1 数据链路
python3 boss/make_brief.py --out "$W/brief.json"
python3 da/build_diagnosis.py --brief "$W/brief.json" --raw da/sample-raw --out "$W/diagnosis.json"
python3 validate.py "$W/diagnosis.json"                      # 退出码 0

# Q5 换月份（当前 main 上失败）
python3 boss/make_brief.py --out "$W/brief-06.json" --month 2026-06
python3 da/build_diagnosis.py --brief "$W/brief-06.json" --raw da/sample-raw --out "$W/diagnosis-06.json"   # 退出码 1

# Q6 换汇率
python3 boss/make_brief.py --out "$W/brief-fx.json" --fx 7.0
python3 da/build_diagnosis.py --brief "$W/brief-fx.json" --raw da/sample-raw --out "$W/diagnosis-fx.json"     # 退出码 0

# Q2 全链路
python3 run_e2e.py --workdir "$W/e2e" --review                # 退出码 0，review.md 10 项全 PASS

# Q9 单元测试
python3 run_tests.py                                          # Ran 165 tests: 0 failures, 0 errors
```

## 已知边界（本文不展开）

- **换观察月份当前不可用**：结构定义写死 2026-09 的期次键（见本文 Q5），放开需要改
  `openbot/examples/org-health-local/schema/diagnosis.schema.json`、页面与文案，属结构改动。
- **`validate.py` 不接受 `--data`**：只用位置参数；`docs/scenario/org-diagnosis/pipeline.md` 与
  `openswarm/examples/org-diagnosis-pipeline.json` 里的 verify 命令仍按 `--data` 写法，按实际 CLI 为准
  （`docs/scenario/org-diagnosis/contract.md` 第 8 节缺口 5）。
- **真实数据链路未演练**：本文实测全部使用仓库内合成样本；接真实数据前需先确认期间、指标定义与结构一致
  （`openbot/examples/org-health-local/README.md:47`）。
