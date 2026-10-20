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

集成（`int/nx041-s1s5`）：L6 的 0262 已合入，xfail 已去掉，测试改调 L6 的复制入口 `runtime.nexloop_stand_in_configure_copy(tenant, from_world, to_world, by)`，并且通过。L6 同时新增了“同一 business digest 只能有一个未结意图”的规则，所以这条测试和 `test_acceptance_stand_ins_make_no_external_call` 改为每个意图使用不同的 business digest。等 D10 的那条 xfail 保持不动。

## 部署说明（由调度员安排，本次没有改 deploy/）

- 孪生进程用新角色 `nexloop_twin_port` 连接数据库，需要单独的 DSN，可以用 Keychain 里的口令，或者 pg_hba 的 peer 或证书认证。
- 孪生只拿它所在 sim world 的那一个凭据，不拿任何 real 或 test 凭据。
- 模拟进程组的 Host 配置 `"world_group":"simulation"`；real Host 不需要改，默认就是 real。

## 审查修复（L6 审查 `nx041-s7-review.md`）

0266 是 S7 的集成编号，还没有发布，以下改动直接在 0266 里修改。

| 编号 | 修复 |
| --- | --- |
| 4.1 | 完整 Pi Run 那条 strict xfail 改成按实际阻塞项判断，不再停在旧的 worker 参数上。worker 现在带 `--world-group simulation`，按 S6 能通过参数检查；如果这里抛出 SystemExit，说明是回归，不是阻塞。之后由 `pi_run_blockers` 逐项探测三个前提：一是 D10，每个 sim world 三个凭据，探测方法是插入第二个 active 凭据，看是否被 S2 的规则拒绝；二是 S6c，看 sim 进程组自己的角色 `nexloop_sim_api` 和 `nexloop_sim_worker` 是否存在；三是 Host 的 sim 上下文输入协议，看 Host 源码是否读取 `nexloop.context-pack.sim.v1`。任何一项缺失就抛 `PiRunBlocked`，xfail 只接受这个异常。**局部验证**：`test_the_pi_run_xfail_flips_once_its_prerequisites_are_there` 先确认现在三个前提都缺；再把探测函数打桩为「都满足」，同一个测试体正常返回。这说明三项齐备时，这条 strict xfail 会变成 XPASS，CI 会因此失败，提示改成真正的端到端测试。 |
| 4.2 / 3 | 逐字节复现的测试改用正式接口 `runtime.nexloop_stand_in_configure_copy`，每个意图使用不同的 business_digest，比较除 world 以外的**整个回执**，包括 `external_ref`。冻结输入复现测试的第二轮，也改为由服务端复制第一轮的冻结配置（含 outcome_seed），同样比较除 world 外的整个回执。 |
| 2.1 | 集成时 L4 已按 S6 处理：Host 的 world group 以 S6 的实现为准，错误码统一为 `invalid_run_world`。本次没有改动。 |
| 2.2 | 补了四条伪造路径的负例，每条都有阳性对照：(1) **Context**：real 的 v6 装配入口拒绝 sim world；v6 契约拒绝 world_id 为 sim 的 binding，real binding 能通过。(2) **resume**：在 sim 组的 Host 上，用另一个 sim 或 shadow world resume 同一个 Run，报 `runtime_state_missing`；在 real 组的 Host 上，把 real 改成 test 再 resume，报 `runtime_request_conflict`；用原命令 resume 正常。(3) **guard**：sim 组的 guard 拒绝 real 和 test，real 组的 guard 拒绝 sim，各自接受本组的 world。(4) **SQL 激活**：激活入口在读到凭据的 world 之前，先校验角色、队列和命令签名，所以未签名的请求对任何凭据、任何 world 都同样报 `activation denied`，测试断言了这一点。world 绑定由两处保证：服务身份（sim 凭据不能认证 real 或 test）和 Run 命令的 world/mode 校验，这两处的负例都已有。**局限**：没有构造一条完整签名、但 world 被篡改的激活命令。原因是这需要真实的签名夹具，而签名本身已经覆盖命令的全文。 |
| 1.4 | `nexloop_twin_port` 角色已存在时，校正它的形态，做法与 S4 N2 相同：校正属性、撤销成员关系、撤销原有权限，再重新授权。测试先建一个属性不对、带成员关系的同名角色，迁移后它只剩规定的属性和两个端口入口。 |
| 1.5 | **每个 world 的事件上限，提议 100,000 条，请调度员定。** 理由：这与 S3 生成器的单次上限（2,000 个 Consumer × 50 条事件）同级，足够一次实验使用。上限只在 `runtime.nexloop_sim_port_event_limit()` 一处定义。端口的串行锁从按 consumer 改为按 world，这样计数是精确的。重放已有的 step 不受上限影响。测试覆盖了：恰好写满时最后一条能写入、再多一条报 `quota_exceeded`、重放仍然返回原结果、另一个 world 不受影响。 |
| 5 | 原来匹配源码字面量的断言改成了行为测试。0266 给 `runtime.nexloop_commitment_evidence` 加了 CHECK `nx041_no_simulation_evidence`：这张证据表不接受 sim 或 shadow world 的行。所以无论哪个写入方（效应意图、data_mode 为 test 的商业记录、owner 的证明、已送达的消息），都不能在 sim world 里产生证据，sim world 的 Commitment 只会违约或取消，永远不会被兑现。测试从 CHECK 定义中枚举出全部证据种类，逐一验证：写 real 能通过（阳性对照，随后回滚），写 sim 和 shadow 都被拒绝；直接写入一个已兑现的 Commitment 也会被守卫拒绝；没有证据时，状态目标不会是 fulfilled。 |
| 6 | 与 S4 修复二在测试文件上的冲突，已由 L4 在集成时合并。本次没有改 S4 的测试。 |

## 定案与后续（调度员 2026-10-21）

- **端口事件上限**：按提议定为每个 world 100,000 条（`runtime.nexloop_sim_port_event_limit()`）。
- **sim world 的 Commitment 不可兑现**：保留 CHECK `nx041_no_simulation_evidence`。这是保守做法，以后可以用新迁移放开。
- **后续（NX-042/043）**：如果实验需要「模拟兑现率」，届时新增一种证据，明确标为 simulated，只在 sim world 里有效，永远不会流到 real。现有的四类真实证据仍然不接受 sim 行。

## 复核余项（L6 `nx041-s7-fix-verify.md`）

| 项 | 处理 |
| --- | --- |
| S6c 探测 | 改为检查三个角色 `nexloop_sim_worker`、`nexloop_sim_guard`、`nexloop_sim_api` 是否都存在。名字与 L2 的 S6c（126e4d1b）一致。 |
| Host 探测 | 不再扫描源码里有没有 schema 字符串，改为检查 Host 的 `src/*.ts` 是否导出了 `parseSimContextPack`，即 L2 在 f13190f5 里实现的 sim 包解析函数，支持 `export function`、`export const` 和 `export {…}` 三种写法。 |
| D10 探测 | reason 和函数注释里都写明：D10 定案时，要按定案的凭据形式同步改写这个探测。现在的探测是复制已有 binding、只改主体；如果最终定的是「三个凭据按用途区分」，这个写法可能永远翻不过来。 |
| 2.2 SQL 激活 | 新增 `tests/test_nx041_s7_activation_pg.py`，用现有的 `runtime_effect_fixture` 拿到真实的 worker 会话、已认领的任务和签名密钥。worker 签两类激活命令，签名都正确：原样的 real 命令作为阳性对照，能通过 SQL 的 world 检查；另一类把 command_text 里的 world 换成 sim、shadow 或 test，mode 也一并改成对应值，所以 Python 侧的命令检查会放行，最后由 SQL 拒绝（0037 的 `activation command denied`：命令里的 world 与凭据的 world 不一致）。**Context 入口**：在测试里登记一把签名密钥，签一条格式正确的 v6 请求。sim world 的请求在第一道检查（只允许 real）就被拒绝；同一条请求换成 real，能通过 world 和签名两道检查，一直走到 `nexloop_assert_context_assemble`。这就是阳性对照，说明 sim 的拒绝确实是因为 world，而不是请求本身有问题。 |
| 5 | 改为按约束名取 Commitment 证据的 kind CHECK（`nexloop_commitment_evidence_kind_check`），不再用 `like` 匹配加 `fetchone()`。 |
| N1 | 事件上限改用计数表 `runtime.nexloop_sim_port_counters(tenant, world, events)`。在 world 的串行锁里先加一再检查，计数是 O(1) 的，而且精确；超限时整个事务回滚，加上去的那一次也一起撤销。重放已有的 step 不计数。计数表有 world 列，会随 world 一起被整体删除（S2）。 |

## 复核余项二（L6 `nx041-s6c-s7-fix-verify.md`）

| 项 | 处理 |
| --- | --- |
| F2 | 计数表加了回填函数 `runtime.nexloop_sim_port_counters_backfill()`，0266 建表后马上调用一次。它按 tenant 逐个执行，把每个 world 的计数设为端口里已有的事件数；可以重复执行，结果不变。这样即使端口和计数表拆成两次上线，计数也从真实值开始。新增测试：先写入 7 条事件、删掉计数行，执行回填后计数为 7；再执行一次仍是 7；之后经端口写一条，计数变成 8。 |
| F3 | Context 阳性对照用的测试签名密钥 `s7-context-probe`，测试结束时删除，并断言已经删掉。 |
| 签名激活 | **阳性对照**改为一次全新的创建：按夹具的流程，新签发一个 Run 凭据，绑定、accept、认领，然后用原样的 real 命令新建激活；新的 activation_ref 不在夹具已有的激活里。**负例定位**：0037 里报 `activation command denied` 的有两处，一处是命令的 tenant、digest、world、not_after 检查（文件第 75 行，函数体第 28 行），另一处是 owner epoch 检查（函数体第 30 行）。所以测试不再看错误文字，而是从错误链中取出 psycopg 错误，断言它的 PL/pgSQL 调用栈正好是 `authz.nexloop_runtime_activation_command_v0037(...) line 28 at RAISE`。负例和对照只差 world，于是拒绝原因就锁定在 world 条件上。 |
