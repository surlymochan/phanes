# Issue #7 E2E 状态（#1334）

检查日期：2026-11-02。检查范围：当前 checkout 中的 CI 配置与 E2E 脚本，以及 Issue #7 提供的独立 main 运行记录。CI workflow 的 `push` 目标包含 `main`（`openbot/.github/workflows/ci.yml:3-8`）。Issue 讨论明确记录 main commit `fd5719fc7f1880b6898f0f6091f69f324ce4ef61` 与 example-only head 出现相同失败；该 main run 的详细日志未随 checkout 提供，故根因仍需按日志确认。

| 场景 | 是否接入 CI | job 与证据 | 最近 main 是否通过 | 本地运行 |
| --- | --- | --- | --- | --- |
| `steer-interrupt-pause` | 是 | `Team API and reverse actions E2E`（`openbot/.github/workflows/ci.yml:317-325`）；步骤 `bun run test:steer-interrupt-pause`（`:400-402`）；脚本由 `openbot/package.json:298` 注册。报告上传路径也已列出（`:592-613`）。 | 已确认失败：Issue 讨论报告 main 与 example-only head 均在 `steer-interrupt-pause-e2e.ts:270` 失败（queued message 在 pause 时完成）。但给出的 main 记录没有 provider/调度器日志，不能据此归因产品代码。当前脚本也包含覆盖队列等待的断言（`openbot/scripts/steer-interrupt-pause-e2e.ts:260-295`）。 | 无法运行：本环境没有 `bun`（`bun: command not found`）。 |
| `backup-model-failover` | 是 | 同一 job（`openbot/.github/workflows/ci.yml:317-325`）；步骤 `bun run test:backup-model-failover`（`:472-474`）；脚本由 `openbot/package.json:260` 注册。报告上传路径已列出（`:630-631`）。 | 已确认失败：Issue 讨论报告 main 与 example-only head 均在 `backup-model-failover-e2e.ts` 的 `usage-work` case 超时。当前 case 预期 primary 报 usage limit 后 backup 继续同一消息（`openbot/scripts/backup-model-failover-e2e.ts:736-765`）；main run 的详细日志不可用，无法确认旧 fake event 假设或产品逻辑为根因。 | 无法运行：本环境没有 `bun`（`bun: command not found`）。 |
| `queue-window` | 否 | 脚本存在于 `openbot/scripts/queue-window-e2e.ts:1-9`，但 `openbot/package.json` 没有 `test:queue-window`，`openbot/.github/workflows/ci.yml` 也没有对应 run step 或 artifact 路径。 | 不适用：未接入 CI，无法从 main CI 判断通过。 | 已尝试：`bun scripts/queue-window-e2e.ts`；失败于缺少 `@openbot/contracts/ipc`（worktree 没有 `node_modules`）。 |

## queue-window 接入所需的最小改动

不修改本次任务中的 workflow。后续若接入，只需在 `Team API and reverse actions E2E` job（`openbot/.github/workflows/ci.yml:317-325`）增加一个步骤，运行：

```yaml
- name: Run the queue window E2E test
  if: ${{ !cancelled() }}
  run: bun scripts/queue-window-e2e.ts
```

如需在 CI 中保留脚本生成的报告，再把 `.openbot-build/queue-window-e2e/` 加入该 job 的 artifact `path`；这不是运行测试所需的最小改动。

## 结论

`steer-interrupt-pause` 与 `backup-model-failover` 在 Issue 所引 main run 上均已确认失败；不能将其描述为尚未复现。当前可读到的源码不足以证明新的产品或测试缺陷：interrupt 用例断言 queued delivery 应等待（`openbot/scripts/steer-interrupt-pause-e2e.ts:280-295`），而 backup 用例断言 failover continuation（`:736-765`）；缺少 main run 报告和本地 Bun 运行环境，无法安全定位或修复根因。`queue-window` 尚未接入 CI；当前仅有本地 E2E 脚本，最小接入方式见上文。
