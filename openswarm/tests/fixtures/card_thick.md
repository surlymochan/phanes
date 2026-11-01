# OpenSwarm CLI：新增 `card-lint` 子命令，把薄卡与薄 PR 的判定收进 openswarm
labels: p1,backend:codex
Repository: openswarm（Python 3.11+）。先读 `openswarm/CONTRIBUTING.md` 与 `openswarm/README.md` 的命令参考一节，
再按仓库现有风格改动；不要引入新依赖，不要改 `openswarm/bench/` 下的评测脚本。

## Scope

- 在 `openswarm/bin/openswarm` 新增 `card-lint` 子命令，负责判定「薄卡」与「薄 PR」。
- 在 `openswarm/policy.json` 新增 `acceptance` 一节，把判定阈值与关键字列表做成可覆盖的策略键。
- 新增 `openswarm/tests/test_card_lint.py`，覆盖三张已知薄卡、一张厚卡与两种 PR diff。
- 在 `openswarm/README.md` 与 `openswarm/SKILL.md` 的命令参考里登记这个子命令。

## Findings

- 薄卡的三种形态在自动派发那边各有一份私有实现：正文只剩关键词、验收只有 `rg`/`head`、预估有效 diff 不足。
- 判定规则散落在控制面脚本里，复审时看不到阈值是怎么定的，也没法在本地复现一次拒绝。
- `openswarm/policy.json` 还没有承载这张表的 `acceptance` 一节，阈值只能改代码。
- 薄 PR 的判定只看 `git diff --numstat`，但没有区分代码文件与文档文件，纯文档改动也被算成有效改动。
- 控制面拿不到稳定的理由码，只能自己再解析一次输出文本。
- 卡片的正文里既有标题行，也有 `labels:` 行与 `验收：` 行，估算有效 diff 时必须把它们排除。
- PR 的 diff 里可能混进重命名路径，`git diff --numstat` 会把它们写成 `old => new`，统计时要按新路径取后缀。
- 二进制文件在 `numstat` 里记成 `-`，不能当成零行，也不能当成有效改动。
- 三张已知薄卡要作为测试夹具固定下来，避免以后阈值微调时无人察觉。
- 厚卡也要有夹具，否则「拒绝」这一侧通过测试不代表「放行」这一侧没写错。
- 理由码的顺序要稳定，控制面会按顺序打印第一条例外。
- 夹具里的薄卡正文只放真实存在的命令，不要为了凑行数写无意义的话。
- 判定函数保持纯函数：不读环境变量、不联网、不写文件。
- 测试只跑本地临时 git 仓库，不碰真实远端与真实仓库。

## Rules

- 规则只从 policy 的 `acceptance` 一节读取：`min_effective_diff`、`thin_keywords`、`weak_acceptance_re`。
- 三个默认值必须与现在的自动派发一致，不要顺手改语义。
- 拒绝时给出稳定的理由码，并同时输出 JSON，方便控制面直接消费。
- 卡片的「预估有效 diff」按正文有效行估算：标题、`labels:` 行、`验收：` 行和标题行不计入。
- PR 的「有效代码行」只统计源码文件，文档、数据与锁文件不计入。
- 子命令的读卡路径支持文件路径与 `-`（标准输入）两种写法。
- 理由码一旦发布就不再改名字，控制面会按名字分支处理。
- 不要顺带改自动派发的调用方；那是另一张卡。
- 不要把仓库里的凭证、`.env*` 文件或认证信息写进测试夹具。

## Report

- 说明每个理由码对应的判定条件。
- 列出实际跑过的命令与结果。
- 附上新增与修改的文件清单。
- 如果某个判定条件没有把握，写清楚不确定性，不要含糊过去。

验收：`python3 -m unittest discover -s tests -p 'test_card_lint.py'`
