# NX-041 S7：消费者端口与 AT-046 完整验收

本次实现依据设计稿 §5、§6、§8 和 §11 S7。新增迁移 `0266_nx041_consumer_port.sql`，编号是临时的，集成时由调度员重排。0260–0263 都没有改动。

## 实现

| 内容 | 位置 |
| --- | --- |
| **契约** `nexloop-consumer-port-event/1`。孪生只能提供四样东西：合成 Consumer、事件种类（message、refuse_contact、payment_intent、visit）、模拟内容（text 和 attributes），以及孪生自己的 version、seed、step。world、事件 ID、序号、内容 digest、世界时间都由服务端给出（`frozen`）。客户端传来的 sim_time、mode、world 或 event_id 一律拒绝。 | `packages/contracts/consumer-port-event.schema.json`、示例、README，以及生成的 TS、OpenAPI、Pydantic 代码 |
| **入站事件表** `runtime.nexloop_sim_consumer_events`。只接受 sim 或 shadow world。每行标记为 data_mode simulation、output_kind simulated_input、origin twin，记录孪生的版本、种子和步数，以及事件到达时服务端从 `runtime.nexloop_world_now` 读到的世界时间（S5）。表是 append-only 的，只能随 world 的整体删除一起删掉（S2）。 | 0266 §1 |
| **观察表** `runtime.nexloop_sim_consumer_observations`，记录 Consumer 被展示了什么：Agent 回复、替身回执或外联。由模拟驱动通过 owner 专用的 `runtime.nexloop_sim_consumer_offer` 写入；孪生通过端口的 `observe` 读取。 | 0266 §2–3 |
| **端口入口** `authz.nexloop_sim_consumer_act` 和 `_observe`。只有 `nexloop_twin_port` 这个新角色能调用，用 sim world 唯一的凭据认证（S2，D10），并沿用 S4 的 `nexloop_sim_effect_tenant`：real 和 test 在任何查询之前就被拒绝。Consumer 必须是本 world 由 S3 生成的合成 Consumer，persona 为 synthetic。事件 ID 由（world、consumer、孪生版本、seed、step）派生：同一 step 重试时重放原结果；同一 step 换了内容报 `step_reused`。 | 0266 §2–3 |
| **Python 端口与孪生桩**。`ConsumerPort` 只接受 sim 或 shadow world，不导入任何网络库或 Action 模块。`TwinStub` 是固定版本、固定种子的确定性孪生，事件只由（seed、consumer、step）决定。真正的孪生在 NX-042。 | `nexloop_eios/consumer_port.py` |
| **Host 进程组**（设计 §6，D3）。新增配置项 `world_group`，默认 `real`，只运行 real 和 test 的 Run；设为 `simulation` 时只运行 sim 和 shadow 的 Run。另一组的 Run 在 dispatch 时、发出任何 guard 请求之前就被拒绝（`runtime_world_group_refused`）。 | `apps/agent-host/src/worlds.ts`、`runtime-host.ts` |

## AT-046 负例（每条都有阳性对照）

| 负例 | 测试 | 阳性对照 |
| --- | --- | --- |
| Run 命令把 mode 伪造成 real、test 或 shadow，world 却是 sim；或者 world 是 real、mode 写成 simulation：服务端在签名之前就以 `runtime_activation._command` 拒绝 | `test_at046_a_forged_mode_is_refused_by_the_server`。Host 侧的同一规则由 S1 的 `nx041-world-check.test.ts` 覆盖 | mode 等于 world 的种类时通过 |
| sim 凭据去认证 real、test 或另一个 sim world：拒绝 | `test_at046_a_sim_credential_never_authenticates_real` | 在它自己的 world 里认证通过 |
| 在 sim world 里走真实效应路径：被 CHECK `nx041_real_effect_world` 拒绝，replica 模式下也一样。孪生角色调用真实外发入口没有权限；sim 凭据在自己的 world 里调用真实外发入口，也会被拒绝 | `test_at046_the_real_effect_path_refuses_a_sim_world_and_the_stand_in_serves_it` | real 行能通过 CHECK；同一个 sim 凭据经替身提交并执行，得到模拟回执 |
| Host 跨组：real 组的 Host 拒绝 sim 的 Run；simulation 组的 Host 拒绝 real 和 test 的 Run | `apps/agent-host/test/nx041-host-group.test.ts`，在 dispatch 层用真实的 `RuntimeHost` 测试 | 本组的 Run 能通过组检查，之后只会因为测试里那个连不上的合成 guard 而失败 |
| 孪生只能经过端口：端口角色能执行的 security definer 函数只有 act 和 observe，再加对 public 开放的 `erasure_permits`；它对任何表都没有权限 | `test_the_port_role_reaches_the_port_only` | 经端口的 act 和 observe 都正常 |
| 模拟输出不能变成事实，也不能变成已兑现的 Commitment（L6 复核 S4 修复时提的 5.2 余项） | `test_at046_simulated_outputs_never_become_facts_or_fulfilled_commitments` | — |
| **待决（xfail strict）**：sim 进程组完整跑一个 Pi Run | `test_at046_a_simulation_process_group_runs_a_complete_pi_run`：runtime worker 目前只接受 real 和 test。D10（每个 sim world 一个凭据还是三个）定下来、worker 有了 sim 选项之后，这条会 XPASS，strict 会让测试失败，提醒补上端到端测试 | — |

关于 5.2 余项，按代码的实际规则说明，有一处更正：Commitment 变为「已兑现」，并不只能由效应意图推进。0111 认四类证据（`runtime.nexloop_commitment_evidence`）：effect_fulfilled、commercial_event、operator_attestation，以及沟通类承诺的 delivered_message。没有证据的状态迁移，以及不是从证据准备出来的 Commitment，都会被守卫拒绝。测试实际验证：直接往 sim world 写一个 fulfilled 的 Commitment，会报 `commitment creation was not prepared from its evidence`。

sim world 里不会出现事实性的 Commitment 或 Claim，因为来源都被封住了：Claim 必须挂在会话上，而会话只允许 real；效应意图和外发消息表都有 CHECK，不接受 sim 行。

## §8 建议新增的三项验收（planning 由调度员登记）

| 建议验收 | 测试 | 对应 |
| --- | --- | --- |
| 替身零外部调用 | `test_acceptance_stand_ins_make_no_external_call`：替身目录里的 4 个替身都经执行器跑一遍，同时记录 Python 层的出站连接，结果一次都没有；用 real 意图调用替身命令会被拒绝 | AT-046；M21 第 296 行 |
| 模拟世界整体删除 | `test_acceptance_whole_deletion_of_a_simulation_world`：生成数据、端口事件和观察、替身意图和回执，全部随 world 删除，删完后该 world 在所有带 world 列的表里都是 0 行；另一个 sim world 不变，real 的行保持不变 | 设计 D2；「删除传播」 |
| 冻结输入可复现 | `test_acceptance_a_frozen_input_reproduces`：同一份冻结输入（生成的 seed 和参数、替身配置、孪生的 version 和 seed、注入计划）在两个 world 里跑：生成 digest、孪生事件、事件指标逐字节一致；替身回执的状态、延迟和版本一致。阳性对照：换一个 seed，事件就不同 | M22 可复现 |

**调度员已定 A**：替身配置里加一个服务端生成的 `outcome_seed`，盐改为 `sha256(outcome_seed|intent)`；复跑时由服务端连同 seed 一起复制冻结配置，调用方拿不到 seed，也不能自己传。这项改动由 L6 在 0262 中实现（`side/nx041-s4-fixes2`），本分支不改 0262。逐字节一致的回执测试 `test_acceptance_a_frozen_input_reproduces_the_stand_in_receipts_byte_for_byte` 先标 strict xfail，原因写「待 L6 0262 outcome_seed」，接口按暂定的 `runtime.nexloop_stand_in_config_copy(source_world, target_world)`；集成时按 L6 的最终命名打开。在此之前，现有的复现测试不比较 `external_ref`。

## 部署说明（由调度员安排，本次没有改 deploy/）

- 孪生进程用新角色 `nexloop_twin_port` 连接数据库，需要单独的 DSN，可以用 Keychain 里的口令，或者 pg_hba 的 peer 或证书认证。
- 孪生只拿它所在 sim world 的那一个凭据，不拿任何 real 或 test 凭据。
- 模拟进程组的 Host 配置 `"world_group":"simulation"`；real Host 不需要改，默认就是 real。
