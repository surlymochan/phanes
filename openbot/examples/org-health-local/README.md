# 本机组织健康诊断示例

<!--
diagnosis.json 顶层字段一览（以 schema/diagnosis.schema.json 为准）：
- scope：数据范围与口径元数据，包含数据截止、观察月份、来源、schema版本（当前为 4）。
- 口径说明：可选的期间、单位及 null 含义说明列表。
- conclusion：诊断结论。
- origin：数据来源标识。
- metrics：按页面顺序排列的指标数据。
- insights：洞察条目列表。
- definitions：指标定义列表。
- risks：风险条目列表。
- recommendations：建议条目列表。
- stage：可选的流程阶段信息。
-->

## 常用命令

在本目录（`openbot/examples/org-health-local`）可直接执行以下三条常用命令：

```bash
# 校验诊断快照数据
python3 validate.py

# 启动本地预览服务（默认端口 8765；未安装 Bun 时可用 --root demo/site）
python3 serve.py

# 运行完整端到端流水线并进行交叉审查
python3 run_e2e.py --workdir demo --review
```

本地 e2e 非零退出时，先查看工作目录中的步骤输出和 `timings.json`，再按其中标出的失败角色定位问题。

## 三 Bot demo：一分钟查看

`demo/` 是三棒（Boss → DA → Builder）加独立审查在本目录用合成样本实跑出来的产物，按
`demo/brief.json` → `demo/diagnosis.json` → `demo/site/` → `demo/review.md` 依次传递。

| 环节 | 产物 | 内容 |
| --- | --- | --- |
| Boss | `demo/brief.json` | 任务书：目标、观察期间、口径、必答问题、禁止事项 |
| DA | `demo/diagnosis.json` | 诊断快照（schema v4），输入是 `demo/brief.json` 与 `da/sample-raw/` 的合成 CSV |
| Builder | `demo/site/` | 静态站点：`index.html`、`app.bundle.js`、`styles.css`、`data/diagnosis.json`、`build-report.json` |
| Review | `demo/review.md` | 交叉审查报告，`review/cross_check.py` 逐条 PASS/FAIL；本地跑 `run_e2e.py --review` 时写出，仓库已提交该报告（当前结论：不通过） |

各环节的产出说明另见 `demo/boss-notes.md` 与 `demo/da-notes.md`。

预览（两条命令，都不需要 Bun）：

```bash
cd openbot/examples/org-health-local
python3 builder/build_site.py --data demo/diagnosis.json --out demo/site --bundle demo/vendor/app.bundle.js
python3 serve.py --root demo/site
```

服务启动后在浏览器打开 http://127.0.0.1:8765/ 。仓库里 `demo/site/` 只提交了 `build-report.json`
（站点产物里的 `app.bundle.js` 约 7.5 MB，不入库；预构建的浏览器包在 `demo/vendor/app.bundle.js`），
所以第一次预览要先跑上面第一条命令重建站点。

不打开浏览器也能核对页面会展示什么：`demo/make_site_summary.py` 把 demo 站点的展示内容整理成
`demo/site-summary.md`，只用 Python 标准库，只读站点产物，不改动 `demo/site/`：

```bash
cd openbot/examples/org-health-local
python3 demo/make_site_summary.py
```

脚本只读 `demo/site/index.html` 与 `demo/site/data/diagnosis.json`；站点还没构建时这两个文件不存在，
脚本退出码为 3 并提示先跑上面第一条 Builder 命令，重建站点后重跑本命令即可刷新摘要。

`demo/site-summary.md` 的内容边界：只汇总页面结构、口径与条目数量——指标卡片的标题、单位与各期次
显示值、口径说明条目、洞察/建议/证据缺口/来源口径的条数，以及页面字段与快照字段的对应关系；
数值全部来自仓库内的合成样本，不含真实组织数据。它也不替代诊断结论或审查结论：结论仍以
`demo/diagnosis.json` 的 `conclusion` 与 `demo/review.md` 为准。

重新生成整条产物链：

```bash
cd openbot/examples/org-health-local
python3 run_e2e.py --workdir demo --review
```

`run_e2e.py` 依次跑 Boss → DA → Builder → 站点自检 → 交叉审查，产物写到 `--workdir`：
`brief.json`、`diagnosis.json`、`dist/`（站点）、`review.md`。两点注意：

- 站点落在 `demo/dist/`，`demo/site/` 仍由上面那条 Builder 命令产出；
- 该命令用 `boss/make_brief.py` 的内置默认目标重建任务书，会覆盖 `demo/brief.json` 与
  `demo/diagnosis.json`；仓库里的这两个文件按 `demo/boss-notes.md` 记的 `--goal` 参数生成，
  与重建结果不同（快照的「口径说明」随之不同）。

未指定 `--bundle` 时，若 `demo/vendor/app.bundle.js` 存在，`run_e2e.py` 会自动复用它；否则
Builder 会调用 Bun 打包，因此无可用预构建包时依赖 Bun。显式指定预构建 bundle（例如
`--bundle demo/vendor/app.bundle.js`）则不需要 Bun。

以下章节按角色逐个说明本示例的脚本与产物。

## 自检

在本目录执行以下命令，可一键运行全部单元测试，或分别运行各模块的单元测试：

```bash
python3 run_tests.py
python3 -m unittest discover -s . -p 'test_*.py'
python3 -m unittest discover -s boss -p 'test_*.py'
python3 -m unittest discover -s da -p 'test_*.py'
python3 -m unittest discover -s builder -p 'test_*.py'
python3 -m unittest discover -s review -p 'test_*.py'
python3 -m unittest discover -s demo -p 'test_*.py'
```

仅展示人工构造的示例数据，不包含真实组织数据。在已安装仓库依赖和 Bun 的环境中运行 `python3 serve.py`；服务启动时编译共享翻译目录，在本机访问 http://127.0.0.1:8765/。启动后终端打印可点击的预览地址与当前服务信息，例如 `预览地址: http://127.0.0.1:8765/`，并给出站点根目录、诊断数据文件和路径白名单（`--root`/`--data` 指定的路径按实际值打印）。默认中文，使用 `?lang=en` 查看英文界面；其他未翻译语言回退英文，诊断数据是用户内容，保持原文。默认端口 8765，可用 `--port` 指定；端口已被占用时打印明确错误并以非 0 退出，不会静默改用其他端口，请先停止其他示例服务。

未安装 Bun 时，未指定 `--root` 的启动会在 stderr 打印「未找到 bun 命令，请先安装 bun 或使用 --root 指向已构建好的静态站点」提示并以退出码 1 结束，不打印 traceback。

页面使用固定的 2026 年示例口径；实际诊断数据必须保存在仓库外，替换使用前需核对期间、指标定义和结构。使用 `python3 serve.py --data /absolute/path/to/diagnosis.json` 读取该文件，不要将真实数据复制到仓库或提交。

若需直接预览由 Builder 产出的站点目录，无需现场打包，可使用 `python3 serve.py --root /path/to/dist`。指定 `--root` 时仅从该目录服务白名单文件（`/`、`/index.html`、`/app.bundle.js`、`/styles.css`、`/data/diagnosis.json`），不再调用 Bun 打包；`/data/diagnosis.json` 默认读取 `<root>/data/diagnosis.json`，亦可使用 `--data` 单独覆盖。

页面可直接用浏览器打印为 PDF。

## 同一场景的三种跑法：本地、SSH lane、issue bus

上面那条产物链（Boss → DA → Builder → 站点自检 → 交叉审查）可以在本机跑，也可以交给执行
网络里的 lane 或 issue bus 执行器跑。三种入口跑的是同一份合成数据、同一组脚本和同一条验收
口径，差别只在「谁执行、在哪执行、结果怎么回来」：

| 入口 | 执行位置 | 任务载体 | 结果回来方式 | 适用场景 |
| --- | --- | --- | --- | --- |
| 本地 `run_e2e.py` | 自己的机器 | 命令行参数 | 产物落在 `--workdir` | 调试、评审、改代码 |
| SSH lane | box → 受控主机（ib1–ib5、gocc、gorelay、govpn、govpn2） | 任务卡文件 | lane 分支 `openswarm/<run-id>` | 自己控制的主机与账号 |
| GitHub issue bus | 不受控执行器（他人 agent、云 agent） | bus issue | issue 里的 `openswarm-result` 评论 | 外部或不受管的执行器 |

网络入口的部署约定见 `docs/dual-mode-*.md`（当前仓库只有 SSH 一篇：`docs/dual-mode-ssh.md`；
issue bus 的协议与结果格式在 `openswarm/docs/BUS.md`），当前盘点见 `brain/dual-mode-status.md`。
原则不变：自己控制的主机走 SSH lane，不要改走 bus。

### 1) 本地跑通

```bash
cd openbot/examples/org-health-local
python3 run_e2e.py --workdir <workdir> --review
```

产物（`brief.json`、`diagnosis.json`、`dist/`、`review.md`）与注意事项见上文「重新生成整条产物链」；
目录内单元测试用 `python3 run_tests.py`。

### 2) 经 SSH lane 派发（受控主机）

前提：box 上已配好 bridge 配置、fleet 的 SSH config、主机清单与 lane 拓扑，见
`docs/dual-mode-ssh.md`。任务卡就是给执行器看的自然语言指令：

```bash
cd /srv/phanes/openbot-workspace
umask 077
task=$(mktemp /tmp/openswarm-org-health.XXXXXX.md)
cat >"$task" <<'EOF'
Repository: openbot
Task type: docs
在 openbot/examples/org-health-local 下运行 python3 run_e2e.py --workdir <目录> --review，
把 brief.json、diagnosis.json、dist/ 和 review.md 的结论写进提交说明；只用合成数据，
不要提交真实组织数据。
EOF

openswarm-fleet fleet status --json
openswarm-fleet fleet exec ib1 -- lane status
openswarm-fleet fleet exec ib1 -- lane assign M1 --task-file "$task" \
  --title "org-health demo" --budget-minutes 15
```

`M1` 这类 lane 名以 `fleet status` 的实际输出为准，不要猜，也不要把主机名写进任务卡。
派发后查状态、取回结果：

```bash
openswarm-fleet fleet exec ib1 -- runs --active --json
openswarm-fleet fleet exec ib1 -- lane status
```

跑完确认 lane 回到 `idle`，再删临时任务卡：

```bash
rm -f -- "$task"
```

box 上由常驻 `lane-autodispatch` 承接 `lane-ready` issue 的自动派发路径见 `brain/autopilot.md`。

### 3) 经 GitHub issue bus 派发（不受控执行器）

协议、状态标签（`openswarm:queued`、`openswarm:claimed`、`openswarm:done`、`openswarm:failed`）
与结果字段见 `openswarm/docs/BUS.md`。先把 bus 仓库和白名单落地：

```bash
openswarm bus init <bus-owner>/<bus-repo>
```

再从目标仓库派发（`--base` 指向已推送的提交）：

```bash
openswarm run --backend github-issue --cwd <target-repo> --base <commit> \
  --budget-minutes 40 \
  --verify 'python3 -m unittest discover -s openbot/examples/org-health-local -p "test_*.py"' \
  --task-file task.md
openswarm bus status
```

执行侧由 worker 认领并回结果：

```bash
openswarm bus-worker --bus <bus-owner>/<bus-repo> --name <executor-name> --backend codex --once
```

`task.md` 写的就是上面 lane 任务卡的同一段要求。planner 取最早一条合法 claim 的
`openswarm-result` 评论作为结果（字段为 `status`、`summary`、`files_changed`、`tests`、
`followups`，bus 另加 `branch`、`commit`），按需在本地复验；planner 自己不合并、不推目标仓库。

### 三种入口的共同边界

- 数据边界不变：真实组织数据只能从仓库外的绝对路径进入（见上文），三种入口一律如此；
- 验收口径不变：仓库内单元测试用 `python3 run_tests.py`，端到端用
  `python3 run_e2e.py --workdir <workdir> --review`；
- 本地产物不入库：`dist/` 与本机打出的 `app.bundle.js` 已由本目录的 `.gitignore` 排除，
  执行机上的产物不要连同真实数据一起提交。

## E2E 编排

运行目录内全部单元测试（包括根目录及 boss、da、builder、review 子包）推荐使用：

```bash
cd openbot/examples/org-health-local && python3 run_tests.py
```

运行 `python3 run_e2e.py --workdir <workdir>` 可依次执行 Boss、DA、Builder 和站点检查；加 `--review` 时再执行独立审查，审查会核对 `<workdir>/brief.json`、`diagnosis.json` 与 `dist`；任一步失败时整体返回非零。可用 `--boss`、`--da`、`--builder`、`--checker` 和 `--reviewer` 指定角色脚本（`--reviewer` 默认使用 `review/cross_check.py`），用 `--raw` 指定原始 CSV 目录、用 `--bundle` 指定已编译的浏览器包。

从仓库根目录运行、只使用 Python 标准库的测试命令：

```bash
python3 -m unittest discover -s openbot/examples/org-health-local -p 'test_*.py'
```

这条命令不需要 Bun、网络或真实组织数据。若未指定 `--bundle`，Builder 在没有可复用的
`demo/vendor/app.bundle.js` 时会依赖 Bun；指定预构建 bundle 则不需要 Bun。端到端产物仍写入
`<workdir>/dist`，而预览命令中 `demo/site` 与 `demo/dist` 的用途保持不变。

加 `--review` 时审查记录写到 `<workdir>/review.md`：审查通过写 PASS 行，审查失败仍写 FAIL 行并让整体返回非 0，
即失败路径也留有产物可查。可用 `--review-report <路径>` 把审查记录写到别处（如临时目录），
默认仍是 `<workdir>/review.md`，产物命名不变；审查脚本返回非 0 时报告保留不删除，
脚本的 stdout 与 stderr（含参数失败信息）都留在报告里，运行器非 0 退出且不打印预览命令。

`openbot/` 目录下运行 `bun run test:org-health-demo` 可启动隔离服务，以浏览器检查中英文、回退语言、无效数据和请求边界，并将结果写入 `openbot/.openbot-build/org-health-demo-e2e/`（仓库根目录没有 `package.json`，这条命令要在 `openbot/` 里跑）。

服务边界测试覆盖 loopback Host 白名单、路径白名单、目录与穿越请求拒绝、`--root` 产物目录服务、`--data` 指定的数据文件、启动提示和端口占用处理；全部单元测试均由 `python3 run_tests.py` 运行。

### 无 Bun 的单角色本机复现

只验证 Builder → 检查器链路时，不需要安装 Bun 或启动浏览器。下面的命令只使用 Python
标准库创建临时目录和假的 bundle；输入是仓库内的合成示例数据，构建产物留在临时目录：

```bash
cd openbot/examples/org-health-local
tmp_dir="$(python3 -c 'import tempfile; print(tempfile.mkdtemp(prefix="org-health-local-"))')"
python3 -c "from pathlib import Path; Path('$tmp_dir/fake.bundle.js').write_text('/* fake bundle */\\nconsole.log(\"fake\");\\n', encoding='utf-8')"
python3 builder/build_site.py --data data/diagnosis.json --out "$tmp_dir/dist" --bundle "$tmp_dir/fake.bundle.js"
python3 builder/check_site.py "$tmp_dir/dist"
```

这会复现 `builder/test_build_site.py` 使用的标准库、`TMPDIR` 临时目录和假 bundle 条件，
但不等同于浏览器 e2e：它不打包真实前端资源、不启动 `serve.py`，也不检查浏览器中的语言、
请求边界或页面行为。真实诊断数据仍必须保存在仓库外；需要检查真实文件时直接把
`--data` 指向仓库外的绝对路径，不要复制进本目录。

## Boss 角色：生成诊断任务书

`boss/make_brief.py` 生成任务书；可用 `--fx` 写入 `口径.汇率`（正数，人民币/美元），省略时保持默认输出且不写汇率；`--goal` 覆盖「目标」（可重复给出，不给时用内置默认目标）。`观察期间` 可选写入 `起始期次`、`参考期次`、`截止期次`，各值必须来自 `期次` 列表。

```bash
python3 boss/make_brief.py --out <brief.json> [--goal 目标] [--month YYYY-MM] [--fx 人民币/美元]
python3 boss/make_brief.py --check <brief.json>   # 校验已有任务书，不生成新文件
```

`--out` 与 `--check` 互斥：生成用 `--out`，校验已有任务书用 `--check`（按 `boss/brief.schema.json` 校验，通过时退出码 0，不通过时退出码 1）。

## DA 角色：合成原始数据 → 诊断快照

`da/` 是诊断流水线里的 DA 角色：按任务书（brief）的观察期间与口径，把原始 CSV 归并成 schema v4 诊断快照。

```bash
cd openbot/examples/org-health-local
python3 da/build_diagnosis.py --brief <brief.json> --raw da/sample-raw --out <diagnosis.json>
python3 validate.py <diagnosis.json>
```

- 任务书可用 DA 格式 `观察期间.起始期次/参考期次/截止期次`，或 Boss 格式 `观察期间.期次`、`观察月份`、`数据截止`。Boss 格式按列表顺序取首个“实际”为起始、首个含“参考”的期次为参考、与观察月份匹配的“实际”为截止。`口径` 提供指标定义；汇率可写在任务书，缺省时从 `fx.csv` 读取。
- 原始数据目录放 `cost.csv`（月度技术人力成本与运维设施成本）、`heads.csv`（月度折算人头）、`revenue.csv`（月度收入）、`forecast.csv`（年末测算与年末目标）和汇率缺省时使用的 `fx.csv`（`人民币每美元` 列）。`da/sample-raw/` 包含纯合成样本，含合成汇率 7.2，数值与 `data/diagnosis.json` 同量级。
- 缺列、缺文件、缺汇率、表头出现重复列名或数据行缺少必需列的取值时退出码非 0，错误信息指出原始数据文件与列名；缺数据的期次写 `null`，不填估算值。输出确定性：同一份任务书与原始数据得到同一份 JSON，键序固定。
- `da/write_method.py` 生成只讲口径、不复述诊断数值的说明文档：给 `--raw` 原始数据目录时，文档里的“数据来源”小节按文件名列出每个 CSV 及其数据行数（不含表头）；不给 `--raw` 时该小节写明未提供原始数据目录。

```bash
python3 da/write_method.py --brief <brief.json> --data <diagnosis.json> --raw da/sample-raw --out <method.md>
```

- DA 单元测试包含在 `python3 run_tests.py` 中。

## 结构定义与校验

`schema/diagnosis.schema.json` 是 `data/diagnosis.json` 的结构定义（JSON Schema draft 2020-12）。字段与 `app.js` 读取的字段一致：`scope` 给出口径和 `schema版本`（固定为 4），`metrics` 按页面顺序给出四个指标，数值区分数值序列（数值或 `null`）与比率序列（数值、`null` 或字符串）。`口径说明` 是可选字段：缺省不报错，出现时必须是非空字符串数组，页面据此显示"数据口径"折叠面板。定义只覆盖当前格式；`app.js` 仍兼容的旧中文键格式不在定义内。
顶层未声明的未知键不被忽略；由于 `additionalProperties` 为 `false`，`validate.py` 会将其作为额外字段报错并使校验失败。

`validate.py` 只用标准库，按该定义校验数据文件：

```bash
cd openbot/examples/org-health-local
python3 validate.py                            # 校验 data/diagnosis.json
python3 validate.py /absolute/path/to/diagnosis.json
python3 validate.py --json data/diagnosis.json  # 输出机器可读的 JSON 结果
python3 validate.py --schema schema/diagnosis.schema.json data/diagnosis.json  # 指定结构定义
```

退出码契约：`0` 全部通过，`1` 有文件未通过校验，`2` 输入不可读（文件缺失或不是合法 JSON）。校验失败时逐条打印出错路径，例如 `/scope/schema版本`。校验器单元测试包含在 `python3 run_tests.py` 中。

## Review 角色：独立交叉审查

`review/` 是独立交叉审查角色：核对任务书（brief）、诊断快照（diagnosis）与站点产物（dist）三方一致性，逐条输出 PASS/FAIL，任一 FAIL 时退出码非 0。

```bash
cd openbot/examples/org-health-local
python3 review/cross_check.py --brief <brief.json> --data <diagnosis.json> [--dist <dist>]
```

- 期间一致：任务书 `观察期间`（对象、字符串或数组）里的期次必须覆盖 `scope.观察月份`；显式的 `观察月份`（Boss 格式）/`截止期次`/`数据截止` 必须与 `scope` 相同。
- 版本一致：任务书 `schema版本` 与 `scope.schema版本` 相同且为 4。
- 必答问题覆盖：任务书 `必答问题` 每条都要在快照的 `insights`/`recommendations` 里被引用一次；命中 id、原文或提取出的关键词任一即算被引用（关键词规则见脚本文档字符串）。
- 站点数据一致：给出 `--dist` 时 `dist/data/diagnosis.json` 与 `--data` 字节相同，并打印 sha256 前 12 位。
- 隐私边界：快照的字段名（含嵌套）不含个人/薪酬/编制粒度黑名单词条；黑名单写在脚本里。

审查单元测试只用测试内构造的最小样本，不依赖其他角色的脚本，并包含在 `python3 run_tests.py` 中。

退出码契约：`0` 全部 PASS，`1` 至少一项 FAIL，`2` 输入不可读或不是 JSON 对象。失败路径仍写产物：
`--out` 的 Markdown 报告照常落盘（含 FAIL 项与汇总），`run_e2e.py --review` 也照常写出 `review.md`。

## 构建静态站点

`builder/build_site.py` 把诊断快照构建成可预览的静态目录：先用 `validate.py` 校验数据，
不合格时退出码非 0 且不生成 `dist`；合格时复制 `index.html`、`styles.css` 与数据到
`dist/data/diagnosis.json`，并写出 `build-report.json`（输入路径、输入 sha256、文件清单、生成时间）。

```bash
cd openbot/examples/org-health-local
python3 builder/build_site.py --data data/diagnosis.json --out dist --bundle demo/vendor/app.bundle.js
```

产物为 `dist/index.html`、`dist/app.bundle.js`、`dist/styles.css`、`dist/data/diagnosis.json`
和 `dist/build-report.json`。`--bundle` 指向已编译好的浏览器包，文件必须已存在（仓库里可用
`demo/vendor/app.bundle.js`）；不带 `--bundle` 时用与 `serve.py` 相同的 bun 命令打包（需要本机安装 Bun）。
退出码契约：`0` 构建成功，`1` 数据未通过校验或 bundle 文件不存在，`2` 输入不可用，`3` 打包或写出失败。
单元测试一律传入假的 bundle 文件，不依赖 Bun 与网络，并包含在 `python3 run_tests.py` 中。
