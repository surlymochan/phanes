# 组织诊断场景独立审查清单

本清单供独立 Reviewer 在 Boss、DA、Builder 交付后使用。审查者不得是证据包产出者或站点实现者。运行工件按 `docs/scenario/org-diagnosis/README.md` 的契约存放在仓库外 `<scenario-workdir>`；本清单只规定检查方法，不要求把真实数据复制进仓库、Issue 或审查记录。

## 审查输入

- Boss 任务书：`<scenario-workdir>/plan.md`；三阶段流水线示例另有 `docs/scenario/org-diagnosis/out/boss-brief.json`。
- DA 快照：`<scenario-workdir>/evidence/diagnosis.json`；证据包及口径、计算、来源和缺失说明：`<scenario-workdir>/evidence/`。如按流水线产出，仓库内方法摘要为 `docs/scenario/org-diagnosis/out/da-method.md`。
- Builder 站点副本：`<scenario-workdir>/site/`；验证报告：`<scenario-workdir>/verification/`。三阶段流水线的实现与摘要位置见 `docs/scenario/org-diagnosis/out/builder-notes.md`。
- 合成示例的结构基准：`openbot/examples/org-health-local/data/diagnosis.json`、`schema/diagnosis.schema.json`；页面校验见 `app.js`。

## 逐项检查

每项记录通过/不通过/不适用及证据路径。不得将真实数值粘贴到仓库内的审查结论；使用路径、字段名、行号或 JSONPath，必要时记录脱敏摘要。

### Boss 任务书

| 检查项 | 看哪个文件/字段 | 通过条件 |
| --- | --- | --- |
| 目标 | `plan.md` 的“目标”；流水线产物 `boss-brief.json` 的 `目标` | 明确要回答的组织诊断目标，范围与聚合健康信号相符；没有个人、薪酬或编制判断目标。 |
| 观察期间 | `plan.md` 的“观察期间”；`boss-brief.json` 同名字段 | 写明观察月份/期间及数据截止日期，期间边界明确且 DA、Builder 后续材料一致。 |
| 口径 | `plan.md` 的“口径”；`boss-brief.json` 同名字段 | 每项指标有定义、单位、计算/折算方式和口径版本；成本、人头及成本率定义可复核，不留相互矛盾的口径。 |
| 必答问题 | `plan.md` 的“必答问题”；`boss-brief.json` 同名字段 | 有明确、逐条可回答的问题；后续 DA 结论、洞察或显式缺口能对应每个问题。 |
| 数据需求 | `plan.md` 的“数据需求”；`boss-brief.json` 同名字段 | 列出回答问题所需数据、来源/字段和期间；来源获授权且粒度足以按约定口径计算。 |
| 禁止事项 | `plan.md` 的“禁止事项”；`boss-brief.json` 同名字段 | 明确禁止个人级、薪酬明细、编制明细及未经授权数据；明确真实数据不得进仓库、Issue、PR、聊天或工单。 |

### DA 快照与证据

| 检查项 | 看哪个文件/字段 | 通过条件 |
| --- | --- | --- |
| Schema v4 | `<scenario-workdir>/evidence/diagnosis.json` 的 `scope.schema_version`、顶层字段、`metrics`；对照 `openbot/examples/org-health-local/schema/diagnosis.schema.json` 和 `app.js` | 明确为 schema v4，结构和指标标题/顺序满足页面校验；可复现执行 `python3 openbot/examples/org-health-local/validate.py <快照路径>` 并通过（快照路径是位置参数）。 |
| 任务书对应 | `diagnosis.json` 的 `scope`、`metrics`、`definitions`、`insights`；`evidence/` 中口径说明及 `da-method.md` | 观察期间、截止、来源、单位和定义与任务书一致；每个结论/指标均能在证据说明中追到来源字段和计算方法。 |
| null 含义 | `diagnosis.json` 中为 `null` 的指标/期间及对应证据说明 | `null` 仅表示真实期间缺口，说明具体缺什么；没有把未取数、未知或未验证伪装成缺口值，也没有以估算值填补；页面显示“未提供”。 |
| 合成与真实数据区分 | 快照 `origin`、`scope`、证据来源；合成基准 `openbot/examples/org-health-local/data/diagnosis.json` | 实际交付快照来自授权真实来源时如实标明；合成示例清楚标为合成，仅用于流程/页面验证，绝不冒充真实诊断或支持真实结论。真实快照正文留在仓库外。 |
| 事实、推断与边界 | 快照 `conclusion`、`insights`、`risks`、`recommendations` 及来源说明 | 事实有来源，推断有标记且不过度声称因果，边界/缺失证据明确；建议带依据、缺失证据和核实方式。 |
| 数据最小化 | 快照所有字段及来源映射 | 只含必要的聚合字段；无姓名、薪酬明细、个人记录或编制明细；没有为避开检查而把这些信息塞进自由文本。 |

### Builder 站点与验证

| 检查项 | 看哪个文件/字段 | 通过条件 |
| --- | --- | --- |
| 只读本机数据 | 站点源码中的数据加载路径、Builder 启动命令和验证报告；参考 `serve.py --data` | 站点消费本机注入的诊断 JSON（同源 `/data/diagnosis.json`），不修改快照、不额外读取其他数据；真实数据路径位于仓库外。 |
| 无外链/无网络请求 | 站点 HTML 的脚本、样式和链接；浏览器网络记录/验证报告 | 不加载第三方脚本、字体、图片或外部链接；无外发请求，CSP 限制为 `default-src 'self'`。 |
| 页面加载与空值 | 本机验证报告中的 URL、启动命令、端口、检查项；渲染结果 | 在回环地址可访问；正确呈现指标、期间、定义及 `null` 的“未提供”状态；非法 schema 或指标顺序应明确失败，不静默降级。 |
| 构建产物完整性 | Builder 工作副本中的 `dist/build-report.json`（或报告所列产物目录）及每个被列文件 | 报告列出构建文件及 SHA-256；对每个文件重算 SHA-256 与报告一致，无缺项或额外未说明产物。可用已合入的 `openbot/examples/org-health-local/builder/check_site.py` 检查（#76）。 |
| 验证记录可复现 | `<scenario-workdir>/verification/` 下的报告或 `builder-notes.md` | 记录站点路径、启动命令、URL、端口、数据快照路径/sha256（不含正文）、逐项结果和失败项；他人可在本机按记录复查。 |
| 隐私边界 | 站点目录、报告、仓库 diff 与版本控制状态 | 真实数据只在仓库外；仓库变更、截图与报告不含真实诊断数值或敏感字段。 |

## 独立复核及脚本状态

审查者应独立检查 Boss、DA、Builder 的输入输出，且不得以 Builder 自述或脚本退出码替代对证据与口径的判断。`validate.py`、`test_serve.py` 和 `review/cross_check.py` 均已在仓库内。使用时仍须按实际版本确认行为与验证记录：`validate.py` 以位置参数接收快照，退出码 `0`/`1`/`2` 分别表示通过/校验失败/输入不可读；`test_serve.py` 以 unittest 运行，退出码 `0` 表示通过，`1` 表示测试失败；`cross_check.py` 使用 `--brief <brief.json> --data <diagnosis.json> [--dist <dist>] [--out <report.md>]`，逐项输出 `PASS`/`FAIL`，退出码 `0` 表示全部通过，`1` 表示至少一项失败，`2` 表示输入不可读或不是 JSON 对象。三者都不改变真实组织数据必须留在仓库外的边界。`builder/check_site.py` 已合入（#76）。

## 审查结论模板

```text
审查者（独立于 DA/Builder）：
审查日期：
运行标识（不得包含真实组织数据）：
审查工件路径：

结论：通过 / 有条件通过 / 不通过

检查项及证据路径：
- [项目] [通过/未通过/不适用] — [文件路径 + 字段/JSONPath/行号；必要时命令]

未决问题/条件（有条件通过时列明责任人与复核条件）：

不通过项及退回要求：

复现命令与结果：
```

“通过”表示所有适用项均通过；“有条件通过”须列出可验证的条件、责任人及复核期限，不得用于掩盖隐私或口径越界；“不通过”用于任一关键项失败、证据无法追溯或出现禁止数据。审查记录存于 `<scenario-workdir>/verification/review.md`，仅记录证据路径和脱敏结论。
