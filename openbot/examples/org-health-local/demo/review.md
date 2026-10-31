# 三棒产物独立审查

审查者：独立 Reviewer（本次仅审阅仓库内 demo 产物）  
审查日期：2026-10-31  
运行标识：org-health-local 合成 demo  
审查工件路径：`openbot/examples/org-health-local/demo/`

## 结论：不通过

Builder 交付目录缺少可运行的站点文件，构建完整性检查失败；没有浏览器验证记录。另有口径证据与任务书对应关系需要修正后复核。

## 检查项及证据路径

### Boss 任务书

- 目标：通过 — `demo/brief.json`，`目标`；`demo/boss-notes.md:8`。目标为聚合组织诊断，并明确是合成演示。
- 观察期间：通过 — `demo/brief.json`，`观察期间`；`demo/diagnosis.json`，`scope.观察月份`、`scope.数据截止`；`cross_check.py` 期间一致检查通过。
- 口径：不通过 — `demo/brief.json`，`口径`、`schema版本`；`demo/diagnosis.json`，`口径说明`。任务书口径未明确口径版本；快照口径说明使用“未统计、未提供或无同口径对比数据”合并 null 成因，未逐项证明缺失属于真实期间缺口。交叉脚本不检查这些内容。
- 必答问题：通过 — `demo/brief.json`，`必答问题`；`demo/da-notes.md`“问题 1–6”；交叉脚本 6 项覆盖均通过。
- 数据需求：不通过 — `demo/brief.json`，`数据需求`。列出逻辑字段与单位，但未在任务书提供来源授权证据；合成样本文件存在于 `da/sample-raw/`，不等同于实际来源授权证明。
- 禁止事项：通过 — `demo/brief.json`，`禁止事项`；`demo/boss-notes.md:23-30`。明确禁止个人、薪酬、编制明细及真实数据入库。

### DA 快照与证据

- Schema v4：通过 — `demo/diagnosis.json`，`scope.schema版本`、`metrics`；对照 `schema/diagnosis.schema.json`、`app.js:94-107`。命令 `python3 openbot/examples/org-health-local/validate.py openbot/examples/org-health-local/demo/diagnosis.json` 输出“校验通过”。
- 任务书对应：不通过 — `demo/diagnosis.json`，`scope`、`metrics`、`definitions`、`口径说明`；`demo/da-notes.md`“已知问题”。快照日期与任务书一致，但没有逐个指标到原始字段/计算的可审计映射；DA 说明记录 `write_method.py` 失败，且以其自述替代了 `da-method.md` 或同等方法记录。
- null 含义：不通过 — `demo/diagnosis.json`，`口径说明`、各指标 `数值`；`demo/da-notes.md`“缺失处理”。材料把 null 描述为未统计、未提供或无同口径对比，三者含义不等同；无法据此确认每个 null 都只代表真实期间缺口。
- 合成与真实数据区分：通过 — `demo/diagnosis.json`，`origin`、`scope.来源`；`demo/da-notes.md:1-6`；`da/sample-raw/`。产物明确标为合成示例，不冒充真实诊断。
- 事实、推断与边界：不通过 — `demo/diagnosis.json`，`conclusion`、`insights`、`risks`、`recommendations`；`demo/da-notes.md`问题 1–6。缺失项有说明，但材料声称“完全同向”“口径完全一致”，没有独立来源映射支持这些判断；部分推断依据无法复核。
- 数据最小化：通过 — `demo/diagnosis.json`；`cross_check.py` 隐私边界检查通过，未发现禁用字段名。检查只覆盖字段名，无法证明自由文本无敏感信息；本快照标记为合成数据。

### Builder 站点与验证

- 只读本机数据：不适用 — `demo/README.md:1`、`builder/build_site.py`。目录未包含源码副本或验证记录，无法审查实际交付站点的数据加载行为。
- 无外链/无网络请求：不适用 — `index.html:7-10` 的源模板使用自限 CSP、同目录资源；但 `demo/site/index.html` 缺失，且没有浏览器网络记录，不能据模板确认实际交付站点行为。
- 页面加载与空值：不通过 — `demo/README.md:1`；缺少 `demo/site/index.html`、`app.bundle.js`、`styles.css`、`data/diagnosis.json`，没有 URL、启动命令、端口或渲染验证记录。无法确认页面加载及 null 呈现。
- 构建产物完整性：不通过 — `demo/site/build-report.json:1-26` 列出四个产物及 SHA-256；对目录运行 `python3 openbot/examples/org-health-local/builder/check_site.py openbot/examples/org-health-local/demo/site`，四个文件均缺失，检查失败。
- 验证记录可复现：不通过 — `demo/site/build-report.json` 只有构建文件清单与生成时间；`demo/README.md:1` 要求先重建再预览。没有验证目录或 Builder 浏览器验证报告。
- 隐私边界：通过 — `demo/diagnosis.json` 标记合成来源；`git status` 显示开始审查时工作树干净，当前仅新增本审查文件。未发现真实诊断快照或截图交付。

## 复核脚本输出

命令：

```text
python3 openbot/examples/org-health-local/review/cross_check.py --brief openbot/examples/org-health-local/demo/brief.json --data openbot/examples/org-health-local/demo/diagnosis.json --dist openbot/examples/org-health-local/demo/site
```

输出摘要：期间一致、版本一致、6 项必答问题覆盖、隐私边界通过；站点数据一致失败，原因是缺少 `demo/site/data/diagnosis.json`。共 10 项检查：9 项通过，1 项失败（退出码 1）。

另运行快照校验，结果为 `demo/diagnosis.json: 校验通过`；运行 `builder/check_site.py`，结果为报告所列 4 个站点文件均不存在，站点检查失败（5 项）。未运行浏览器验证，因为交付目录缺少可运行站点。

## 未决问题与退回要求

- Builder：按记录重建完整 `demo/site/`，再提交可复现的本机验证记录（启动命令、URL、端口、渲染与 null 检查、网络检查）。重新核对报告哈希。
- DA：补齐指标到原始字段及计算方法的证据映射；逐项说明 null 原因，并统一任务书与快照的缺失口径；复核并佐证洞察中的同向性与口径一致性判断。
- Boss/DA：为数据需求补充可核验的授权来源信息，或明确本交付仅为合成演示，不声称真实来源已授权。
