# 本机组织健康诊断示例

仅展示人工构造的示例数据，不包含真实组织数据。在已安装仓库依赖和 Bun 的环境中运行 `python3 serve.py`；服务启动时编译共享翻译目录，在本机访问 http://127.0.0.1:8765/。默认中文，使用 `?lang=en` 查看英文界面；其他未翻译语言回退英文，诊断数据是用户内容，保持原文。如端口已被占用，先停止其他示例服务。

页面使用固定的 2026 年示例口径；实际诊断数据必须保存在仓库外，替换使用前需核对期间、指标定义和结构。使用 `python3 serve.py --data /absolute/path/to/diagnosis.json` 读取该文件，不要将真实数据复制到仓库或提交。

若需直接预览由 Builder 产出的站点目录，无需现场打包，可使用 `python3 serve.py --root /path/to/dist`。指定 `--root` 时仅从该目录服务白名单文件（`/`、`/index.html`、`/app.bundle.js`、`/styles.css`、`/data/diagnosis.json`），不再调用 Bun 打包；`/data/diagnosis.json` 默认读取 `<root>/data/diagnosis.json`，亦可使用 `--data` 单独覆盖。

页面可直接用浏览器打印为 PDF。

仓库根目录运行 `bun run test:org-health-demo` 可启动隔离服务，以浏览器检查中英文、回退语言、无效数据和请求边界，并将结果写入 `.openbot-build/`。

示例目录内运行 `python3 -m unittest test_serve -v` 可验证本机服务边界（仅标准库，不调用 Bun）：loopback Host 白名单、路径白名单、目录与穿越请求拒绝、`--root` 产物目录服务，以及 `--data` 指定的数据文件。

## DA 角色：合成原始数据 → 诊断快照

`da/` 是诊断流水线里的 DA 角色：按任务书（brief）的观察期间与口径，把原始 CSV 归并成 schema v4 诊断快照。

```bash
cd openbot/examples/org-health-local
python3 da/build_diagnosis.py --brief <brief.json> --raw da/sample-raw --out <diagnosis.json>
python3 validate.py <diagnosis.json>
```

- 任务书至少给出 `观察期间`（起始期次、参考期次、截止期次、数据截止）与 `口径`（各指标定义和汇率）；可选 `目标`、`必答问题`、`数据需求`、`禁止事项`、`schema版本`（=4）。
- 原始数据目录放四张 CSV：`cost.csv`（月度技术人力成本与运维设施成本）、`heads.csv`（月度折算人头）、`revenue.csv`（月度收入）、`forecast.csv`（年末测算与年末目标）。`da/sample-raw/` 是纯合成样本，数值与 `data/diagnosis.json` 同量级。
- 缺列、缺文件、缺汇率时退出码非 0；缺数据的期次写 `null`，不填估算值。输出确定性：同一份任务书与原始数据得到同一份 JSON，键序固定。
- 单元测试：`cd openbot/examples/org-health-local && python3 -m unittest da.test_build_diagnosis -v`。

## 结构定义与校验

`schema/diagnosis.schema.json` 是 `data/diagnosis.json` 的结构定义（JSON Schema draft 2020-12）。字段与 `app.js` 读取的字段一致：`scope` 给出口径和 `schema版本`（固定为 4），`metrics` 按页面顺序给出四个指标，数值区分数值序列（数值或 `null`）与比率序列（数值、`null` 或字符串）。`口径说明` 是可选字段：缺省不报错，出现时必须是非空字符串数组。定义只覆盖当前格式；`app.js` 仍兼容的旧中文键格式不在定义内。

`validate.py` 只用标准库，按该定义校验数据文件：

```bash
cd openbot/examples/org-health-local
python3 validate.py                            # 校验 data/diagnosis.json
python3 validate.py /absolute/path/to/diagnosis.json
python3 validate.py --json data/diagnosis.json  # 输出机器可读的 JSON 结果
```

校验失败时退出码非 0，并逐条打印出错路径，例如 `/scope/schema版本`。单元测试：

```bash
cd openbot/examples/org-health-local && python3 -m unittest test_validate -v
```

## Review 角色：独立交叉审查

`review/` 是独立交叉审查角色：核对任务书（brief）、诊断快照（diagnosis）与站点产物（dist）三方一致性，逐条输出 PASS/FAIL，任一 FAIL 时退出码非 0。

```bash
cd openbot/examples/org-health-local
python3 review/cross_check.py --brief <brief.json> --data <diagnosis.json> [--dist <dist>]
```

- 期间一致：任务书 `观察期间`（对象、字符串或数组）里的期次必须覆盖 `scope.观察月份`；显式的 `截止期次`/`数据截止` 必须与 `scope` 相同。
- 版本一致：任务书 `schema版本` 与 `scope.schema版本` 相同且为 4。
- 必答问题覆盖：任务书 `必答问题` 每条都要在快照的 `insights`/`recommendations` 里被引用一次；命中 id、原文或提取出的关键词任一即算被引用（关键词规则见脚本文档字符串）。
- 站点数据一致：给出 `--dist` 时 `dist/data/diagnosis.json` 与 `--data` 字节相同，并打印 sha256 前 12 位。
- 隐私边界：快照的字段名（含嵌套）不含个人/薪酬/编制粒度黑名单词条；黑名单写在脚本里。

单元测试只用测试内构造的最小样本，不依赖其他角色的脚本：

```bash
cd openbot/examples/org-health-local && python3 -m unittest review.test_cross_check -v
```

## 构建静态站点

`builder/build_site.py` 把诊断快照构建成可预览的静态目录：先用 `validate.py` 校验数据，
不合格时退出码非 0 且不生成 `dist`；合格时复制 `index.html`、`styles.css` 与数据到
`dist/data/diagnosis.json`，并写出 `build-report.json`（输入路径、输入 sha256、文件清单、生成时间）。

```bash
cd openbot/examples/org-health-local
python3 builder/build_site.py --data data/diagnosis.json --out dist --bundle app.bundle.js
```

产物为 `dist/index.html`、`dist/app.bundle.js`、`dist/styles.css`、`dist/data/diagnosis.json`
和 `dist/build-report.json`。不带 `--bundle` 时用与 `serve.py` 相同的 bun 命令打包（需要本机安装 Bun）；
单元测试一律传入假的 bundle 文件，不依赖 Bun 与网络：

```bash
cd openbot/examples/org-health-local && python3 -m unittest builder.test_build_site -v
```
