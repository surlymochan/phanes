# Project #6 看板用法与标签映射

本文记录 phanes RSI 异构 Agent 网络看板（GitHub Project #6）的地址、Status 列的用法，以及
issue 标签 `lane-ready` / `in-progress` / `lane-pr` 与看板状态的对应建议。

看板是**执行视图**，GitHub issue 是**任务队列**；两者靠标签对齐，靠人/脚本维护。

---

## 1. 看板地址

| 项 | 值 |
|---|---|
| 看板 URL | <https://github.com/users/surlymochan/projects/6> |
| 看板名称 | phanes RSI 异构 Agent 网络（`README.md:8`） |
| 归属账号 | `surlymochan`（用户级 Project，URL 中的 `/users/surlymochan/`） |
| 覆盖范围 | 本仓库的 Issue / PR 汇总看板（`README.md:8`：Issue、PR、Discussions 以本仓库为入口） |
| 其他看板 | 既有 Phanes / Agent Task Project 保留历史与跨项目视图（`README.md:8`） |

本仓库的 Issue、PR 和 Discussions 都以本仓库为入口，看板只做执行视图（`README.md:8`）。

查看与写入命令（需要 `gh` 已登录，且 token 带 `project` scope）：

```sh
gh project view 6 --owner surlymochan
gh project field-list 6 --owner surlymochan
gh project item-list 6 --owner surlymochan --field Status
```

---

## 2. Status 列

Status 是 GitHub Project 的单选字段，写入用 `gh project item-edit --field Status --value ...`
（`gh project item-edit --help`）。本文建议的选项集合：

| 建议 Status | 含义 | 结束条件 |
|---|---|---|
| `Todo` | 任务已建、等待被 lane 领走 | 有 lane 开始执行 |
| `In Progress` | 已被某个 lane 领走，正在执行 | lane 产出 PR |
| `In Review` | 已开 PR，等待审查与合并 | PR 合入、issue 关闭 |
| `Done` | 已合入并关闭 | — |

**未核实项：** 本仓库没有保存该看板 Status 选项的快照；撰写本文的环境没有 GitHub 登录也没有网络
（`gh auth status` 报未登录，`api.github.com` 不可解析），因此上表是**建议值**，不是看板当前的实际
选项名或字段 ID。落地前请先核对：

```sh
gh project field-list 6 --owner surlymochan --format json
```

看板实际选项名与建议不一致时，以看板为准调整本文表格，不要反过来改看板字段定义。

---

## 3. 标签与看板 Status 的对应建议

### 3.1 三个标签的语义

| 标签 | 语义 | 现状 |
|---|---|---|
| `lane-ready` | 待派发队列标签；`lane-autodispatch` 从中挑活，优先匹配 `backend:*`，否则派无 backend 标签的 issue | 已在用（`brain/autopilot.md:11`、`brain/autopilot.md:17`、`brain/autopilot.md:35`、`brain/dual-mode-status.md:15`） |
| `in-progress` | 已被 lane 领走、正在执行 | 建议新增；本仓库无任何脚本或文档产生该标签 |
| `lane-pr` | lane 已完成并开出 PR（`Closes #N`），等待审查与合并 | 建议新增；本仓库无任何脚本或文档产生该标签 |

### 3.2 对应关系

| issue 标签 | 建议看板 Status | 谁负责打/换这个标签 | 依据 |
|---|---|---|---|
| `lane-ready` | `Todo` | 建 issue 时人工或规划侧打上 | `brain/autopilot.md:17`（标签 `lane-ready` + `p0\|p1\|p2`）、`brain/autopilot.md:35` |
| `in-progress` | `In Progress` | `lane-autodispatch` 派活成功后打上 | 建议；对应 lane 状态 `busy`（`openswarm/bin/openswarm:7384`）与 run 状态 `running`（`openswarm/bin/openswarm:3626`、`openswarm/bin/openswarm:7698`） |
| `lane-pr` | `In Review` | lane 完成后推分支并开 PR 时打上 | `brain/autopilot.md:11`（推 `fleet/<L>-issue<N>-<run>` 并开 PR，Closes #N）、`brain/autopilot.md:12`（`lane-merge` 合并 `fleet/*` 与 `brain/*` PR） |
| 无上述标签，且 issue 已关闭 | `Done` | PR 合并后 issue 自动关闭 | `brain/autopilot.md:11-12` |

补充约定：

1. **同一时刻只保留一个 lane 类标签。** 换标签时用 `--add-label` + `--remove-label` 一起做，避免
   一个 issue 同时带 `lane-ready` 与 `in-progress`，看板与队列口径分裂。
2. **不映射到 Status 的标签：** `p0|p1|p2`（优先级）与 `backend:pi|agy|codex`（选路维度）只影响派活
   顺序与落到哪台主机（`brain/autopilot.md:11`、`brain/autopilot.md:17`），不要拿来当看板状态用。
3. **没打 lane 类标签的 issue**（例如只写了 `p2,backend:pi` 的 backlog 条目）建议留在 `Todo` 或不入板，
   不要占用 `In Progress` / `In Review`。

### 3.3 与 openswarm issue bus 标签的区别

`openswarm:queued` / `openswarm:claimed` / `openswarm:done` / `openswarm:failed` /
`openswarm:cancelled` 是 issue bus 协议的显示标签，作用在 bus 仓的卡片 issue 上
（`openswarm/docs/BUS.md:65`、`openswarm/bin/openswarm:5951`），与本看板的 `lane-*` 三标签**不是同一套**，
不要互相替换。`brain/dual-mode-status.md:26` 已写明这条边界：`lane-ready → autodispatch → PR` 是受控
SSH lane 的闭环，不是 `github-issue` bus 协议。

---

## 4. 手工操作参考

```sh
# 建缺失的标签（幂等：已存在时加 --force 更新颜色与描述）
# 颜色沿用 openswarm issue bus 状态标签的同一套色板（openswarm/bin/openswarm:6460）
gh label create in-progress --description "已被 lane 领走，执行中" --color fbca04 --repo surlymochan/phanes
gh label create lane-pr     --description "已开 PR，等待审查合并" --color 1d76db --repo surlymochan/phanes

# 换标签（一次只留一个 lane 类标签）
gh issue edit <N> --repo surlymochan/phanes --add-label in-progress --remove-label lane-ready

# 把 issue 放进看板
gh project item-add 6 --owner surlymochan --url https://github.com/surlymochan/phanes/issues/<N>

# 改看板 Status
gh project item-edit 6 --owner surlymochan \
  --url https://github.com/surlymochan/phanes/issues/<N> \
  --field Status --value "In Progress"
```

命令使用注意（均来自本机 `gh` 的 `--help` 输出）：

- `gh issue edit` 的 `--add-project` / `--remove-project` 接受的是**项目标题**而不是编号；按编号加看板
  请用 `gh project item-add`。
- `gh project item-edit` 对非草稿 issue **每次调用只能更新一个字段值**；同一 issue 的多个字段要分多次调用。
- `gh project item-edit --url` 传的是 issue/PR 的 URL，不是项目 URL；`--owner` 才决定改哪个项目。

---

## 5. 现状与边界

- **本文件只是口径文档，不是自动化实现。** 仓库内没有把 `lane-*` 标签同步到看板 Status 的脚本；
  `lane-autodispatch` 与 `lane-merge` 跑在 box 控制面上（`~/phanes-ctl/bin/...`），不在本仓库
  （`brain/autopilot.md:11-12`、`brain/autopilot.md:39-40`）。要自动化，得改 box 侧脚本。
- `brain/dual-mode-status.md:24` 的「与 phanes Project / lane-ready 标签体系衔接说明」条目即本文；
  issue bus 一侧的落地仍是 #238 / backlog rsi-08。
- 看板是视图，issue 才是队列真相来源；看板状态与标签冲突时，以 issue 标签为准并修正看板。

---

## 6. 引用来源

- `README.md:8` —— 看板 URL、看板名称与仓库入口关系
- `brain/autopilot.md:3` —— 主看板 URL；`:11-12` —— 派活/合并两个常驻循环；`:17` —— 建 issue 的标签口径；
  `:35` —— 加任务方式；`:39-40` —— box 上手动启动循环
- `brain/dual-mode-status.md:3` —— Project URL；`:15` —— `lane-ready` 作为派活入口；`:24` —— 衔接说明条目；
  `:26` —— 与 issue bus 的边界
- `openswarm/docs/BUS.md:65`、`openswarm/bin/openswarm:5951` —— issue bus 状态标签
- `openswarm/bin/openswarm:3626`、`:7384`、`:7698` —— lane/run 的 `running`、`busy` 状态写入点
- `openswarm/bin/openswarm:6460` —— issue bus 状态标签的色板（本文标签颜色沿用）
