# Issue #21：模型额度限制作用域与未知状态处理核实

范围：`openswarm/bin/openswarm`（行号为核查时的版本）。`openbot/` 下的 quota 字样均为 UI/i18n/账号用量展示，与路由切换无关，未纳入。

## 1. 额度/限流状态的追踪位置

| 作用域 | 位置 | 说明 |
|---|---|---|
| 熔断器存储 | `breakers()` / `trip_breaker()`，2041–2049 | 键 → 过期时间，时长 `thresholds.breaker_minutes`（30） |
| 键生成 | `rate_limit_breaker_key()`，5092–5101 | codex→`codex/limit/<limit_id>`；ocgo→“usage limit exceeded”时 `ocgo/all`，否则 `ocgo/<model>`；deepseek→`deepseek/all`；其他→`<backend>/<model或all>` |
| 规划时读取 | `plan_codex` 2846–2849、`ocgo_pick` 2895–2903、`plan_deepseek` 2942、`plan_copilot` 2986、`plan_cursor` 3004、`plan_agy` 3068、`plan_pi` 3118 | 均按上述键检查 |
| 实际额度 | codex 窗口 `limits[limit_id]`（2860–2881）、ocgo `usage`（2906）、deepseek `balance`（2940） | |
| 单车道 agy | `lane_write_state(quota_model, quota_until)`，8100–8150、8237、8335 | 仅按 `quota_model == lane model` 比对，作用域为模型 |

## 2. 入口核查结论

- **作用域保持**：codex 以 limit_id（模型族共享额度）和单模型两层键过滤候选（2846–2849）；ocgo 区分整个套餐与单模型；其余后端按模型。键粒度与上游错误语义一致，未发现跨作用域误伤或漏拦（测试覆盖，见 §4）。
- **限流后的切换（`cmd_run`，5267–5290）**：
  - `pi/codex/cursor/agy` 遇 `rate_limit` 直接结束，**不**切换到同账号其他模型/后端（5275–5277）。
  - 其他后端 `rate_limit` 时 `exclude=tried`，已尝试后端不会再选，不会同账号反复切换；`model_unavailable` 同后端换模型，但每次都 `trip_breaker(<b>/<model>)`，候选有限，终止。
  - 同一后端内重试受 `rate_limit.max_retries`（4）与预算截止时间约束（3698–3785，且仅在可 resume 时重试）。
- **未知状态**：
  - deepseek 余额未知（`balance is None`）→ 不可用（2938–2939），不会作为付费兜底。
  - 付费兜底：`route()` 只遍历 `routing.default_order`（`prioritize` 仅重排，不增加，3197–3235）；默认策略仅 `["codex"]`（`policy.json:74`），deepseek/ocgo 需显式加入或 `--backend` 强制。故未知状态不会触发未批准的付费回退。
  - codex 无近期 rollout（额度未知）→ 使用默认并记录提示（2881）。这是“未知即放行”，但有界：一次 429 即 `trip_breaker(codex/limit/…)` 且不再切换（见上）。
  - cursor/agy/pi 额度恒为未知（`quota: None`），同样靠 429 熔断，每模型 30 分钟。

## 3. 发现的缺口（未改动，建议跟进）

1. `exec_error` 回退使用 `exclude=[b]` 而非 `tried`（5284），A→B→A 的循环仅被 120 秒剩余预算与 `deadline` 限制，无次数上限。非额度专属，修改会影响通用回退语义，故未动。
2. `lane` 路径的 agy 额度恢复每次探测写 `quota_until=NOW()+3600`（8238、8338），探测失败是否会无限循环依赖 `agy_quota_available` 的返回，未做运行期验证。
3. 熔断器是全局文件，不区分账号；多账号场景无账号维度（当前实现为单账号假设）。
4. `ocgo_pick` 的 `max(m["usage"].values())`（2906）在 `usage` 为空字典时会抛 `ValueError`；`probe` 当前总是填充，未发现可触发路径。

## 4. 新增测试

`openswarm/tests/test_quota_scope.py`（在 `openswarm/` 下运行 `python3 -m unittest tests.test_quota_scope`）：键作用域（codex limit_id、ocgo 全局/单模型、模型缺省）、熔断仅影响对应键、deepseek 余额未知不被选、默认顺序下 codex 限流后不会回退到 deepseek、codex 单 limit 熔断只屏蔽该 limit、agy 未知额度后端按模型熔断。与 `tests/test_route.py` 一起运行共 27 项通过。

未修改任何生产代码。
