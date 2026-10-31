# 本机组织健康诊断示例

仅展示人工构造的示例数据，不包含真实组织数据。在已安装仓库依赖和 Bun 的环境中运行 `python3 serve.py`；服务启动时编译共享翻译目录，在本机访问 http://127.0.0.1:8765/。默认中文，使用 `?lang=en` 查看英文界面；其他未翻译语言回退英文，诊断数据是用户内容，保持原文。如端口已被占用，先停止其他示例服务。

页面使用固定的 2026 年示例口径；实际诊断数据必须保存在仓库外，替换使用前需核对期间、指标定义和结构。使用 `python3 serve.py --data /absolute/path/to/diagnosis.json` 读取该文件，不要将真实数据复制到仓库或提交。

页面可直接用浏览器打印为 PDF。

仓库根目录运行 `bun run test:org-health-demo` 可启动隔离服务，以浏览器检查中英文、回退语言、无效数据和请求边界，并将结果写入 `.openbot-build/`。

示例目录内运行 `python3 -m unittest test_serve -v` 可验证本机服务边界（仅标准库，不调用 Bun）：loopback Host 白名单、路径白名单、目录与穿越请求拒绝，以及 `--data` 指定的数据文件。

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
```

校验失败时退出码非 0，并逐条打印出错路径，例如 `/scope/schema版本`。单元测试：

```bash
cd openbot/examples/org-health-local && python3 -m unittest test_validate -v
```
