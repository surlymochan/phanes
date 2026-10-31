# Issue #21 / #110 调研审查：OpenBot 模型额度作用域与回退机制现状梳理

> **审查范围**：`openbot/` 后端、契约及调度链路中的额度、限流、429、模型家族、账号作用域与回退决策。  
> **关联 Issue**：Refs #21, Refs #110  
> **性质**：只读调研与现状梳理，不修改任何业务生产代码。

---

## 1. 额度/限流相关的核心类型与传播路径

在 `openbot/` 代码库中，模型额度限制并非仅停留在 UI/i18n 展示层，而是一套贯穿**契约层**、**驱动层**、**生命周期层**、**门禁层**与**调度编排层**的完整状态机。

### 1.1 核心类型定义（文件 + 行号）

1. **契约层（IPC & 状态快照）**：
   - `openbot/packages/contracts/src/ipc-agent-runtime.ts`
     - `AccountUsage`（L148–158）：账号全局用量读数，包含 `limits: readonly AccountRateLimitWindow[]` 与 `usageLimitedModels: readonly UsageLimitedModel[]`。
     - `AccountRateLimitWindow` / `UsageLimitWindow`（L112–118）：单个配额窗口，包含 `usedPercent`、`resetsAt: number | null`（Unix 秒）以及 `windowSeconds`。
     - `AgentRuntimeSnapshot.usageLimits`（L171–174）：向前端广播的挂起 Agent 列表及对应的预期重置时间戳 `{ agentId: string; resetsAt: number | null }[]`。
     - `AgentRuntimeSnapshot.usageLimitedModels`（L175–183）：受限的 provider/model/providerAccountId 三元组列表。
   - `openbot/packages/contracts/src/ipc-agents.ts`
     - `AgentBackupModels`（L28–31）：备用模型配置 `{ automatic: boolean; order: readonly AgentModelChoice[] }`。
     - `AgentModelChoice`（L17–21）：模型选择项 `{ provider: AgentProvider; model: string; providerAccountId?: string | null }`。
     - `AgentUsageLimitReturn`（L33–37）：限流切走后记录的原路回退锚点，包含原始模型的 `reasoningEffort` 与可选的 `cause?: "provider-error"`。
     - `UpdateAgentInput`（L153–167）：包含 `backupModels` 与 `leaveUsageLimit?: boolean`（标记本次模型切换是否因限流暂离）。
   - `openbot/packages/contracts/src/ipc-conversation-events.ts`
     - `AgentEvent` 中 `type: "usage-limit-reached"`（L48–54）：限流事件广播，携带 `provider`、`resetsAt` 及被 hold 的 `agentCount`。
     - `ChatActionMarker`（L85–97）：时间轴提示标记，包含 `usage-limit-switch`、`provider-error-switch`、`usage-limit-return`、`backup-models-card`。

2. **后端门禁与状态类型（`UsageLimitGate`）**：
   - `openbot/src/backend/agent/usage-limit-gate.ts`
     - `USAGE_LIMIT_METHOD = "openbot/usageLimit"`（L9）：客户端或协议层通知限流的标准内部 RPC 方法名。
     - `UsageLimitKey`（L55–62）：限流键结构 `{ provider: AgentProvider; accountId: string | null; model: string; family: string | null }`。
     - `UsageLimit`（L68–72）：内存中维护的配额限制项，包含 `resetsAt: number | null` 和定时恢复句柄 `timer: ReturnType<typeof setTimeout> | null`。
     - `ProviderFailure`（L64–66）：非配额类的临时 Provider 报错记录（固定退避 10 分钟）。
     - `limitKey()`（L386–388）：限流键序列化函数 `${provider}\0${accountId ?? ""}\0${family ?? ""}`。
     - `modelFamily()`（L395–400）：从模型名解析家族（目前仅包含 `"opus"` 与 `"sonnet"`）。
     - `spentWindowReset()`（L403–413）：从 `AccountUsage` 读数中计算最近且用量达 100% 的窗口最远重置时间。

3. **错误诊断与模式识别**：
   - `openbot/src/backend/agent/provider-diagnostics.ts`
     - `isUsageLimitDiagnostic(message)`（L32–35）：正则匹配 HTTP 429、`resource has been exhausted`、`rate limit`、`quota ... exceeded/exhausted`。
     - `isPlanLimitDiagnostic(message)`（L40–45）：识别按时间周期的套餐限制（支持自动重置）。
     - `isBalanceDiagnostic(message)`（L50–55）：识别充值余额/点数耗尽（不自动重置，不可自愈）。
     - `classifyFailure(error)`（L70–110）：分类并映射为统一原因码（`rate_limited`、`quota_exhausted`、`provider_error` 等）。

---

### 1.2 额度事件与状态的传播路径

从 Provider 底层触发限流到前端感知的全链路传播如下：

```
[Provider 报错/事件]
  │
  ├─ Claude SSE 流: rate_limit_event (claude-client.ts:1211) 
  │    └─ 提取 rateLimitType -> claudeLimitFamily ("opus"|"sonnet")
  │    └─ 触发 notification: USAGE_LIMIT_METHOD (claude-client.ts:1402)
  │
  ├─ Codex AppServer: JSON-RPC error codexErrorInfo === "usageLimitExceeded"
  │
  ├─ Gemini/Antigravity: acp-flavor-antigravity.ts:76 正则捕获流式输出中的配额耗尽文本
  │
  └─ 同步 turn/start 失败: drain-scheduler.ts:924 (isPlanLimitDiagnostic)
         │
         ▼
[TurnLifecycle 通知处理] (turn-lifecycle.ts:730-791)
  │
  ├─ 收到 USAGE_LIMIT_METHOD:
  │    └─ limitedTurns.set(turnId, { resetsAt, family })
  │
  └─ 收到 error 通知且匹配 isPlanLimitDiagnostic:
       └─ limitedTurns.set(turnId, { resetsAt: null, family: null })
         │
         ▼
[TurnLifecycle 结束结算] (turn-lifecycle.ts:904-1048)
  │
  ├─ 检查 turn 是否已产生动作 (running.acted):
  │    ├─ 若未产生且配置了自动备份: 尝试 failover
  │    └─ 若已产生: 尝试携带 workSteps 继续或触发中断/重排
  │
  └─ 调用 UsageLimitGate:
       ├─ 限流触发: usageLimits.reached(agentId, resetsAt, model, family) (L1030, L1047)
       └─ 若成功完成: usageLimits.completed(agentId, model) (L1048)
         │
         ▼
[UsageLimitGate 状态机] (usage-limit-gate.ts:191-280)
  │
  ├─ 计算复合键: limitKey(provider, accountId, family)
  │    ├─ family 为 null 时: 锁死该 provider+accountId 下的所有 Agent
  │    └─ family 非 null 时: 仅锁死同 family 的模型 (如仅锁定 Opus)
  │
  ├─ 定时器管控:
  │    ├─ 有 resetsAt: arm 延迟到 resetsAt + 30s 宽限期后触发释放
  │    └─ 无 resetsAt: arm 15分钟后触发主动探测 (readUsage)
  │
  └─ 触发钩子: hooks.emitRuntimeSnapshot() 与 hooks.scheduleDrain(agentId)
         │
         ▼
[DrainScheduler 与 AgentService 协同]
  │
  ├─ 轮询到受限 Agent 时调用 followUsageLimit (drain-scheduler.ts:405)
  │
  └─ AgentService.#usageLimitMove (agent-service.ts:2976):
       ├─ 若存在 usageLimitReturn 且原模型已就绪: 安排回退 ("return")
       └─ 若当前模型受限且开启 automatic: 从 backupModels.order 选取首个可用模型切走
```

---

## 2. 从报错到切换/回退的决策流程与作用域审查

### 2.1 决策流程文字图

```
                [Turn 运行中发生错误 / turn/start 失败]
                                   │
                                   ▼
                   是否命中配额/套餐限流诊断规则？
                   (isPlanLimit / codexErrorInfo / USAGE_LIMIT_METHOD)
                         │                  │
                        YES                 NO ──> 是否属于通用 Provider 临时故障？
                         │                         (FAILOVER_CAUSES)
                         │                               │
                         │                              YES ──> 标记 failedOver
                         │                                      记录 ProviderFailure (10min)
                         ▼                                      重排该 Turn 并调度备用模型
       判断是否有已执行的不可逆动作 (running.acted)
           │                                │
          NO                               YES
           │                                │
    可无损重新入队 (requeue)        是否允许携带 workSteps 跨模型恢复？
           │                        (mayFailOver + channel/task delivery)
           │                                │               │
           │                               YES              NO
           │                                │               │
           ▼                                ▼               ▼
    调用 mailbox.restoreQueued         生成上下文摘要      转入 interrupted / planStopped
    保留消息与调度状态                 将当前中间步骤      在界面弹出 Usage Limit Notice 卡片
                                       注入重排队列        等待人工介入或重置到期
                         │                  │
                         └─────────┬────────┘
                                   │
                                   ▼
             调用 UsageLimitGate.reached(agentId, resetsAt, model, family)
                                   │
         ┌─────────────────────────┴─────────────────────────┐
         ▼                                                   ▼
   [全账号级作用域]                                     [模型家族级作用域]
   (family == null)                                    (family != null)
   锁定: provider + accountId 下所有 Agent             仅锁定: provider + accountId + family 下 Agent
   其他 Model (如 Sonnet) 同步被 hold                  同账号其他 Family (如 Sonnet) 保持放行正常消费
         │                                                   │
         └─────────────────────────┬─────────────────────────┘
                                   │
                                   ▼
                 DrainScheduler 触发并判定切换 (followUsageLimit)
                                   │
               Agent 是否配置了 backupModels.automatic == true？
                         │                  │
                        YES                 NO
                         │                  │
                         ▼                  ▼
     遍历 backupModels.order 备选模型:      保持 hold 状态，等待:
     1. provider 状态 connected/available   1. 到达 resetsAt + 30s 宽限期自动解开
     2. account 处于 connected              2. 15 分钟探测周期拉取配额
     3. model 未被 isLimited / isFailed     3. 用户手动在前端卡片点击切换或购买额度
     找到首个可用备选 -> 触发切换
     并在 Agent 身上持久化 usageLimitReturn
                                   │
                                   ▼
                [未来某一时刻: 收到 Turn 成功或配额恢复事件]
                                   │
         ┌─────────────────────────┴─────────────────────────┐
         ▼                                                   ▼
   [成功运行 Turn: UsageLimitGate.completed]           [定时重置到期 / 探测成功: #release]
   1. 解除该 provider+accountId 全局 hold              1. 移除 limits 集合中对应 entry
   2. 解除该 family 的 hold (若当前模型有 family)      2. 对原先受限的全部 Agent 触发 scheduleDrain
   3. 解除当前模型的 failed 状态                       3. AgentService 检查 usageLimitReturn:
                                                          若原模型已恢复，自动平滑切回并恢复原推理强度
```

---

### 2.2 作用域在哪些入口可能丢失？

经过对全路径代码逐行排查，发现以下 **6 处明显的作用域丢失或非预期退化点**：

1. **`drain-scheduler.ts:930, 934`（turn/start 同步失败入口）**：
   ```ts
   // drain-scheduler.ts:930
   yield* this.#usageLimits.reached(delivery.recipientAgentId, null, requestedModel);
   ```
   - **问题**：此处调用 `usageLimits.reached` 时，第 4 个参数 `family` 被省略（默认为 `null`）。
   - **后果**：即使 `requestedModel` 是 `claude-opus-4-5`，一旦在 `turn/start` 阶段被拒，作用域会直接退化为全账号锁定（`family: null`），导致同账号下完全不受影响的 Sonnet 任务被错误挂起。

2. **`turn-lifecycle.ts:787–790`（文本诊断错误入口）**：
   ```ts
   if (planLimit && notification.method === "error" && errorTurnId && this.#runningTurns.has(errorTurnId)) {
     this.#limitedTurns.set(
       errorTurnId,
       this.#limitedTurns.get(errorTurnId) ?? { resetsAt: null, family: null },
     );
   }
   ```
   - **问题**：当错误未走专用 `USAGE_LIMIT_METHOD` 而是匹配到 `isPlanLimitDiagnostic(message)` 时，`family` 被硬编码设置为 `null`。
   - **后果**：未尝试根据当前 `turnModel` 提取家族，丢失了模型家族作用域。

3. **`agent-service.ts:1035, 1037`（Channel 领航路由 Turn 报错入口）**：
   ```ts
   failure instanceof GenerationUsageLimitError
     ? this.#usageLimits.reached(lead.id, failure.resetsAt, lead.model)
     : failure.cause instanceof Error && isPlanLimitDiagnostic(failure.cause.message)
       ? this.#usageLimits.reached(lead.id, null, lead.model)
       : Effect.void
   ```
   - **问题**：Channel 协调任务在执行主控路由调用时，遭遇限流仅传入了 `lead.model`，未传入 `family` 参数。

4. **`profile-generation.ts:270–297`（Profile 构造调用入口）**：
   - 在接收 `USAGE_LIMIT_METHOD` 通知时，仅解析了 `resetsAt`，未接收 `params.family`；随后抛出的 `GenerationUsageLimitError` 结构体本身也缺少 `family` 字段，向上层丢失了家族元数据。

5. **`agent-service.ts:2220–2290`（工作流生成与侧边提问入口）**：
   - `generateWorkflowText` 和 `askSideQuestion` 在遇到 `GenerationUsageLimitError` 时，直接 `Effect.mapError` 转换为前端文案，**完全没有上报给 `UsageLimitGate`**，也未在发起前检查 `isLimited`。若工作流步骤中高频遇限，将不断穿透碰撞 Provider API。

6. **`modelFamily` 规则硬编码仅限 Claude 家族**：
   - `usage-limit-gate.ts:395–400` 仅对模型名匹配 `opus` 和 `sonnet`。Codex、Gemini（如 Pro vs Flash 分级配额）、DeepSeek 等其它 Provider 即使底层具有不同配额池，也会全部被抹平为 `family: null` 全账号级锁定。

---

### 2.3 未知状态（Unknown State）如何处理？

系统对不可预知或缺失的元数据设定了明确的降级与兜底策略：

1. **重置时间未知（`resetsAt === null`）**：
   - 见 `usage-limit-gate.ts:301–318`。
   - 降级为 15 分钟定时轮询机制（`USAGE_LIMIT_RECHECK_MS = 15 * 60_000`）。
   - 定时器到期触发 `#fire` -> 调用 `#readReset` -> 通过 Provider 驱动的 `readUsage` 尝试读取最新配额。
   - 若 `readUsage` 返回失败或读数中不再包含已耗尽窗口（例如不支持配额读取的 Provider），则系统假定限流已结束，主动解除 gate（`#release`），允许下一次 Turn 作为探针（Probe）发起实测。

2. **临时 Provider 故障但无重置指示**：
   - 见 `usage-limit-gate.ts:21, 142–161`。
   - 记录于 `#failures` 映射中，施加 `PROVIDER_FAILURE_HOLD_MS = 10 * 60_000`（10 分钟）隔离期。
   - 隔离期内备用模型选择器会避开该 `provider + accountId + model`，但不阻断无备选模型的单 Agent 队列。一旦该模型上有任何一次 Turn 运行成功（`completed`），立刻提前解封。

3. **余额耗尽 vs 周期套餐限流**：
   - 见 `provider-diagnostics.ts:50–55` 与 `turn-lifecycle.ts:1269–1279`。
   - 若错误信息诊断为欠费或余额耗尽（`isBalanceDiagnostic` 为真），系统明确**不将其作为自动限流挂起**，因为余额耗尽不会随时间自然重置。此时直接标记 Turn 失败，提示用户人工干预。

---

## 3. 现有测试覆盖与缺口分析

### 3.1 现有测试文件及覆盖点

1. **`openbot/scripts/usage-limit-account-e2e.ts`**：
   - 测试了内存模式下 `UsageLimitGate` 的基本功能：
     - 账号级限流时，同账号其他模型受阻，跨账号/跨 Provider 模型不受影响；
     - 成功运行一次 Turn 会释放账号级限制；
     - `opus` 家族限流仅阻断 `c-opus`，不阻断同账号 `c-sonnet`；
     - `c-sonnet` 成功不会释放 `c-opus` 的家族锁；
     - 账号级限制与家族级限制复合时，两者的重叠与分步解封逻辑。
2. **`openbot/scripts/backup-model-failover-e2e.ts`**：
   - 测试了遭遇限流或 Provider 报错时，开启 `automatic` 的 Agent 自动在 `backupModels.order` 中按序寻找就绪模型完成 failover，并在解除限流后切回原模型的流程。
3. **`openbot/scripts/channel-usage-limit-e2e.ts`**：
   - 测试了 Channel 协同工作区中，子任务遭遇限流后将任务交还 Channel 队列、避免锁死车道的行为。
4. **`openbot/src/backend/agent/provider-diagnostics.test.ts`**：
   - 正则级单元测试，覆盖了各类限流、配额耗尽与余额耗尽错误信息的文本归类。

### 3.2 测试覆盖缺口

1. **单元测试层缺失**：
   - `UsageLimitGate` 没有任何对应的 `.test.ts` 单元测试，目前仅靠独立 script `scripts/usage-limit-account-e2e.ts` 进行验证。
2. **时间推移与探测未自动化测试**：
   - `resetsAt === null` 时 15 分钟的定时重试、`#readReset` 成功/失败后的 probe 机制，在现有测试中没有使用虚拟时钟（fake timers）进行断言。
3. **入口作用域丢失缺乏防退化测试**：
   - `drain-scheduler.ts` 在 turn 启动失败时调用 `reached` 丢弃 `family` 参数的行为没有任何测试发现或约束。
4. **多厂商家族扩展测试缺失**：
   - 目前所有测试均基于 Claude Opus/Sonnet 双家族假设，缺少多 Provider 扩展场景的测试用例。

---

## 4. 后续独立改进 Issue 建议（3–5 项，含验收命令）

### Issue 建议 1：修复 `drain-scheduler` 与 `turn-lifecycle` 中 model family 作用域丢失

- **目标与改动点**：
  1. 在 `openbot/src/backend/agent/drain-scheduler.ts:930, 934` 调用 `this.#usageLimits.reached` 时，补齐第 4 个参数 `modelFamily(requestedModel)`。
  2. 在 `openbot/src/backend/agent/turn-lifecycle.ts:787–790` 捕获文本限流通知时，从当前 `errorTurnId` 的模型名中提取 `modelFamily(turnModel)`，避免单家族限流退化为全账号锁定。
- **验收命令**：
  ```bash
  cd openbot && bun scripts/usage-limit-account-e2e.ts
  ```

---

### Issue 建议 2：为 `UsageLimitGate` 补充 Vitest 单元测试集（覆盖定时轮询、时间宽限与状态清理）

- **目标与改动点**：
  - 新增 `openbot/src/backend/agent/usage-limit-gate.test.ts`，基于 Vitest + `@sinonjs/fake-timers` 验证：
    1. `resetsAt === null` 触发的 15 分钟定时器及 `#fire` 主动探测分支；
    2. 到期后 `RESET_GRACE_MS`（30 秒）宽限期行为；
    3. `dispose()` 对未决定时器及 Effect Scope 的完整释放，杜绝内存泄漏；
    4. 独立验证 `failed()` 10 分钟临时退避与 `completed()` 提前解除机制。
- **验收命令**：
  ```bash
  cd openbot && bun test src/backend/agent/usage-limit-gate.test.ts
  ```

---

### Issue 建议 3：补齐工作流生成与侧边提问的配额门禁联动

- **目标与改动点**：
  1. 在 `agent-service.ts:generateWorkflowText` 与 `askSideQuestion` 执行前，增加 `this.#usageLimits.isLimited(usageLimitOf(choice))` 前置检查，若受限直接快速失败或等待，避免撞频。
  2. 当 `generateWorkflowText` 捕获到 `GenerationUsageLimitError` 时，调用 `this.#usageLimits.reached` 将限制同步至全局门禁。
- **验收命令**：
  ```bash
  cd openbot && bun test src/backend/agent/workflow-decoding.test.ts
  ```

---

### Issue 建议 4：重构 `modelFamily` 解析器以支持多 Provider 细粒度配额池

- **目标与改动点**：
  - 将 `usage-limit-gate.ts` 中的硬编码 `modelFamily` 抽离为 Provider 驱动能力或注册表机制（如 `ProviderDriver.modelFamily(model: string): string | null`）。
  - 支持将 Gemini Pro vs Gemini Flash、OpenAI o-series vs 4o 等具有独立套餐窗口的模型映射至独立的 `family` 键，防止单个轻量模型触限导致全系列不可用。
- **验收命令**：
  ```bash
  cd openbot && bun scripts/usage-limit-account-e2e.ts
  ```
