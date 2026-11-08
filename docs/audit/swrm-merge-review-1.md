# swrm-merge 第 1 轮修复证据

REPORT: blocked — 评审列出的代码问题已修复；隔离全门禁仍有 2 项既有 guardrails 错误，干净提交副本复现，不能报告 CI 全绿。

## 逐项修复

| 级别 / 问题 | 修复 | 验证 / 复现命令 |
| --- | --- | --- |
| BLOCKER：删除模块的可执行调用方 | 删除依赖旧 dispatcher 快照协议的 fleet 页面和离线收割回放工具，以及旧页面文档；清理 AD 注释及说明中的失效模块链接。保留的 AD 过滤器由自身测试覆盖。日常状态使用 swrm 原生 `status` / `lane status`。 | `rg -n 'swrm/lib/dispatch/(fleet_status|harvest_filter)\.py' tools docs` 无匹配；`python3 -m unittest discover -s tools/lane-autodispatch -p 'test_*.py' -v`：33 项通过。 |
| BLOCKER：发布脚本及其他部署信息 | `pub2.sh` 改为读取环境变量并传递远端环境，使用同目录脚本；其他发布、PR 生成、运行监视脚本移除本机路径和部署主机默认值。桥接脚本显式配置目标；容量协议使用通用逻辑角色。历史文档及测试样例中的部署标识脱敏，提供本地未跟踪配置及无真实值示例。 | `python3 -m unittest discover -s tools/tests -p test_publish_config.py -v`：两个脚本 × 默认/显式目标的 4 个离线场景通过；相关 shell 文件 `bash -n` 通过。扫描 7179 个现存 Git 跟踪文本文件，来自修复前部署脚本/配置的已知部署标识匹配为 0。该扫描不是完整密钥审计。 |
| MAJOR：上游基线不符且不可核对 | 只读解析指定上游 a4ff878，更新完整 SHA；列出超出纯命名替换的逐文件差异和测试方法增删。保留的 PyPI 发布与 stub 四文件显式列入 `phanes_only`。 | `python3 tools/check-swrm-upstream.py "$SWRM_UPSTREAM_REPO" --ref a4ff878`；结果见 [逐文件清单](swrm-merge-upstream-diff.json)。 |
| MAJOR：旧目录残留 `.so` | 删除旧目录下全部四个二进制；补充旧路径忽略规则，防止再次纳入。 | `git diff --name-only --diff-filter=D -- openswarm/`：四项；`find openswarm -type f`：无输出。删除尚未提交，因此索引仍记录旧文件，工作树已删除。 |
| MAJOR：本地 lane 被误判为远端 | 按发现来源记录 host；本机同名 lane 优先，包括 busy 状态；本机 A1/C1/GH1/GH2 使用本地 assign，远端即便叫 K1 也走远端。M1 只读 one-shot 限于本机。 | `python3 -m unittest discover -s swrm/tests -p test_fleet_auto.py -v`：17 项通过，含无远端配置的 dry-run/assign、同名 busy 覆盖、远端 K1；均使用 mock，不实际派发。 |
| MINOR：固定 PR 仓库 | 两个发布脚本默认由 gh 在 `PHANES_LOCAL_REPO` 内解析仓库，可通过 `PHANES_PR_REPO` 显式覆盖。 | 上述 `test_publish_config.py` 记录最终 gh argv 和 cwd，4 个场景通过；无提交、推送或网络请求。 |

## 上游一致性与测试差异

指定基线：`a4ff878970ad3978058ee47cc62af6851dbae9e1`。
使用现存文件优先、文件改名兜底的路径映射，并对源码小写 `openswarm` → `swrm`
做归一化；不会把兼容环境键当成新命名键。

| 分类 | 文件数 | 说明 |
| --- | ---: | --- |
| 字节一致 | 238 | 保留上游内容 |
| 仅命名替换 | 65 | 包含主 CLI 与 guardrails 测试，未引入语义差异 |
| 其他修改 | 26 | 文档、部署配置、桥接、自动路由及测试，清单逐项列出 |
| 删除 | 594 | 全部位于 bench/swebench/results 的 raw / raw-deepseek 运行产物目录 |
| 新增 | 4 | PyPI 发布 workflow 与 stub 三文件 |

修改过的 Python 测试文件中，AST 测试方法比较没有删除方法；相对指定上游，
`test_fleet_auto.py` 新增 4 项，`test_hub_bridge.py` 新增 2 项。
清单记录方法名及前后数量；方法数量相同不代表断言字节相同。
未单独运行上游完整套件，不宣称上游与本地执行结果一致。

## 实际验证

所有本轮验证设置 `TMPDIR=$PWD/.tmp/test-tmp`，`OPENSWARM_HOME` 指向 `.tmp` 下的隔离状态目录。
完整门禁额外通过自身脚本隔离 HOME、配置与缓存。

| 命令 | 结果 |
| --- | --- |
| `BASE_SHA=HEAD bash tools/ci-swrm.sh` | **退出 1**：414 项，2 errors、3 skipped，其余通过；在单测阶段停止，后续步骤另行运行 |
| `python3 -m unittest discover -s tools/tests -p 'test_*.py' -v` | 146 项通过 |
| `python3 -m unittest discover -s tools/lane-autodispatch -p 'test_*.py' -v` | 33 项通过 |
| `python3 -m unittest discover -s swrm/tests -p test_fleet_auto.py -v` | 17 项通过 |
| `python3 -m unittest discover -s swrm/tests -p test_hub_bridge.py -v` | 17 项通过，覆盖 local、配置远端、未知 host 拒绝及实际 SSH argv 目标 |
| `python3 tools/check-dual-hosts-ok.py` | 通过，离线假 SSH |
| `python3 tools/check-fleet-batchmode-probe.py` | 通过，离线假 SSH |
| `bash tools/check-dual-mode-ssh-doc.sh` | 通过 |
| `bash tools/check-lane-status-paths.sh` | 通过，未开启 probe |
| 其他 11 个受影响离线契约脚本 | ad-ssh-command、ctl-hb-refresh、fleet-unknown-host、ghost-in-progress、health-hosts-ok-pause、health-pause、ib-cooldown-skip、ib-cooldown-status、ib-need-ok、pause-set-cooldown、soak-nine 最终均通过。首次 cooldown-status 暴露脱敏后动态夹具名称不一致，已同步修复并定向复跑通过 |
| `python3 swrm/bin/swrm status` / `route --type impl --difficulty 3` | 设置 `OPENSWARM_STATE_FILE=$PWD/swrm/examples/demo-state.json`，均退出 0；route 返回 `ok: true` |
| `bash -n tools/pub.sh tools/pub2.sh tools/pushbr.sh tools/dropexport.sh tools/watch8.sh` | 通过 |
| `git diff --check` / JSON 解析 / 变更 Python AST 解析 / `git check-ignore .deployment.env` | 通过 |

### 阻塞复现

修复前提交：`9994183f38cf05f0e5dafc2a6ed407a647852bf0`。
将其归档到本工作树 `.tmp/baseline` 后，以同样的隔离 TMPDIR 复跑：

```sh
mkdir -p .tmp/baseline .tmp/baseline-home .tmp/test-tmp
git archive 9994183f38cf05f0e5dafc2a6ed407a647852bf0 swrm | tar -x -C .tmp/baseline
TMPDIR="$PWD/.tmp/test-tmp" OPENSWARM_HOME="$PWD/.tmp/baseline-state" \
  HOME="$PWD/.tmp/baseline-home" python3 -m unittest discover \
  -s .tmp/baseline/swrm/tests -p test_guardrails.py -v
```

15 项中复现同样的 2 errors：

- `test_enforced_risks_all_protocols_and_policy_opt_out`：第 125 行，`enforced_risk()` 返回 None。
- `test_rm_block_records_only_path_class`：第 189 行，同样返回 None。

两处都以 `TypeError: 'NoneType' object is not subscriptable` 结束。
guardrails 实现和测试未在本轮修改；需要独立处理工作树内临时夹具与 Git 根目录识别的关系，随后重跑完整门禁。
完整原始日志保留在本地 `.tmp/ci-swrm.log`、`.tmp/baseline-guardrails.log`，不纳入公开仓库。

## 部署侧边界

没有执行真实派发、发布、提交、推送、分支切换或存量状态迁移。
`OPENSWARM_HOME` 显式覆盖仍受支持；默认目录迁移不自动移动旧数据。
真实旧状态迁移、SSH forced-command 的新逻辑角色映射与实际发布需要部署侧验收，
配置及迁移说明见 [部署脚本配置](../../tools/deployment-scripts.md)。
