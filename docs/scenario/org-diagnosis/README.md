# 组织诊断网站场景：Boss 任务书与交接契约

本文对应 Phanes #51，是 #6（Boss/DA/Builder 组织诊断网站场景）中 Boss 侧的角色契约。它规定三个角色的职责、输入、输出，交接工件清单与路径约定，以及统一验收清单。

仓库内只允许出现人工构造的合成示例数据。真实诊断数据必须留在仓库之外，不得进入仓库、Issue 或 PR，依据 `openbot/examples/org-health-local/README.md:115`。

## 1. 场景边界

- 目标：用一个只读本机 JSON 的静态站点复核组织健康诊断快照，页面不发送网络请求。
- 三个角色：Boss（规划与派工）、DA（数据分析与证据）、Builder（建站与本机验证）。
- 页面只描述聚合信号，不作个人、薪酬或编制判断（`openbot/packages/i18n/src/messages/zh/orgHealthDemo.ts:21`）；事实、推断与边界在页面上分标签展示（`openbot/packages/i18n/src/messages/zh/orgHealthDemo.ts:81-84`）。
- 站点消费的数据结构固定为 schema v4，结构或指标顺序变化时页面直接判为"加载失败"，不做静默降级（`openbot/examples/org-health-local/app.js:73-82`、`openbot/packages/i18n/src/messages/zh/orgHealthDemo.ts:59`）。

## 2. 三角色职责、输入与输出

| 角色 | 职责 | 输入 | 输出 |
| --- | --- | --- | --- |
| Boss 规划 | 拆解诊断问题；固定期间、指标定义与口径版本；编写任务书与验收标准；派工与放行判定；拒绝越界数据 | 诊断问题与业务边界；可用数据源及其敏感级别；合成示例站点 | 任务书 `<scenario-workdir>/plan.md`（仓库外）：口径、期间、字段要求、隐私边界、验收清单、交付要求 |
| DA 数据分析 | 按任务书取数与计算；区分事实、推断与边界；标注缺失证据；保留来源与口径说明 | `<scenario-workdir>/plan.md`；授权范围内的数据源访问 | 证据包 `<scenario-workdir>/evidence/`（仓库外）：口径说明、计算过程、来源引用、缺失证据清单；诊断快照 `<scenario-workdir>/evidence/diagnosis.json`（schema v4） |
| Builder 建站 | 基于合成示例实现或替换页面；用 `--data` 指向 DA 快照在本机起服务；核对渲染、失效与请求边界 | `<scenario-workdir>/plan.md`；`<scenario-workdir>/evidence/diagnosis.json`（仓库外路径） | 站点工作副本（仓库外，或仓库内仅限合成示例）；本机访问验证报告 `<scenario-workdir>/verification/`（仓库外） |

### 2.1 Boss（规划）

- 只定义"要算什么、算什么口径、什么算通过"，不替 DA 取数、不替 Builder 实现页面。
- 任务书必须写明：观察月份、数据截止、来源标注、schema 版本、指标标题与顺序、成本与折算人头的单位、哪些字段允许为 `null`（期间缺口）。
- 派工时同时给出隐私边界：只允许聚合口径，禁止个人、薪酬与编制粒度。
- 放行判定只依据交接工件与可复现命令，不依据执行者的口头或运行摘要式自述。

### 2.2 DA（数据分析）

- 证据包与快照的数值来源、期间与口径必须可回溯到任务书；无法回溯的数值不写入快照。
- 快照顶层字段沿用合成示例：`scope`、`conclusion`、`origin`、`metrics`、`insights`、`definitions`、`risks`、`recommendations`（`openbot/examples/org-health-local/data/diagnosis.json:1-98`）。
- 期间缺口用 `null` 表达，不填估算值；页面把 `null` 渲染为"未提供"（`openbot/examples/org-health-local/app.js:129`、`openbot/packages/i18n/src/messages/zh/orgHealthDemo.ts:52`）。
- 每条建议必须带依据、缺失证据和核实方式（`openbot/examples/org-health-local/data/diagnosis.json:89-98`）。

### 2.3 Builder（建站）

- 站点结构以合成示例为准：`openbot/examples/org-health-local/index.html` 页面骨架、`openbot/examples/org-health-local/app.js` 渲染与校验、`openbot/examples/org-health-local/styles.css` 样式、`openbot/examples/org-health-local/serve.py` 本机服务。
- 真实数据只通过 `--data /absolute/path/to/diagnosis.json` 注入（`openbot/examples/org-health-local/serve.py:53`、`openbot/examples/org-health-local/README.md:115`），不复制进站点目录。
- 验证报告须记录访问 URL、启动命令、端口、实际检查项与失败项，便于他人复现。

## 3. 交接工件清单与路径约定

### 3.1 工作目录约定

场景工件统一放在**仓库之外**的一个运行目录下，下表中的路径均为该目录内的相对路径：

```
<scenario-workdir>/            # 仓库外，例如 ~/phanes-runs/org-diagnosis/<run-id>/
  plan.md                      # Boss 任务书
  evidence/                    # DA 证据包
    diagnosis.json             # DA 产出的诊断快照（schema v4），Builder 直接消费
    <口径与来源说明>.md         # 口径、计算过程、来源引用、缺失证据
  site/                        # Builder 站点工作副本
  verification/                # Builder 本机访问验证报告 + 交叉审查记录
```

### 3.2 本地 e2e 临时目录产物

`openbot/examples/org-health-local/run_e2e.py` 使用 `--workdir` 指定的临时目录时，产物直接落在该目录根下：Boss 步骤生成 `brief.json`，DA 步骤读取它并生成 `diagnosis.json`；Builder 步骤生成站点目录 `dist/`（其中包含 `build-report.json`，对应上文的 site/report 产物）。启用审查时，Reviewer 步骤生成 `review.md`；编排器最后还会写入 `timings.json` 记录各步骤耗时。任一步骤失败时也应保留整个临时目录（包括已生成的产物和失败时写出的审查记录），供复查失败原因。

### 3.3 工件清单

| 工件 | 生产者 | 消费者 | 路径 | 能否入仓库 |
| --- | --- | --- | --- | --- |
| 场景任务书 | Boss | DA、Builder | `<scenario-workdir>/plan.md` | 否。含真实期间与口径，属敏感上下文 |
| 诊断快照 | DA | Builder、交叉审查者 | `<scenario-workdir>/evidence/diagnosis.json` | 否。真实诊断数据禁止入仓 |
| 证据包（口径、计算过程、来源引用、缺失证据） | DA | Boss、交叉审查者 | `<scenario-workdir>/evidence/` | 否 |
| 站点工作副本 | Builder | Builder | `<scenario-workdir>/site/` | 仅当内容全部为合成示例时，才可放回仓库的 `openbot/examples/` |
| 本机访问验证报告 | Builder | Boss、交叉审查者 | `<scenario-workdir>/verification/` | 否。含本机路径与运行细节；仓库内只留结论摘要 |
| 独立交叉审查记录 | 交叉审查者 | Boss | `<scenario-workdir>/verification/review.md` | 否 |
| 合成示例站点与合成快照 | 仓库维护者 | 全部角色 | `openbot/examples/org-health-local/` | 是。仅限人工构造数据（`openbot/examples/org-health-local/README.md:36-44`） |

### 3.3 路径与命名规则

1. `<scenario-workdir>` 必须位于仓库工作树之外；不得在仓库根目录或任何子目录下写入真实数据文件。
2. 仓库内唯一的诊断数据文件是合成示例 `openbot/examples/org-health-local/data/diagnosis.json`，且必须保持人工构造。
3. 快照文件名固定为 `diagnosis.json`（放在 `<scenario-workdir>/evidence/` 内）；顶层字段与 `metrics` 的标题顺序不得改动，否则页面判为结构错误（`openbot/examples/org-health-local/app.js:74-82`）。
4. 页面固定请求同源路由 `/data/diagnosis.json`（`openbot/examples/org-health-local/app.js:234`），真实文件位置由服务端的 `--data` 决定（`openbot/examples/org-health-local/serve.py:36-39`、`openbot/examples/org-health-local/serve.py:53`）。
5. 交接以"路径 + sha256"传递，不把真实数据正文粘进 Issue、PR、聊天记录或工单。

## 4. 交接流程与放行条件

1. Boss 写 `<scenario-workdir>/plan.md`，写明口径、期间、字段要求、隐私边界与验收清单。
2. DA 产出 `<scenario-workdir>/evidence/` 与其中的快照文件，标注事实、推断、边界与缺失证据。
3. Builder 用 `--data <scenario-workdir>/evidence/diagnosis.json` 起本机服务，按第 5 节逐项验证，写 `<scenario-workdir>/verification/`。
4. 独立交叉审查者复核快照、来源口径与 Builder 验证记录，写 `<scenario-workdir>/verification/review.md`。
5. Boss 按第 5 节验收清单判定"接受"或"退回"；任一项未通过即退回，不做部分接受。

## 5. 验收清单

对应 #6 的验收描述：数据口径、验证、隐私边界、独立交叉审查。

### 5.1 数据口径

- [ ] `scope` 四项齐全且与任务书一致：观察月份、数据截止、来源、schema 版本（`openbot/examples/org-health-local/data/diagnosis.json:2-6`）。
- [ ] 指标标题与顺序符合页面期望顺序，否则页面直接报错（`openbot/examples/org-health-local/app.js:74-82`）。
- [ ] 各期数值为有限数值或显式 `null`；`null` 只用于真实缺口，不用于未取数（`openbot/examples/org-health-local/app.js:109-127`）。
- [ ] 5 月缺少同口径整体成本与人头时保持 `null`，页面显示"未提供"并注明 5 月缺同口径值（`openbot/packages/i18n/src/messages/zh/orgHealthDemo.ts:28`、`openbot/packages/i18n/src/messages/zh/orgHealthDemo.ts:52`）。
- [ ] 成本单位、折算人头口径与成本率定义与任务书一致（`openbot/packages/i18n/src/messages/zh/orgHealthDemo.ts:30`、`openbot/packages/i18n/src/messages/zh/orgHealthDemo.ts:33`）。
- [ ] 事实、推断、边界分别标注，推断不写成事实（`openbot/packages/i18n/src/messages/zh/orgHealthDemo.ts:81-84`）。
- [ ] 缺失领域显式列为证据缺口，不以成本或人数代替（`openbot/packages/i18n/src/messages/zh/orgHealthDemo.ts:48`）。
- [ ] 每条建议带依据、缺失证据与核实方式（`openbot/examples/org-health-local/data/diagnosis.json:89-98`）。

### 5.2 验证

- [ ] 仓库内合成示例的回归脚本 `bun run test:org-health-demo` 通过（`openbot/package.json:194`），覆盖中英文与回退语言、无效数据、无效 Host 与私有路径（`openbot/scripts/org-health-demo-e2e.ts:107`、`openbot/scripts/org-health-demo-e2e.ts:189-193`）。
- [ ] 从仓库根目录运行 `python3 openbot/examples/org-health-local/run_tests.py`，执行全量 Python 单测，覆盖 Boss、DA、Builder、Reviewer、服务与编排器测试（`openbot/examples/org-health-local/run_tests.py:9-22`）。
- [ ] 站点在本机可访问：服务只绑定回环地址 `127.0.0.1`（`openbot/examples/org-health-local/serve.py:87`），启动后打印预览地址、站点根目录、诊断数据文件与路径白名单（`openbot/examples/org-health-local/serve.py:94-100`）。
- [ ] 端口被占用时打印明确错误并以非 0 退出，不静默改用其他端口（`openbot/examples/org-health-local/serve.py:74-81`、`openbot/examples/org-health-local/serve.py:105-121`）。
- [ ] 非 `127.0.0.1:<port>` / `localhost:<port>` 的 Host 头被拒（403），白名单外路径返回 404（`openbot/examples/org-health-local/serve.py:26-34`）。
- [ ] 目录列表被关闭（`openbot/examples/org-health-local/serve.py:41-43`）；请求日志不落盘（`openbot/examples/org-health-local/serve.py:45-47`）。
- [ ] 验证报告记录访问 URL、启动命令、端口、逐项检查结果与失败项，可被第三方复现。

### 5.3 隐私边界

- [ ] 真实诊断数据只存在于 `<scenario-workdir>`；仓库、Issue、PR 内只有合成示例与脱敏摘要。
- [ ] 快照只含聚合口径，不含人名、薪酬明细、编制明细等个人级字段。
- [ ] 页面文案不作个人、薪酬或编制判断（`openbot/packages/i18n/src/messages/zh/orgHealthDemo.ts:21`）。
- [ ] 页面无第三方脚本与外链，内容安全策略为 `default-src 'self'`（`openbot/examples/org-health-local/index.html:7`、`openbot/examples/org-health-local/index.html:10`）。
- [ ] 页面不发送网络请求，数据来自本机同源路由（`openbot/examples/org-health-local/app.js:234`、`openbot/packages/i18n/src/messages/zh/orgHealthDemo.ts:51`）。

### 5.4 独立交叉审查

- [ ] 交叉审查者既不是证据产出者也不是站点实现者，独立复核快照与来源口径。
- [ ] 审查记录写明复核项、依据来源、结论与未决问题，落盘 `<scenario-workdir>/verification/review.md`。
- [ ] 放行结论引用独立复核记录与可复现命令，不以执行者自述为依据；最终验收由控制面做出并引用独立验证（`README.md:35`）。
- [ ] 审查发现的越界数据（个人级、越权来源、口径不符）按退回处理，不做"页面隐藏"式处理。

## 6. 现有合成示例引用

| 路径 | 行号 | 内容 |
| --- | --- | --- |
| `openbot/examples/org-health-local/README.md` | 19-29 | 合成示例常用校验、预览与端到端命令 |
| `openbot/examples/org-health-local/README.md` | 36-44 | Boss → DA → Builder → Review 的合成交接链 |
| `openbot/examples/org-health-local/README.md` | 115-117 | 真实数据必须存仓库外、`--data` 用法与站点路径边界 |
| `openbot/examples/org-health-local/serve.py` | 26-34 | Host 校验 403 与路径白名单 404 |
| `openbot/examples/org-health-local/serve.py` | 36-39 | `/data/diagnosis.json` 到真实数据文件的映射 |
| `openbot/examples/org-health-local/serve.py` | 41-47 | 关闭目录列表、不写请求日志 |
| `openbot/examples/org-health-local/serve.py` | 49-66 | `--port` / `--data` 参数与端口预检 |
| `openbot/examples/org-health-local/serve.py` | 83-100 | 构建前端、回环监听与预览地址、路径信息输出 |
| `openbot/examples/org-health-local/app.js` | 23-56 | 旧版结构兼容与结构校验 |
| `openbot/examples/org-health-local/app.js` | 73-82 | schema v4、观察月份与指标顺序校验 |
| `openbot/examples/org-health-local/app.js` | 109-127 | 期次与数值格式校验，非法即判失败 |
| `openbot/examples/org-health-local/app.js` | 234 | 读取本机 `./data/diagnosis.json` |
| `openbot/examples/org-health-local/index.html` | 7 | 内容安全策略 `default-src 'self'` |
| `openbot/examples/org-health-local/index.html` | 50 | 指标卡容器 `#metric-grid` |
| `openbot/examples/org-health-local/data/diagnosis.json` | 1-9 | `scope` 四项、`conclusion`、`origin` |
| `openbot/examples/org-health-local/data/diagnosis.json` | 10-74 | 四组指标的期间值与 `null` 缺口 |
| `openbot/examples/org-health-local/data/diagnosis.json` | 75-98 | `insights`、`definitions`、`risks`、`recommendations` |
| `openbot/scripts/org-health-demo-e2e.ts` | 16 | 以临时目录中的副本启动隔离服务 |
| `openbot/scripts/org-health-demo-e2e.ts` | 189-193 | 无效 Host 403 与私有路径 404 断言 |
| `openbot/package.json` | 194 | `test:org-health-demo` 脚本定义 |
| `openbot/packages/i18n/src/messages/zh/orgHealthDemo.ts` | 15 | 历史快照提示，不等于当前组织结论 |
| `openbot/packages/i18n/src/messages/zh/orgHealthDemo.ts` | 21 | 仅聚合信号，不作个人、薪酬或编制判断 |
| `openbot/packages/i18n/src/messages/zh/orgHealthDemo.ts` | 28 | 5 月缺同口径值、年末值非已发生实际 |
| `openbot/packages/i18n/src/messages/zh/orgHealthDemo.ts` | 30 | 成本单位与折算人头口径 |
| `openbot/packages/i18n/src/messages/zh/orgHealthDemo.ts` | 33 | 技术人力成本率定义 |
| `openbot/packages/i18n/src/messages/zh/orgHealthDemo.ts` | 48 | 缺失领域显示为缺失，不以成本或人数代替 |
| `openbot/packages/i18n/src/messages/zh/orgHealthDemo.ts` | 51 | 本机静态数据，不发送网络请求 |
| `openbot/packages/i18n/src/messages/zh/orgHealthDemo.ts` | 59-60 | 结构或数值格式变化的失败提示 |

## 7. 未决事项

- DA 证据包与 Builder 本机访问验证报告的具体模板由对应角色任务定义，本文只固定路径与必填项。
- 真实数据接入流程尚未演练；`openbot/examples/org-health-local/README.md:115-117` 只给出 `--data` 用法，实际演练前需先确认期间、指标定义与结构一致。
- 站点样式与页面文案的最终口径由 Builder 按任务书落定，本文不预先固定视觉稿。
