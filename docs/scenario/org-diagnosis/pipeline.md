# 组织诊断场景：Boss→DA→Builder 三角色流水线

Refs #71、#54（#6「三 Bot 场景」的一部分）。

本文给出用 OpenSwarm 编排「组织健康诊断」三阶段流水线的做法：每个阶段的角色、任务提示、产物路径和
`--verify` 门禁；阶段之间用上一阶段的提交作为下一阶段的 `--base`；每阶段结束后做一次跨执行器交叉审查。
真实组织数据不进入仓库，只经仓库外的绝对路径传入（DA `--raw <dir>`，Builder `--data <diagnosis.json>`，预览 `serve.py --root <dist>`）。

配套的可执行示例编排：[`openswarm/examples/org-diagnosis-pipeline.json`](../../../openswarm/examples/org-diagnosis-pipeline.json)
（`python3 -m json.tool openswarm/examples/org-diagnosis-pipeline.json` 可解析）。
从零跑通演示可参考：[`docs/scenario/org-diagnosis/demo.md`](demo.md)。

## 1. 场景与角色

诊断站点已经在仓库内落地：`openbot/examples/org-health-local/`（`serve.py` + `app.js` + `index.html`），
用合成示例数据渲染「技术人力成本 / 折算人头 / 收入与成本率」等指标。真实组织数据按
`openbot/examples/org-health-local/README.md:5` 的约定保存在仓库外，只经绝对路径传入。

本流水线把「做一次组织诊断」拆成三个角色：

| 阶段 | 角色 | 职责 | 产物 | 是否含真实数据 |
|---|---|---|---|---|
| 1 | Boss | 把诊断目标变成口径与问题清单（任务书） | `docs/scenario/org-diagnosis/out/boss-brief.json`（仓库内） | 否 |
| 2 | DA | 按任务书口径把真实数据归并成结构化诊断 | `${DIAGNOSIS}`（仓库外）+ `docs/scenario/org-diagnosis/out/da-method.md`（仓库内） | 是（仅仓库外那份） |
| 3 | Builder | 基于诊断快照构建静态分发包并执行静态检查 | `${DIST}`（仓库外）+ `openbot/examples/org-health-local/` 改动（可选）+ `docs/scenario/org-diagnosis/out/builder-notes.md`（仓库内） | 是（仅仓库外 dist 中的 diagnosis.json） |

```
                 提交/分支交接（下一阶段 --base = 上一阶段 commit）
   ┌────────┐        ┌────────┐        ┌──────────┐
   │  Boss  │ ─────► │   DA   │ ─────► │ Builder  │
   └────────┘        └────────┘        └──────────┘
        │                 │                  │
        └──── 交叉审查 ────┴──── 交叉审查 ────┘   （openswarm review，跨执行器族）

   真实数据：${RAW_DIR} ──DA──► ${DIAGNOSIS} ──Builder──► ${DIST} ──预览──► serve.py --root （全程在仓库外）
```

三个阶段都由 `openswarm run` 执行：每次运行在独立 worktree 里进行，分支名 `openswarm/<run-id>`
（`openswarm/bin/openswarm:4854`），执行器结束后在分支上提交（`openswarm/bin/openswarm:5055-5059`），
结果写进 `$OPENSWARM_HOME/runs/<run-id>/result.json`（`openswarm/bin/openswarm:5383-5394`）。

## 2. 前置条件

- Python 3.9+、git，以及至少一个已登录的执行器（本例用 `codex`）。
- 一个**位于仓库之外**的工作目录（下文记作 `WORKSPACE`），放真实数据、任务提示文件和 DA 产物。
- 审查执行器必须是另一个模型族：`codex` 的运行只能由 `agy` / `pi` / `cursor` 审查
  （`openswarm/bin/openswarm:5487-5494`）。
- openswarm 不合并、不推送：合入由人工评审后执行 `git -C <repo> merge --no-ff openswarm/<run-id>`。

## 3. 三个阶段

### 3.1 Boss：口径与任务书

- **输入**：`docs/scenario/org-diagnosis/pipeline.md`（本文）、`openbot/examples/org-health-local/README.md`、
  合成示例 `openbot/examples/org-health-local/data/diagnosis.json`。
- **任务提示**（写进 `WORKSPACE/tasks/boss.md`，也可直接用示例编排里的 `stages[0].prompt`）：
  产出 `docs/scenario/org-diagnosis/out/boss-brief.json`，包含 `目标` / `观察期间` / `口径` / `必答问题` /
  `数据需求` / `禁止事项`；只写口径、问题与字段映射，不得写入任何真实组织数据或真实数字。
- **产物**：`docs/scenario/org-diagnosis/out/boss-brief.json`（仓库内，随分支提交）。
- **`--verify`**：`python3 -m json.tool docs/scenario/org-diagnosis/out/boss-brief.json >/dev/null`
  （Python 标准库命令，当前仓库无需新增脚本）。

```bash
openswarm run \
  --cwd <repo> --base HEAD \
  --backend codex --type docs --difficulty 2 --budget-minutes 20 \
  --title 组织诊断任务书 \
  --task-file <workspace>/tasks/boss.md \
  --verify 'python3 -m json.tool docs/scenario/org-diagnosis/out/boss-brief.json >/dev/null'
```

### 3.2 DA：诊断分析

- **输入**：上一阶段的任务书 `docs/scenario/org-diagnosis/out/boss-brief.json`（在 DA 的 worktree 里，
  因为 `--base` 就是 Boss 阶段的提交）；真实组织原始数据 `${DATA}`（仓库外绝对路径，只读）。
- **任务提示**：按任务书口径把真实数据归并成 schema v4 的诊断 JSON，写到仓库外的 `${DIAGNOSIS}`；
  同时在仓库内提交 `docs/scenario/org-diagnosis/out/da-method.md`，逐条说明结论字段的数据来源路径、
  计算口径与已知缺口，**不含真实数字**。
- **产物**：`${DIAGNOSIS}`（仓库外，真实数据）+ `docs/scenario/org-diagnosis/out/da-method.md`（仓库内）。
- **`--verify`**：`python3 openbot/examples/org-health-local/validate.py --data "<DIAGNOSIS>" &&
  test -s docs/scenario/org-diagnosis/out/da-method.md`
  —— 其中 `openbot/examples/org-health-local/validate.py` 由 **DA 校验器 issue**（与 #54 同批次的并行
  issue）提供，**当前仓库还没有这个文件**；在它落地前本阶段的 `--verify` 不可执行，临时替代为
  `python3 -m json.tool "<DIAGNOSIS>" >/dev/null`（只查 JSON 形状，不查 schema）。

```bash
openswarm run \
  --cwd <repo> --base <boss 阶段 commit> \
  --backend codex --type impl --difficulty 4 --budget-minutes 30 \
  --title 组织诊断数据分析 \
  --task-file <workspace>/tasks/da.md \
  --verify 'python3 openbot/examples/org-health-local/validate.py --data <workspace>/out/da/diagnosis.json && test -s docs/scenario/org-diagnosis/out/da-method.md'
```

### 3.3 Builder：站点接入

- **输入**：诊断数据 `${DIAGNOSIS}`（仓库外）、口径说明 `docs/scenario/org-diagnosis/out/da-method.md`、
  现有站点实现 `openbot/examples/org-health-local/`。
- **任务提示**：让站点能加载并正确渲染真实诊断数据；保留 `serve.py --data /absolute/path/to/diagnosis.json`
  这个外部数据入口；真实数据不得进入仓库；改动限于 `openbot/examples/org-health-local/`，
  并在 `docs/scenario/org-diagnosis/out/builder-notes.md` 记录改动点与验证方式；不引入新的运行时依赖。
- **产物**：`openbot/examples/org-health-local/` 的改动 + `docs/scenario/org-diagnosis/out/builder-notes.md`。
- **`--verify`**：`python3 openbot/examples/org-health-local/test_serve.py --data "<DIAGNOSIS>"`
  —— `test_serve.py` 由 **Builder 测试 issue** 提供，**当前仓库还没有这个文件**；在它落地前本阶段没有
  自动门禁，可先用仓库已有的 `bun run test:org-health-demo`（`openbot/package.json:194`）覆盖合成示例，
  但它需要 bun 与 Chrome，且不覆盖真实数据。

```bash
openswarm run \
  --cwd <repo> --base <da 阶段 commit> \
  --backend codex --type impl --difficulty 3 --budget-minutes 25 \
  --title 组织诊断站点接入 \
  --task-file <workspace>/tasks/builder.md \
  --verify 'python3 openbot/examples/org-health-local/test_serve.py --data <workspace>/out/da/diagnosis.json'
```

## 4. 阶段之间的交接

交接靠 git，不靠复制文件：

1. 每个阶段用 `openswarm run --base <sha>` 起一个独立 worktree（`openswarm/bin/openswarm:4848-4862`），
   阶段产物提交在自己的 `openswarm/<run-id>` 分支上。
2. 阶段结束后从 `result.json` 取 `commit`（阶段提交）与 `branch`（`openswarm/<run-id>`），
   下一阶段的 `--base` 就取这个 `commit`。
3. 上一阶段没有产生仓库内提交时（`commit` 为空），回退到它的 `workspace.base`，即该阶段实际使用的
   base 提交，流水线不中断。
4. 三个阶段的提交是链式的（Boss 的 commit 是 DA 的 base，DA 的 commit 是 Builder 的 base），
   所以最终合并 Builder 分支即可带上前两阶段的改动；也可以逐段合并：

```bash
git -C <repo> merge --no-ff openswarm/<boss-run-id>
git -C <repo> merge --no-ff openswarm/<da-run-id>
git -C <repo> merge --no-ff openswarm/<builder-run-id>
```

5. 审查不通过时返工：`openswarm run --rework-of <run-id> ...`（README「Observe / Learn」一节）。

**每个阶段都必须留下仓库内提交**：`openswarm review` 要求分支存在且 diff 非空
（`openswarm/bin/openswarm:5532-5543`），没有提交就谈不上交叉审查。因此 DA 阶段除了仓库外的诊断 JSON，
还要在仓库内提交不含真实数字的 `da-method.md`。

**真实数据不参与 git 交接**：它始终留在仓库外的 `${DIAGNOSIS}`，靠路径传递。

## 5. 交叉审查

每阶段结束后跑一次跨执行器族的只读审查，审查结果落在 `$OPENSWARM_HOME/runs/<run-id>/review.json`
（`openswarm/bin/openswarm:5853-5856`）：

```bash
openswarm review <run-id> --by pi --json
```

- 门禁：`verdict=changes` 或 findings 里出现 P0/P1 时**不进入下一阶段**，改用
  `openswarm run --rework-of <run-id>` 返工。
- 审查执行器必须与阶段执行器不同族（`codex` → `agy`/`pi`/`cursor`，
  `openswarm/bin/openswarm:5487-5494`）。
- 自动审查只在 `--card-type` 属于 `feature`/`fix`/`review-fix`/`migration` 时触发
  （`openswarm/bin/openswarm:5905-5910`，`openswarm/policy.json` 的 `review.auto_card_types`），
  本流水线不带 `--card-type`，所以交叉审查是显式的一步。
- 想要两个执行器并行产出 DA 结论再对比时，可以用 `openswarm compare --task-file <card> --title <t>
  --a codex --b pi`。

## 6. 真实数据边界

- 真实组织数据只经**仓库外的绝对路径**进入：`python3 openbot/examples/org-health-local/serve.py
  --data /absolute/path/to/diagnosis.json`（`openbot/examples/org-health-local/serve.py:39-42`）。
- 仓库内只保留合成示例 `openbot/examples/org-health-local/data/diagnosis.json`；
  `openbot/scripts/org-health-demo-e2e.ts:13-16` 也是把合成数据复制到临时目录再经 `--data` 传入，
  作为「数据不落库」的既有做法。
- DA 阶段的诊断 JSON 写在仓库外，不随分支提交；仓库内只提交不含真实数字的说明文件。
- `verify` 命令里，仓库内路径用相对路径（相对 worktree 根），仓库外路径用绝对路径：openswarm 只把
  base 仓库的绝对路径前缀改写成 worktree 路径（`openswarm/bin/openswarm:4923-4926`），
  仓库外路径不会被改写。
- 建议加一道提交后检查：列出该阶段改动的文件，除合成示例外不应出现任何 `diagnosis*.json`：

```bash
git -C <repo> diff --name-only <stage-base>..<stage-commit>
git -C <repo> show --stat <stage-commit>
```

示例编排的 runner 会自动执行这条检查（`data_policy.forbidden_paths` /
`data_policy.allowed_paths`），命中即中止流水线。

## 7. 运行示例编排

示例编排 `openswarm/examples/org-diagnosis-pipeline.json` 描述了变量、三阶段、交接规则、审查门禁和
未落地依赖。下面这段 runner 按顺序执行三个阶段，做完一个阶段就交叉审查，并把上一阶段的提交喂给
下一阶段的 `--base`（可直接从仓库根目录运行）：

```bash
export REPO="$PWD"                       # 仓库根目录
export WORKSPACE="/path/to/org-diagnosis" # 必须在仓库之外
export OPENSWARM=openswarm               # 未安装时可用 openswarm/bin/openswarm
python3 - <<'PY'
import json, os, pathlib, re, shutil, subprocess, sys

SPEC = pathlib.Path("openswarm/examples/org-diagnosis-pipeline.json")
spec = json.loads(SPEC.read_text(encoding="utf-8"))
variables = spec["variables"]

def resolve(name, seen=()):
    value = os.environ.get(name) or variables[name]["default"]
    for ref in re.findall(r"\$\{(\w+)\}", value):
        if ref in seen or ref not in variables:
            continue
        value = value.replace("${" + ref + "}", resolve(ref, seen + (name,)))
    return value

values = {name: resolve(name) for name in variables}

def expand(text):
    for _ in range(8):
        new = re.sub(r"\$\{(\w+)\}", lambda m: values.get(m.group(1), m.group(0)), text)
        if new == text:
            break
        text = new
    return text

repo = pathlib.Path(expand(values["REPO"])).resolve()
workspace = pathlib.Path(expand(values["WORKSPACE"])).resolve() if values["WORKSPACE"] else None
if workspace is None:
    sys.exit("必须设置 WORKSPACE（仓库外目录）")
if workspace == repo or repo in workspace.parents:
    sys.exit("WORKSPACE 必须位于仓库之外")
cli = values["OPENSWARM"]
if shutil.which(cli) is None:
    fallback = repo / "openswarm" / "bin" / "openswarm"
    if fallback.exists():
        cli = str(fallback)
values["OPENSWARM"] = cli
home = pathlib.Path(os.environ.get("OPENSWARM_HOME", str(pathlib.Path.home() / ".claude/openswarm")))
policy = spec["data_policy"]

base = values["BASE"]
commits = {}
for stage in spec["stages"]:
    source = stage.get("base_from")
    if source:
        base = commits[source]
    stage_base = base
    values["BASE"] = base
    task_file = pathlib.Path(expand(stage["task_file"]))
    task_file.parent.mkdir(parents=True, exist_ok=True)
    task_file.write_text(expand(stage["prompt"]), encoding="utf-8")
    argv = [expand(part) for part in stage["command"]]
    print("==>", " ".join(argv), flush=True)
    done = subprocess.run(argv, capture_output=True, text=True)
    print(done.stdout, done.stderr, flush=True)
    if done.returncode != 0:
        sys.exit(f"阶段 {stage['id']} 失败，退出码 {done.returncode}")
    payload = json.loads(done.stdout) if done.stdout.strip().startswith("{") else {}
    run_id = payload.get("id")
    record = home / "runs" / str(run_id) / "result.json"
    if run_id and record.exists():
        payload = json.loads(record.read_text(encoding="utf-8"))
    commit = payload.get("commit") or (payload.get("workspace") or {}).get("base") or stage_base
    commits[stage["id"]] = commit
    changed = subprocess.run(["git", "-C", str(repo), "diff", "--name-only", f"{stage_base}..{commit}"],
                             capture_output=True, text=True).stdout.split()
    leaked = [path for path in changed
              if path not in policy["allowed_paths"]
              and any(re.search(pattern, path) for pattern in policy["forbidden_paths"])]
    if leaked:
        sys.exit(f"阶段 {stage['id']} 把真实数据带进了分支：{leaked}")
    values["RUN_ID"] = run_id or ""
    review = subprocess.run([expand(part) for part in spec["review"]["command"]])
    if review.returncode != 0:
        sys.exit(f"阶段 {stage['id']} 的交叉审查失败，流水线停止")
print("流水线完成，各阶段 base/commit：", commits)
PY
```

说明：

- `openswarm run` 的退出码：0 表示 `done`，1 表示未通过，3 表示预算拒绝
  （`openswarm/bin/openswarm:5414`）；runner 遇到非 0 就停，不自动重试。
- `--verify` 在执行器结束、提交之后于宿主上运行（`openswarm/bin/openswarm:5339`），超时上限 600 秒；
  它的退出码覆盖执行器自报的结果（`openswarm/bin/openswarm:4996-5004`）。
- `WORKSPACE` 必须在仓库之外：仓库内的绝对路径会被 openswarm 改写成 worktree 路径
  （`openswarm/bin/openswarm:4923-4926`），真实数据放仓库内会被误改写。
- runner 只做编排，不做合并；合并仍由人工评审后执行。

## 8. 未落地依赖

| 依赖 | 提供方 | 影响 |
|---|---|---|
| `openbot/examples/org-health-local/validate.py` | DA 校验器 issue（与 #54 同批次的并行 issue） | 未落地前 DA 阶段的 `--verify` 不可执行，只能退回 `python3 -m json.tool` |
| `openbot/examples/org-health-local/test_serve.py` | Builder 测试 issue | 未落地前 Builder 阶段没有自动门禁 |
| `bun run test:org-health-demo`（`openbot/package.json:194`） | 仓库已有 | 需要 bun 与 Chrome，且只覆盖合成示例数据 |
| 真实组织数据文件 | 数据提供方 | 必须自行放在仓库外，流水线不提供真实数据 |

## 9. 已知限制

- 本文档只描述编排方式，没有跑过真实组织数据：三个阶段的 `--verify` 依赖上表中两个尚未落地的脚本。
- 示例编排固定使用 `codex` 执行、`pi` 审查；换执行器时要同时满足跨族审查约束
  （`openswarm/bin/openswarm:5487-5494`）。
- 审查是只读的第二意见，最终验收仍属控制面：`openswarm` 不合并、不推送，验收决定由人做出
  （`openswarm ledger accept <run-id> --reason "..."` 可记录该决定）。
- `WORKSPACE` 里的真实数据不进版本库，也不进 `$OPENSWARM_HOME` 的运行记录；流水线结束后按数据提供方的
  保留策略清理该目录。
