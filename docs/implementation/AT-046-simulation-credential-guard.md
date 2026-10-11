# AT-046 前置：模拟世界不能借用真实效应凭据（v0.1 回归负例）

- **分支与迁移**：分支 `at046-sim-credential-guard`，从 `nx037-driver` `404b126` 切出；迁移 `0320_at046_real_credential_alone.sql`（临时号）。
- **依据**：
  - 14_TEST_ACCEPTANCE AT-046：Twin 调用真实消息 Action 时服务端拒绝，即使模型伪造 mode；
  - ROADMAP §10：“模拟世界不能借用真实效应凭据”先作为 v0.1 回归负例落地。
- **范围**：本次不实现模拟世界本身的功能，只核对、补强并用测试固定以下两点：
  - world 不是 `real` 的调用（simulation、shadow，或伪造 world 字段），在每个能产生外部效应的入口都被拒绝；
  - 这类调用不可能取得真实凭据。

## 1. 各入口的检查点

下表的文件路径相对于 `packages/eios-core/src/`。

| 入口 | 检查点 | 非 real world 的结果 |
|---|---|---|
| 服务凭据认证（所有服务进程） | `eios/migrations/0004_service_authority_facts.sql:82`（`p_world=any(worlds)`）；`nexloop_eios/authorization.py` 中 `authenticate_service` | `service authentication denied` |
| 同一凭据跨 world（**本次补强**） | `eios/migrations/0320_at046_real_credential_alone.sql`：凭据只要含 `real`，就只能含 `real` | 写入时 CheckViolation `nexloop_service_credentials_real_alone` |
| 效应执行（effect worker） | `nexloop_eios/effect_worker.py:33`（`--world` 只能是 `real`）；`nexloop_eios/effect_execution.py:94`（会话 world 不是 real 时，不打开连接就拒绝）；`eios/migrations/0042_effect_execution_ledger.sql:165`（`real effect world required`） | 参数错误（退出码 2）、`EffectExecutionUnavailable`、42501 |
| 本地 JSON 投递 | `nexloop_eios/local_json_delivery.py:89`（固定以 `real` 认证）；`eios/migrations/0051_local_json_delivery.sql:24` | 42501 `local delivery denied` |
| 消息 Run 签发（runtime dispatch） | `eios/migrations/0049_message_run_issuance.sql:3,9`（表约束 `world='real'`）、`:37`；`eios/migrations/0063_role_runtime.sql:27` | 42501 `message relay unavailable` |
| Runtime worker 与 guard（**本次补强**） | `nexloop_eios/runtime_worker.py:81`、`nexloop_eios/runtime_guard_worker.py:42`：`--world` 只能是 `real` 或 `test`（原来接受任意字符串） | 参数错误（退出码 2），在读取凭据文件之前 |
| 真实派发开关 | `eios/migrations/0153_real_dispatch_switch.sql:128`（非 real world 不能打开开关）、`:151`（`closed_layer` 对非 real world 返回 `world`）、`:195-197`（外部效应被拒绝，拒绝码 NXC07） | 其余各层全开时仍返回 `world` |
| 优惠发放 | `nexloop_eios/background_services.py:78`（incentive-issuer 的 `--world` 只能是 `real`）；`eios/migrations/0153_real_dispatch_switch.sql:209` 起（发放命令，外部效应同样经开关判定） | 参数错误；42501 |
| 出站记录与原生 WebChat 发送 | `nexloop_eios/outbound_messages.py:75`；`eios/migrations/0079_nx047_outbound_messages.sql:14`（表约束）、`:154` | 参数错误；42501 `outbound message unavailable` |
| 效应意图提交 | `authz.nexloop_effect_intent_command`（经凭据认证） | 42501 |

## 2. 测试 `tests/test_at046_simulation_isolation_pg.py`（8 个，干净 catalog）

凭据都来自仓库的 service-grants manifest，经受信配置写入，与部署方式相同。

1. **认证**：每个真实服务主体的凭据，在 world `real` 下能认证；在 `simulation` 和 `shadow` 下认证被拒绝。
2. **跨 world 凭据**：同时含 `real` 和其他 world（`simulation`、`shadow`、`test`）的凭据，新增和修改都被拒绝；库中不存在这样的凭据。
3. **伪造 world 调用各入口**：以测试夹具直接写入开关行，打开三层开关（部署层设置、租户开关 data_mode real、Action 开关）。然后用各入口应有的数据库角色和真实凭据，以 `simulation` 或 `shadow` 调用 6 个入口，全部被 42501 拒绝：
   - 本地投递、消息 Run 签发、出站消息：断言 world 检查对应的拒绝文字；
   - 效应执行、效应意图、优惠发放：凭据层先拒绝。
   - 同时断言 `closed_layer` 在 real world 下返回 null（全开），在伪造 world 下返回 `world`；
   - 意图、执行尝试、出站消息、优惠发放各表都没有任何记录，即没有发生任何外部调用。
4. **EffectExecutionPort**：会话 world 为 `simulation`、`shadow` 或 `test` 时，在打开任何连接之前就拒绝。
5. **进程参数**：effect worker、outbound、incentive-issuer、commercial-recorder、reply-guarantor、runtime worker、guard 都在参数解析阶段拒绝 `simulation` 和 `shadow`（退出码 2）。传入的凭据路径都不存在，因此可以确认拒绝发生在读取凭据之前。

## 3. 已知限制（v0.1 不覆盖）

- **非 real world 中的效应意图**：在 simulation world 中仍能记录效应意图（0040 的提交函数和意图表都没有 world 约束）。安全依赖派发和执行时的拒绝（0042:165、0153），以及凭据不能跨 world。本次没有加意图层面的约束，因为将来的模拟世界需要记录模拟意图。
- **只读查询 Action 的豁免**：`nexloop.service.query` 在 0042:165 不受 world 检查。它是只读查询，前提是它不能有外部副作用。
- **外部效应声明**：Action 是否为外部效应（0153 `external_effect`）按 (tenant, world) 由受信配置声明，未声明时视为外部效应。如果配置者在 simulation world 把某个 Action 声明为非外部效应，开关就不再判定该 Action。这类声明只应用于回环或原生渠道，且只有 configurator 角色能写。
- **模型凭据**：Agent Host 的模型凭据不区分 world。本次只保证 runtime worker 不处理 simulation 或 shadow 的 Run。模拟世界的模型调用用什么凭据，属于模拟世界功能本身，不在本次范围内。
- **双向调用的情形**：AT-046 原文要求 Twin 调用真实消息 Action 时被拒绝，这需要 Twin/模拟 Run 存在。本次只覆盖服务端入口的拒绝，没有覆盖 Twin 本身。
