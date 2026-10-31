# Phanes Issue #4：SWE-bench 优化 Backlog 梳理与状态审计

- **关联 Issue**：Phanes #4（承接原 OpenSwarm Issue #3）
- **审计日期**：2026-10-31
- **审计基准**：SWE-bench Verified 评测结果（`openswarm/bench/swebench/`）与当前代码库实现

---

## 一、背景与核心评测基准回顾

在 2026-10-18 的 SWE-bench Verified 实验中（详见 [`openswarm/bench/swebench/README.md`](file:///Users/surlymo/.claude/openswarm-executor-candidate/lanes/A1/wt/openswarm/bench/swebench/README.md) 及 [`openswarm/README.md`](file:///Users/surlymo/.claude/openswarm-executor-candidate/lanes/A1/wt/openswarm/README.md#L16-L37)），针对 6 个真实困难问题（来自 sympy、pytest、pylint，难度在 1-4 小时区间）使用 `deepseek-flash` 进行了 4 个 Arm 的横向对比（n=1，独立 TMPDIR 重测后）：

| Arm | 解决率 (Official Tests) | 耗时中位数 (IQR) | 单任务成本中位数 (DeepSeek) | 总成本 (6 runs) | 输入+输出 Token 中位数 |
| --- | --- | --- | --- | --- | --- |
| **baseline** (plain agent) | 3/6 (50%) | 124 s (110–175) | $0.0234 | $0.16 | 78,779 |
| **superpowers** v7.0.0 | 2/6 (33%) | 154 s (136–340) | $0.0285 | $0.31 | 85,122 |
| **openflow** 0.2.1 | 4/6 (67%) | 230 s (200–266) | $0.0412 | $0.23 | 105,793 |
| **openswarm** (planner + Pi) | **5/6 (83%)** | 228 s (187–254) | $0.0377 | $0.21 | 105,651 |

虽然 openswarm 取得了最高的任务解决率（83%），但结果也暴露出了明显的优化 backlog（即 Issue #4 聚焦的核心事项）：
1. **Overhead**：openswarm 的耗时为 baseline 的 1.83 倍（228s vs 124s），单任务成本增加 61%（$0.0377 vs $0.0234）；
2. **CI**：SWE-bench 相关的 harness 逻辑与评测有效性验证完全未接入持续集成；
3. **Next Round**：既有评测仅为 6 个实例且 n=1，MiniMax 因用量超限未跑通，需进一步扩展实例规模与模型支持。

---

## 二、Backlog 逐项状态与代码证据

### 1. Overhead（耗时与 Token 开销优化）

- **当前状态**：`open`
- **目标定义**：降低 openswarm 工作流带来的额外耗时（降低至接近 baseline）与 Token 冗余开销，提升成本效益比。
- **代码现状与证据**：
  1. **Planner 独立会话与冗余 Prompt**：
     - 在 [`openswarm/bench/swebench/harness/arms.py`](file:///Users/surlymo/.claude/openswarm-executor-candidate/lanes/A1/wt/openswarm/bench/swebench/harness/arms.py#L160-L179)，`PLANNER` 提示词模版将完整的 [`openswarm/SKILL.md`](file:///Users/surlymo/.claude/openswarm-executor-candidate/lanes/A1/wt/openswarm/SKILL.md) 全量注入（数千 Token），并包含长篇工程任务说明。
     - 在 [`openswarm/bench/swebench/harness/arms.py`](file:///Users/surlymo/.claude/openswarm-executor-candidate/lanes/A1/wt/openswarm/bench/swebench/harness/arms.py#L183-L207) 的 `arm_openswarm` 中，流程被强制分为两段：先启动一个 300 秒超时的独立 Pi 会话生成 `executor-task.md` 与 `verify.txt`（L189: `run_pi(tid + "-plan", ...)`），随后才执行 `openswarm run` 启动真实的执行器。这导致了双重冷启动和额外的整轮大模型往返开销。
  2. **评测数据证据**：
     - 在 [`openswarm/bench/swebench/results/deepseek/summary.json`](file:///Users/surlymo/.claude/openswarm-executor-candidate/lanes/A1/wt/openswarm/bench/swebench/results/deepseek/summary.json#L143-L165)，openswarm 中位耗时为 228.35s（baseline 为 124.45s，见 L11-L15）；总输入输出 Token 中位数为 105,651（baseline 为 78,779，见 L18）；单任务成本中位数高出 61.5%（$0.0377 vs $0.0234）。
  3. **运行时流程与工作区开销**：
     - 在 [`openswarm/bin/openswarm`](file:///Users/surlymo/.claude/openswarm-executor-candidate/lanes/A1/wt/openswarm/bin/openswarm#L5129-L5351) 中，`cmd_run` 虽通过 `--no-worktree`（`arms.py` L204）略过了二次工作树复制，但后续的 [`run_verify`](file:///Users/surlymo/.claude/openswarm-executor-candidate/lanes/A1/wt/openswarm/bin/openswarm#L4940-L4947) 宿主机检验直接调用 pytest/node，在大型测试套件下无局部裁剪，拖长总耗时。
  4. **优化进展评估**：
     - 代码库中目前 `openswarm run` 具备直接传入 `--prompt` 或 `--task-file` 的能力（[`openswarm/bin/openswarm`](file:///Users/surlymo/.claude/openswarm-executor-candidate/lanes/A1/wt/openswarm/bin/openswarm#L5116-L5120)），但 SWE-bench 评测体系尚未引入紧凑版任务计划或单会话直通模式（direct execution path），开销优化项仍处于完全开放（`open`）状态。
- **下一步具体行动 (Next Concrete Step)**：
  1. **精简 Planner Prompt**：在 `openswarm/bench/swebench/harness/arms.py` 中移除完整 `SKILL.md`，换为专注于代码修复、目标文件识别与单行 verify 命令的精炼提示词（预计减少 40% 的 planner 输入 Token）。
  2. **支持轻量单轮 / 直通执行模式**：为 `arm_openswarm` 增加可选的直通执行分支（单会话合并 planner 与执行指令），针对单文件小改动跳过独立 planner 轮次。
  3. **验证命令目标收敛**：优化 planner 生成的 `verify.txt`，确保仅运行与该 issue 直接相关的单项测试用例（如特定 test function），避免触发整个测试文件的全量 setup。

---

### 2. CI（SWE-bench 体系接入持续集成）

- **当前状态**：`partially done`
- **目标定义**：将 SWE-bench 的验证有效性判定、离线评分解析与数据流健康检查接入 CI，防止评测工具链回归。
- **代码现状与证据**：
  1. **主线 CI 现状**：
     - 查看 [`.github/workflows/phanes.yml`](file:///Users/surlymo/.claude/openswarm-executor-candidate/lanes/A1/wt/.github/workflows/phanes.yml#L1-L47)，CI 仅包含 `openswarm` 基础单元测试（L25: `python -m unittest discover -s tests -v`）、基于 demo-state 的离线 CLI 冒烟测试（L27-33: `openswarm status`, `openswarm route`），以及 openbot 的类型/i18n 检查（L34-47）。SWE-bench 相关组件未出现在任何工作流中。
  2. **既有 Bench 测试局限**：
     - 查看 [`openswarm/tests/test_bench.py`](file:///Users/surlymo/.claude/openswarm-executor-candidate/lanes/A1/wt/openswarm/tests/test_bench.py#L1-L60)，该测试仅校验合成路由基准测试（`make_trace.py`、`catalogue.json`、`trace.jsonl`），完全不涉及 `openswarm/bench/swebench/`。
  3. **离线测试资产未接入**：
     - 在 [`openswarm/bench/swebench/test_validity.py`](file:///Users/surlymo/.claude/openswarm-executor-candidate/lanes/A1/wt/openswarm/bench/swebench/test_validity.py#L1-L150) 中，已实现了针对 MiniMax 运行有效性（`validity.py`）的 8 项单测，用于检测超时、网络泄露、空输出及异常运行。
     - 本地实测表明：在设置 `PYTHONPATH=openswarm/bench/swebench` 时，这 8 项测试全部通过（`Ran 8 tests in 0.010s, OK`）；但由于其文件位于 `bench/swebench/` 且代码直接写 `import validity`（L11），导致默认的 `python -m unittest discover -s tests` 既无法扫描到它，直接执行也会产生 `ModuleNotFoundError: No module named 'validity'`。
  4. **其他未接入 CI 的工具**：
     - [`openswarm/bench/swebench/harness/validate.py`](file:///Users/surlymo/.claude/openswarm-executor-candidate/lanes/A1/wt/openswarm/bench/swebench/harness/validate.py#L1-L80) 包含金标补丁（gold patch）离线验证逻辑；
     - [`openswarm/bench/swebench/report.py`](file:///Users/surlymo/.claude/openswarm-executor-candidate/lanes/A1/wt/openswarm/bench/swebench/report.py#L29-L45) 包含数据泄露（leak scan）检查与统计报表汇总；这些离线工具均无自动化测试守卫。
- **下一步具体行动 (Next Concrete Step)**：
  1. **测试迁移与导入修复**：将 `openswarm/bench/swebench/test_validity.py` 修复导入路径（支持模块化 import 或在 `openswarm/tests/` 建立薄包装 `test_swebench_validity.py`），使 `unittest discover -s tests` 能直接自动发现并运行。
  2. **新增离线 Harness 单元测试**：在 `openswarm/tests/` 下新增 `test_swebench_harness.py`，覆盖：
     - `swe_env.py` 中的测试命令构造与输出日志解析；
     - `report.py` 中的 leak markers 扫描匹配；
     - `regrade.py` 中的独立 TMPDIR 注入逻辑。
  3. **工作流配置**：在 `.github/workflows/phanes.yml` 中将上述离线单元测试正式纳入门禁。

---

### 3. Next Round（下一轮评测扩展与多模型/规模化验证）

- **当前状态**：`open`
- **目标定义**：扩展 SWE-bench 评测至完整实例池、增加重复次数以消除统计噪声、解决 MiniMax 配额超限问题，并接入 Phanes 统一后端的更多模型。
- **代码现状与证据**：
  1. **实例池定义与既有范围差距**：
     - 在 [`openswarm/bench/swebench/instances.json`](file:///Users/surlymo/.claude/openswarm-executor-candidate/lanes/A1/wt/openswarm/bench/swebench/instances.json#L5-L20)，Round 2 已定义并选定了 12 个真实 GitHub 实例（覆盖 sympy 7 个、pytest 3 个、pylint 2 个在 1-4 小时和 >4 小时的全部 Verified 题）。
     - 然而当前评测报告（[`openswarm/bench/swebench/README.md`](file:///Users/surlymo/.claude/openswarm-executor-candidate/lanes/A1/wt/openswarm/bench/swebench/README.md#L6-L8)）中仅跑了 6 个实例（`pylint-8898`, `pytest-10356`, `pytest-5787`, `pytest-6197`, `sympy-14248`, `sympy-17630`）。剩余的 6 个实例（`sympy-12489`, `sympy-13852`, `sympy-13878`, `sympy-16597`, `sympy-18199`, `pylint-4551`）尚未在 4 个 arm 上执行。
  2. **样本重复度与置信度差距**：
     - 在 [`openswarm/bench/swebench/README.md`](file:///Users/surlymo/.claude/openswarm-executor-candidate/lanes/A1/wt/openswarm/bench/swebench/README.md#L35-L43) 的 Caveats 中明确指明：“n = 1 run on 6 instances (not 3 repeats on 12); differences of one or two resolved runs are within noise”。需要扩展到 `reps 3` 才能提供具备统计学意义的对比。
  3. **MiniMax 用量超限未闭环**：
     - 在 [`openswarm/bench/swebench/results/minimax/`](file:///Users/surlymo/.claude/openswarm-executor-candidate/lanes/A1/wt/openswarm/bench/swebench/results/minimax/) 与 README 中记录，MiniMax-M3.1-Flash-Preview 矩阵中 openswarm 和 openflow 因遭遇 API usage cap / 429 报错，没有产出任何干净的完成记录，导致仅有 baseline (3/4) 与 superpowers (2/4) 的不完整表。
  4. **执行器后端单一，未利用 Phanes 异构能力**：
     - 当前 [`openswarm/bench/swebench/harness/arms.py`](file:///Users/surlymo/.claude/openswarm-executor-candidate/lanes/A1/wt/openswarm/bench/swebench/harness/arms.py#L11) 的所有模型调用均硬编码通过 `run_pi.sh`（Pi 代理），无法评测其他高阶模型。
     - 而在当前 Phanes 仓库（[`HANDOFF.md`](file:///Users/surlymo/.claude/openswarm-executor-candidate/lanes/A1/wt/HANDOFF.md#L5-L17) 及 [`openswarm/backends/`](file:///Users/surlymo/.claude/openswarm-executor-candidate/lanes/A1/wt/openswarm/backends/)）中，已统一集成了 Codex (`gpt-6-luna`), Antigravity (`gemini-3.8-flash-medium`), GitHub Copilot (`claude-sonnet-5.5` / `claude-haiku-5.5`) 等多种执行器后端，这些后端尚未接入 SWE-bench 进行评测。
  5. **运行器基础设施已完备**：
     - [`openswarm/bench/swebench/run.py`](file:///Users/surlymo/.claude/openswarm-executor-candidate/lanes/A1/wt/openswarm/bench/swebench/run.py#L1-L120) 已经内建支持 `--arms`, `--reps`, `--jobs`, `--only` 以及断点续跑（resumable jsonl），具备立即扩展评测的能力。
- **下一步具体行动 (Next Concrete Step)**：
  1. **放开 12 实例完整矩阵评测**：移除 `run.py` 的 `--only` 限制，对 `instances.json` 中全部 12 个 Round 2 实例发起评测。
  2. **执行多轮重复测试（reps=3）**：以 `--reps 3` 运行，获取每个 arm 在每个 instance 上的多次采样，计算稳定的成功率与四分位耗时（IQR）。
  3. **引入 MiniMax 节奏控制（Rate Limit Pacing）**：在调用入口配置并发上限或请求间隔，防止触碰 MiniMax subscription 额度上限，完成 MiniMax 4-arm 的干净数据产出。
  4. **接入 Phanes 多执行器后端**：在 `arms.py` 中扩展接入 openswarm 的 `--backend codex`、`--backend agy`、`--backend copilot`，评测在更强基模型（如 GPT-6 Luna, Claude Sonnet 5.5, Gemini 3.8）下 openswarm 工作流带来的边际收益。

---

## 三、Backlog 推进路线与任务优先级总结

| Backlog 项 | 当前状态 | 阻塞程度 | 涉及核心文件 | 预期收益 | 推荐优先级 |
| --- | --- | --- | --- | --- | --- |
| **CI** | `partially done` | 低（已有测试与工具，仅缺接入和路径修复） | `openswarm/bench/swebench/test_validity.py`<br>`.github/workflows/phanes.yml` | 防止 validity/harness 代码默默损坏，提升工程自闭环质量 | **P0（最高）** |
| **Overhead** | `open` | 中（需重构 prompt 模版并校验效果） | `openswarm/bench/swebench/harness/arms.py`<br>`openswarm/bin/openswarm` | 预计缩减 30%-40% 耗时与 Token，显著缩小与 baseline 的效率差距 | **P1** |
| **Next Round** | `open` | 高（依赖 API 额度与较大计算耗时） | `openswarm/bench/swebench/instances.json`<br>`openswarm/bench/swebench/run.py`<br>`openswarm/bench/swebench/harness/arms.py` | 产出可信度高（n=3、12 实例）、覆盖多后端的正式评测数据与论文/技术报告素材 | **P2** |
