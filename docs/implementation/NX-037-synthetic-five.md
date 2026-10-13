# NX-037 五类交互在本机合成栈上跑通（readiness gaps §9 第 2 步）

本文说明怎样在本机、用合成数据，让 NX-037 试运行的五类交互都经过运行入口 `scripts/nx037/run_interactions.py` 跑通。五类是：普通回复、企业交付、拒绝后限制、人工接管、结果未知后对账。本文还写清 synthetic 与 real 两种模式的差异，以及 real 模式还缺哪些外部材料。

整个过程不碰真实模型、不碰真实凭据，也不连服务器。

## 1. 怎么跑

```bash
nvm use 24 && pnpm --filter @nexloop/agent-host build
LC_ALL=C.UTF-8 .venv/bin/python -m pytest -q tests/test_nx037_synthetic_five_pg.py
```

这条测试必须串行跑，开发机上大约一分钟。每次都在干净的 PG 上从头建库（catalog bootstrap），不依赖上一次运行留下的任何状态。

## 2. 测试搭了什么

| 组件 | 本测试里的形态 |
|---|---|
| 租户 | 合成租户 `TENANT_A`（`tests/support/tenants.py`），由 marker `nx037_test_stack` 指定。 |
| API | 实际的 `nexloop_eios.http_api`，TLS，监听 127.0.0.1。客户经 HTTPS 登录、建会话、发消息，接口和 WebChat 用的是同一组。 |
| 消息 relay | run profile 模式（与 stage 部署一致）。客户经 `tests/load_chain.py` 的 enrollment 路径建立，并带企业交付槽位（ADR-030）：独立的 EffectControl、企业 offering 和客户绑定，再用 `relay_enrollment.enroll(..., enterprise=...)` 登记。 |
| Agent Host | `deterministic-test` 档，币种 CNY，上下文包 v2，`deterministic_enterprise_deliver: true`（见 §4.1）。 |
| 运行时 worker、guard | 实际进程。 |
| 回复投递 | `local_json_delivery`（`nexloop.service.request` 的投递服务），再由出站消息录入器（`nexloop_eios.outbound_messages`，NX-047）把回复写进会话流。 |
| 企业端点 | `apps/reference-endpoint` 在测试进程内启动，用测试 CA、测试 service token 和 staff token。测试会记录端点收到的每个请求，记方法和 intent id。 |
| effect worker | `--provider-routes-file`：`service.request` 路由到 JSON 投递，`enterprise.deliver` 路由到参考端点，没有默认 provider。 |
| 三层开关 | 只在这个测试库里打开：effect worker 进程和运行入口的环境变量 `REAL_DISPATCH_ENABLED=true`、租户开关（`data_mode` 为 real）、`nexloop.enterprise.deliver:1` 的 Action 开关。 |
| 租户就绪 | 用 configurator 角色应用效应类别 v3 和告警规则 v4（只声明已发布的 Action）。100 CNY 的模型预算作为测试种子直接写入（见 §5）。 |
| 运行入口 | `run_interactions.main([... '--driver', 'real' ...], test_stack=True, operator=Operator)`。驱动用 RealDriver，只观察，不代发任何消息。 |

交互计划：试运行配置取 v2（CNY；上限 100/2/300；告警规则 v4；效应类别 v3），总数改为 5，五类各 1 次。顺序是：普通回复、企业交付、人工接管、结果未知、拒绝。

真实模式的流程照样保留：前 3 次是校准交互，跑完后暂停（`calibration`）；拿到 owner 的确认引用（`--resume`）后，再跑完剩下 2 次。

自动操作员替真人执行每一步：

- **普通回复**：发一条普通问题。
- **企业交付**：发一条以 `企业交付：` 开头的消息。确定性 Host 先调 `nexloop.enterprise.deliver`，再调回复。
- **人工接管**：
  1. 先发一条消息，等 Agent 回复。
  2. 由 staff 接管会话（调用 0126 的 handler，governed entry 也是这样调用它）。
  3. 客户在接管期间再发一条。这时 relay 不为它创建 Run。
  4. staff 回复这条消息（0127），然后交还会话。
- **结果未知**：发消息前先执行参考端点的本机演练命令 `drill --delay-next-posts 1 --delay-seconds 5`。这就是 L5 的"暂时未知"开关：端点先把请求落库，5 秒后才应答，超过 worker 3 秒的超时。
- **拒绝**：发"不要再联系我"。

## 3. 每一类断言什么

| 交互 | 断言 |
|---|---|
| 全部五类 | outcome 为 ok；`test_stack: true`；成本币种是 CNY，没有其他币种；未授权发送、错误收件人、拒绝后联系、跨租户读取都是 0；每个意图最多执行 1 次。 |
| 回复到达 | 客户在会话里看到这五条消息各自的一条 Agent 回复，`reply_to_message_id` 指向对应的来信。 |
| 普通回复、人工接管、拒绝 | 意图为 `{fulfilled: 1}`，对账结果 settled。 |
| 企业交付 | 意图为 `{fulfilled: 2}`，即回复和交付各一个。交付工单落在参考端点的 SQLite 里，`consumer_ref` 是该客户；同一个 intent 只 POST 过一次。 |
| 结果未知 | 端点对这个 intent 的请求序列恰好是 `POST, GET`：先记为 unknown，再查询对账，没有重发。账本事件里有 `unknown`，最终状态 `fulfilled`。 |
| 人工接管 | 接管期间那条来信没有 Agent 回复，只有 staff 回复（`sender_kind: human_takeover`）。 |
| 拒绝 | 客户被拒绝消息本身限制住（`contact_restrictions.message_id` 等于这条消息）；绑定到这条消息的那一条回复照常发出（ADR-023 §2.6）。 |
| 预算和币种 | 模型预算为 CNY 100；试运行 state 和每条记录都按 CNY 计；证据包 summary 的币种也是 CNY。 |
| 证据包 | `evidence_pack.py --trial` 生成的证据包：`index.json` 里 `test_stack: true`、`real_evidence: false`；`summary.by_kind` 五类各 1；`check()` 结果干净。 |

另外有一条断言：没有 `test_stack` 时，同一组参数会被拒绝，理由是 `host_not_real_model`。也就是说，放行只能通过关键字参数，不能通过命令行。

## 4. 为了跑通做的代码改动

### 4.1 确定性 Host：`deterministic_enterprise_deliver`

改动在 `apps/agent-host/src/runtime-host.ts`。

**允许出现的条件**（否则 Host 拒绝启动，测试 `apps/agent-host/test/deterministic-enterprise-deliver.test.ts` 覆盖了所有情况）：

- `runtime_profile` 必须是 `deterministic-test`。`deepseek-flash` 档（也就是所有 stage 部署的 Host）只要配置里出现这个键，就拒绝启动。对照组：同一份模型配置去掉这个键后能通过配置检查。
- 还要求 `effect_tools: true`、`deterministic_message_from_input: true`、`context_input_protocol` 为 v2。

**调用 deliver 的条件**（同时满足才调，而且每个 Run 只调一次）：

- 当前客户消息以 `企业交付：` 开头；
- 本 Run 的上下文 `effect_supplies` 提供了企业交付，而这只在 Run 凭据包含 `nexloop.enterprise.deliver:1` 时才会有；
- 本次模型请求确实声明了 `nexloop.enterprise.deliver` 工具。

调完 deliver 后，按原协议继续回复。派发时的所有复查都照旧执行：guard、SQL 和 effect worker 的三层开关与准入检查都不绕过。

### 4.2 运行入口的测试栈：`main(..., test_stack=True)`

改动在 `scripts/nx037/run_interactions.py`，测试在 `tests/test_nx037_run_interactions.py`。

- 只能通过关键字参数开启，不提供 CLI 选项。
- 要求：
  - 使用 real 驱动；
  - 租户必须是合成测试租户（`tests/support/tenants` 的三个常量），真实租户 uuid 一律拒绝；
  - 试运行币种必须是 CNY；
  - Host 必须是 `deterministic-test` 档，币种仍与试运行一致；这时不要求 tariff。
- real 模式原有的其他前置检查不变：租户就绪核查、三层开关与计划完全一致、回复兜底预算的币种。
- 记录和 state 都标 `test_stack: true`。暂停后恢复时不能切换栈，否则报 `PlanInvalid`。
- 证据包遇到 `test_stack` 时写 `test_stack: true` 和 `real_evidence: false`，永远不能算作真实试运行的证据。

### 4.3 产品缺陷修复：0480，`intents_open` 计入 unknown

改动在 `packages/eios-core/src/eios/migrations/0480_nx037_observation_unknown_open.sql`，是临时编号。

**缺陷**：0172 的 `intents_open` 只统计 accepted、dispatching、observed_fulfilled 三种状态，不统计 unknown。

- RealDriver 判定"交互完成"的条件是 Run 已结束且 `intents_open = 0`。所以只要遇到第一个结果未知的意图，交互就被判为完成，记成 `reconciliation: unresolved`，试运行随即以 `unresolved_unknown_effect` 暂停。
- 此时 worker 的查询对账其实还没发生。

**影响**：修复前，真实试运行会在第一次结果未知时误暂停。计划 v2 里结果未知的 3 次都会这样，每次都需要 owner 确认才能继续，而这些暂停并不代表真有问题。

**修复后**：未结清的 unknown 也算"未完成"；如果它一直结不清，就以 `interaction_timeout` 暂停，仍然停下来交给 owner。

**阳性对照**：把 0480 从 catalog 和目录里临时移走后，`tests/test_nx037_synthetic_five_pg.py` 在第 4 次交互暂停，规则为 `unresolved_unknown_effect`；恢复 0480 后通过。

**语义一致性**：0172 里没有其他读取 `intents_open` 的地方，它只被 RealDriver 的完成条件使用。0480 与 0172 的函数体只有这一行不同。

### 4.4 测试夹具

- `local_message_assembly_fixture`：带 marker `nx037_test_stack` 时，发布 `nexloop.enterprise.deliver:1`，执行者也持有这个 Action。不带 marker 时完全不变。
- `test_trusted_configuration_pg.configured`：带同一个 marker 时，租户使用 `TENANT_A`。

## 5. synthetic 与 real 的差异

| 项 | 本测试（test stack） | real 模式 |
|---|---|---|
| 模型 | `deterministic-test`：按协议脚本回复，成本为 0 CNY。 | `deepseek-flash`，按 CNY tariff 计费，每个 Run 的成本受上限约束。 |
| Host 上下文包 | v2。 | stage 示例是 v6（见 §6 第 1 条，企业交付在 v6 下目前走不通）。 |
| 企业交付的触发 | 消息以 `企业交付：` 开头。 | 模型根据客户意图自行决定。 |
| 客户 | 自动操作员通过 HTTPS API 发消息。 | owner 的员工在 WebChat 里扮演客户。 |
| 人工接管 | 测试直接调用 0126/0127 的 handler（与 governed entry 的调用方式相同）。 | staff 在工作台登录，通过工作台 HTTP 接管、回复、交还。 |
| 企业端点 | 参考端点在测试进程内运行，用自签 CA 和测试 token。 | 企业自己的端点，或 stage 上部署的参考端点：origin、CA 和 service 凭据由企业提供。 |
| 结果未知 | 参考端点的本机 `drill` 命令。 | 同一个 drill 命令，在企业端点所在的机器上执行；或者用 `unknown_drill_relay`。 |
| 预算 | 测试种子直接写入 `control.nexloop_budget_limits`。 | owner 在工作台执行 `Budget.set`（受治理 Action，见 `real_tenant_init.py` 的 budget_request）。 |
| 开关 | 测试库里由 admin 写入（`set_switch`）。 | owner 通过受治理入口打开，打开的状态必须与计划的 `real_dispatch` 完全一致。 |
| 证据 | `test_stack: true`、`real_evidence: false`。 | 真实证据，另外还需要 inventory、release manifest、恢复演练等条目。 |

## 6. real 模式还缺的外部材料与阻塞

1. **企业交付和 v6 上下文包不兼容**（跨线问题，已回报调度员，未改动）：
   - stage Host 配置为 v6，Host 在 `runtime-host.ts:167` 按 v6 校验；
   - stage 的 message-relay 没有 `--context-strategy`，产出的是 v2 包；
   - 企业槽位只存在于 v2 包（`message_relay.py:238`：使用 v6 时跳过企业槽位，原因 `context_unsupported`）。

   以上来自读代码，未在真实模型上运行验证。需要 L2/L4 决定：让 v6 包也携带 `effect_supplies`，或者 stage 的 Host 改用 v2。
2. 模型：DeepSeek 的模型配置和凭据（由 owner 提供），以及经过核实的 CNY tariff 文件。
3. 企业端点：origin、CA 和 service 凭据（由企业提供；也可以在 stage 上部署参考端点，自行生成 CA 和 token），防火墙参数 `ENTERPRISE_ENDPOINT_ADDR/PORT`。
4. owner 在工作台执行 `Budget.set`（100 CNY，覆盖试运行期间），并通过受治理入口打开三层开关。
5. WebChat 渠道：渠道引用、扮演客户的员工账号；工作台 staff 账号（人工接管用）；owner 会话文件（0600）。
6. 价值目录文件、试运行时间段。
7. 证据包的其余条目：`inventory_before/after`、`release_manifest`、`ci_result`、`restore_drill`、`effect_reconciliation`、`public_preflight` 等。
8. 停止规则 `contact_after_refusal` 与 ADR-023 §2.6 有冲突，待调度员决定（见 §7 第 2 条）。决定之前，真实试运行里拒绝场景应该用单独的客户来扮演。

## 7. 发现的产品缺陷

1. **0172 的 `intents_open` 不统计 unknown**：已用 0480 修复，见 §4.3。
2. **`contact_after_refusal` 会计入 ADR-023 §2.6 允许发出的绑定回复**（0172，L3；待调度员定修法）：
   - (a) 拒绝消息自己的那条回复：如果录入器把它写进会话流的时间早于观测，就会被计为 1，结果取决于时序。本测试把拒绝排在最后，目前通过。
   - (b) 拒绝之后同一客户的来信，得到的回复一定被计入。对照实验：把拒绝排在第 3 次，第 4 次（同一客户发来的接管消息）就以 `contact_after_refusal` 停止。计划 v2 里拒绝排在第 4 次，后面还有 26 次；如果全程只用一个客户，试运行会在拒绝后的第一次交互就停止。
3. **relay 的 recipe 模式接受 `enterprise` 块，但 SQL 不认**（L2；已回报，未改动）：
   - `validate_recipe` 和 `MessageRelay` 都接受这个块；
   - 但 0180 的 issue 检查要求消息必须通过 relay claims 和 enrollment，recipe 模式下一律报 `enterprise slot unavailable`，CLI 只输出 "Message relay unavailable"。

   stage 用的是 run profile 模式，不受影响；本测试也已改走 enrollment。
4. **真实模型下企业交付走不通**（跨线问题，L2/L4）：见 §6 第 1 条。
5. 测试缺口（不是产品缺陷）：`tests/test_nx037_real_driver_pg.py` 没有启动出站消息录入器，因此从未断言过"回复出现在会话里"；本测试补上了这一点。
