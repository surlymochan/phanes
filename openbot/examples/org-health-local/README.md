# 本机组织健康诊断示例

仅展示人工构造的示例数据，不包含真实组织数据。在已安装仓库依赖和 Bun 的环境中运行 `python3 serve.py`；服务启动时编译共享翻译目录，在本机访问 http://127.0.0.1:8765/。默认中文，使用 `?lang=en` 查看英文界面；其他未翻译语言回退英文，诊断数据是用户内容，保持原文。如端口已被占用，先停止其他示例服务。

页面使用固定的 2026 年示例口径；实际诊断数据必须保存在仓库外，替换使用前需核对期间、指标定义和结构。使用 `python3 serve.py --data /absolute/path/to/diagnosis.json` 读取该文件，不要将真实数据复制到仓库或提交。

仓库根目录运行 `bun run test:org-health-demo` 可启动隔离服务，以浏览器检查中英文、回退语言、无效数据和请求边界，并将结果写入 `.openbot-build/`。

示例目录内运行 `python3 -m unittest test_serve -v` 可验证本机服务边界（仅标准库，不调用 Bun）：loopback Host 白名单、路径白名单、目录与穿越请求拒绝，以及 `--data` 指定的数据文件。

## 结构定义与校验

`schema/diagnosis.schema.json` 是 `data/diagnosis.json` 的结构定义（JSON Schema draft 2020-12）。字段与 `app.js` 读取的字段一致：`scope` 给出口径和 `schema版本`（固定为 4），可选字段 `口径说明`（字符串数组）提供页面数据口径说明折叠面板，`metrics` 按页面顺序给出四个指标，数值区分数值序列（数值或 `null`）与比率序列（数值、`null` 或字符串）。定义只覆盖当前格式；`app.js` 仍兼容的旧中文键格式不在定义内。

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
