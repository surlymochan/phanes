# Issue #7 E2E 状态（#1334）

检查日期：2026-11-01。检查范围：当前 checkout 中的 `main` CI 配置与三个 E2E 脚本。CI workflow 的 `push` 目标包含 `main`（`openbot/.github/workflows/ci.yml:3-8`）。本地 checkout 没有 GitHub Actions 运行记录，因此“最近 main 上是否通过”只能记录为“未确认”，不能据此声称已不复现。

| 场景 | 是否接入 CI | job 与证据 | 最近 main 是否通过 | 本地运行 |
| --- | --- | --- | --- | --- |
| `steer-interrupt-pause` | 是 | `Team API and reverse actions E2E`（`openbot/.github/workflows/ci.yml:317-325`）；步骤 `bun run test:steer-interrupt-pause`（`:400-402`）；脚本由 `openbot/package.json:298` 注册。报告上传路径也已列出（`:592-613`）。 | 未确认。workflow 只证明该步骤会在 main push 运行，当前 checkout 没有最近运行结果。不能写“已不复现”。 | 已尝试：`bun run test:steer-interrupt-pause`；失败于缺少 `@openbot/contracts/ipc`（worktree 没有 `node_modules`）。 |
| `backup-model-failover` | 是 | 同一 job（`openbot/.github/workflows/ci.yml:317-325`）；步骤 `bun run test:backup-model-failover`（`:472-474`）；脚本由 `openbot/package.json:260` 注册。报告上传路径已列出（`:630-631`）。 | 未确认。workflow 只证明该步骤会在 main push 运行，当前 checkout 没有最近运行结果。不能写“已不复现”。 | 已尝试：`bun run test:backup-model-failover`；失败于缺少 `@openbot/contracts/ipc`（worktree 没有 `node_modules`）。 |
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

`steer-interrupt-pause` 与 `backup-model-failover` 已接入 main CI，但本地证据不足以确认最近 main 通过，因此本次不判定“已不复现”。`queue-window` 尚未接入 CI；当前仅有本地 E2E 脚本，最小接入方式见上文。
