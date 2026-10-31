# Issue #34：跨主机 TTL 恢复权威设计

## 结论

将 TTL 的权威时间与回收资格放在 fleet-capacity 协调器：每条 reservation 持有由所属主机通过现有 SSH forced-command 身份认证续期的 lease。协调器只在 lease 过期后接受回收请求，并以事务条件删除精确 reservation。主机本地进程树检查仍用于决定是否尝试回收及停止遗留进程，但不再作为跨主机授权凭据。

不增加服务，不引入签名密钥或明文凭据；复用已有专用 fleet-control SSH key/forced-command 通道。协调器仍是现有 SQLite 数据库及命令。

## 当前依据

- `openswarm/bin/fleet-capacity:72-79`：协调器在本机 SQLite `reservations` 表存储 token、owner、run_id、backend、provider、created，并以 `BEGIN IMMEDIATE` 执行操作。
- `openswarm/bin/fleet-capacity:83-94`：recover 接收调用者提供的 `confirm` 字符串；本机 owner 或 gocc 可据此删除 reservation。该字符串本身不是 liveness 证明。
- `openswarm/bin/openswarm:3377-3418`：回收候选由本地 `status.json` 的 `fleet_reserved_at`、本地 PID 存活检查及 pending 状态构成。
- `openswarm/bin/openswarm:3436-3451`：调用方检查/停止记录的 executor 后，再发送 recover；协调器无法独立确认远端进程已停止。
- `openswarm/README.md:690-699`：现有部署通过专用 fleet-control SSH key 和限制的 forced-command endpoint；forced command 固定认证 owner，执行端记录并复用该 owner。

## 威胁与故障模型

目标是避免网络分区、执行主机重启或恢复程序误判导致仍在工作的 reservation 被其他主机释放。假设：

1. fleet-capacity 主机及其 SQLite 文件、SSH host/key 身份配置可信；攻击者若取得 fleet-control SSH 凭据或协调器主机权限，已越过本设计信任边界。
2. 各参与主机可能崩溃、重启、失联、时钟漂移，命令回复可能丢失；调用可重试且必须幂等。
3. 本地 `status.json`、PID 存活结果和客户端传来的 `confirm` 是非权威输入；不能用它们单独证明另一主机已停止。
4. lease 到期意味着协调器允许回收旧 reservation，不是证明远端进程物理停止。恢复端仍应尽力终止旧 executor；系统不能保证崩溃分区中的旧 worker 不会继续工作。因此 TTL 需大于续期间隔并留出网络抖动余量，业务侧需接受租约过期后的陈旧 worker 风险。
5. 网络分区时续租失败必须 fail closed：保留 reservation、不启动依赖新容量的工作；分区恢复后续租/回收按协调器状态收敛。

## 方案比较与选择

| 选项 | 证据及代价 | 判断 |
| --- | --- | --- |
| 强制命令通道上的签名 heartbeat | 协调器验证签名；需要在各主机安全分发/轮换私钥、管理 key ID 和吊销；SSH 身份已提供主机认证，重复引入签名密钥扩大秘密面。 | 不选 |
| 经现有 forced-command 认证的 lease renewal | 协调器从 SSH 固定 owner 得到认证主体，客户端只能为该 owner/run/token 续租；续租时间由协调器时钟写入，过期与删除由同一 SQLite 事务判断。无新服务或凭据。 | **选择** |

forced-command 传入的 owner 是认证边界；请求体不得覆盖 owner。客户端不能替其他 owner 续租、改写 lease 时间或指定未来时间。

## 数据结构与协议变化

为 `reservations` 增加 `lease_expires REAL NOT NULL`，建议初始值按协调器 `time.time()` + 配置 TTL 生成。可保留 `created` 作为审计/兼容信息，但它不再用于过期决策。TTL 配置应有正数校验；续租周期建议不超过 TTL 的三分之一，并允许调用方重试。

新增 `renew` 操作，输入 `run_id`、reservation `token`；身份来自 forced-command 的 host 参数。协调器在事务内校验 token、host、run_id 精确匹配后，将 `lease_expires` 更新为协调器当前时间加 TTL，并返回新到期时间。未知 reservation 或 owner 不匹配返回拒绝，不创建 reservation。

`recover` 输入精确的 host、run_id、token。仅允许已认证的 owner 回收自身 reservation（operator 修复权限单独沿用既有 gocc 管理路径，但同样必须满足 lease 过期并精确定位记录）。SQL 删除条件包含 token、host、run_id 和 `lease_expires <= coordinator_now`；零行表示尚未过期/记录已变化，绝不报告为已回收。操作在 `BEGIN IMMEDIATE` 事务中串行化，避免 renew 与 recover 的竞态。

`status` 可输出 lease 到期时间，避免暴露 token。现有调用端须传 token；旧的按 host/run_id 批量删除路径不得继续绕过 lease 检查。

## 迁移

1. SQLite 启动时检查旧 schema，并以事务增加 nullable `lease_expires`，避免破坏现有数据库。
2. 对旧行采用保守迁移：填入 `created + configured_TTL`；若缺少有效 `created`，填入当前协调器时间 + TTL，延迟回收而不提前释放。完成填值后约束/代码路径要求非空。
3. 部署顺序先更新协调器（兼容旧 acquire/release/status），再滚动更新 openswarm 客户端启用 renew 与精确 recover。兼容窗口内旧客户端不具备续租，应将旧记录按迁移规则延迟过期，避免旧 confirm 直接删除；拒绝无 token 或未到期 recover。
4. 客户端启动后立即续租并按配置周期续租；每次 acquire 成功及响应丢失后的 reconcile 都必须恢复续租循环。释放仍按 token/host/run_id 精确匹配。
5. 观测至少记录 renew/recover 的拒绝原因、owner/run_id、lease 边界和 SQLite 结果，不记录 SSH 私钥、凭据或 token。无需记录请求中的敏感 payload。

## 测试计划

在 `openswarm/tests/test_fleet_capacity.py` 增加/调整：

- 迁移旧表：有效 `created` 转换为到期时间；缺失时间按保守规则延期；重复启动不重复迁移。
- renew：owner/token/run_id 匹配时延长至协调器时间 + TTL；错误 owner、token、run_id 不改变记录；请求不能指定 owner 或到期时间。
- recover：lease 未到期拒绝且 reservation 保留；过期后仅删除精确 token/owner/run；重复请求幂等；错误身份和 token 不会删除其他记录。
- 并发 renew/recover：事务串行化，结果只能是成功续租且不可回收，或在租约先过期时成功回收，不能发生未到期删除。
- 协调器时钟是 TTL 判断依据：模拟客户端时钟漂移不改变续租/回收结论。

在 `openswarm/tests/test_lane.py` 验证 lane 对续租失败采取 fail-closed、重试恢复后续租继续、runner 崩溃且 executor 清理完成后仅在 coordinator lease 到期时回收。另以隔离的测试 SSH forced-command 配置做端到端协议测试，断言认证 owner 来自 forced command 且请求体伪造 owner 无效；不使用生产密钥或生产协调器。
