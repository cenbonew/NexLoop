# NX-015 — 真实效应故障测试设计

本文件是测试协议设计，不是已通过的验收报告。依据 `06_ACTION_RUNTIME_AND_RECOVERY.md` §2–5、§8、AT-033～037，以及当前 Action 源码和 `NX-015-effect-source-audit.md`。本次未运行测试、provider、迁移或生产操作，未更新 planning；Pi 完成不能作为业务效应成功证据。

## 可复用的真实基础与精确 API

| 位置 | 可复用内容 | 边界 |
|---|---|---|
| `tests/conftest.py` 的 `pg` / `admin` | 每例独立 Homebrew PGDATA、Unix socket、随机端口；必须存在 PostgreSQL，否则失败，不跳过 | bootstrap admin 只用于 disposable schema、合成 authority 配置及技术账本观察，应用不拿此 DSN |
| `test_postgres_action_claims.action_port` | `bootstrap(admin)`、`seed_authority`、独立 signer、`nexloop_api` 角色与真实 resolver | 默认只是 claim，并未登记 published Action；不能直接当成 external Action fixture |
| `test_postgres_action_claims.request` / `finish` / `governance_inputs` | 真正的 `ActionClaimRequest`、`ClaimBindingPayload`、`ActionDefinition`、`CapabilityContractSnapshot` 与治理证据结构 | `Consumer.create` / `postgres` 是对象创建样例，外部 provider 要登记独立契约，不替换业务含义 |
| `test_action_definitions.published_action` | 完整 published definition、capability、精确 object schema digest；签名 bundle reader；published version 不可变测试 | 测试配置阶段直接登记 control/schema 表；不是产品发布 API |
| `authority_fixture.seed_authority` / `replace_fact` | 可生成两个真正不同 `identity_suffix` 的 SERVICE，撤销 grant 并触发当前 authority/epoch 复核 | 所有主体配置完成后重新 authenticate，不能沿用 stale session；token 仅运行时生成 |
| `agent_authority_fixture.seed_agent`、`test_agent_governed_write.py` | Source Agent release、application ceiling、actor grant、完整 chain 与撤权窗口 | 不假冒人类；测试需兼顾 SERVICE 和真实 AGENT/Run-bound 来源 |
| `test_action_definitions.test_committed_create_survives_sigkill_before_receipt` | 独立子进程、匿名 stdin 配置、固定 barrier、实际 SIGKILL、restricted backend 重开 | 它证明本地对象原子事务，不证明外部 effect；新的 kill barrier 必须由 provider 持久受理证据触发 |
| `test_runtime_atomic_accept.py` / `test_runtime_worker.py` | spawn/独立 CLI、persist-beforeACK、opaque activation、旧 fence、backend/Host 重开编排 | Runtime lease 不授予 external Action 权；不把 runtime receipt 当 provider receipt |

当前真实可调用接口：

- `open_backend(database_url=..., artifact_root=..., signing_key_file=..., signing_key_id=...)`；`backend.authenticate(token, world='real')` 派生 tenant/principal/world，不能由请求覆盖。
- `AuthenticatedServices.create_object(*, action_name, action_version, intent_id, type_name, properties)`：现有受治理对象写接口，可用于验证 fixture，不是发送 provider 的入口。
- `PostgresActionDefinitionReader(pool, session, signer).get(stable_name, version)` / `.get_with_schemas(stable_name, version, *, relation=False)`：实际调用 signed `authz.nexloop_read_action_bundle(token_digest, world, claims, signature)`，检查 PUBLISHED、capability binding 和精确 schema digest。
- `govern_published_action(pool, session, signer, *, claim_request, request, object_reader=None)`：从真实 published store 获取 definition/capability，以 server scopes 组合 governor；返回 `ActionExecutionPermit` 或 terminal reference。
- `PostgresActionClaimPort(pool, session, signer).reserve(ActionClaimRequest)`、`.mark_retryable(ActionClaimRetryableCommand)`、`.finalize(ActionClaimFinalizeCommand)`：真实 PG claim/revision/fence、current EIOS authority；不是 dispatch/query/reconcile port。

**实际定义登记路径**：`published_action` 先构造 PUBLISHED `ActionDefinition` 和 schema，写 `ontology.object_type_versions`，再写 `control.nexloop_action_definitions(tenant_id, world, resource_id, definition, capability)`；签名 key 与 authority 随后配置。应用经 reader/governor 读取该固定 bundle。当前 Backend 没有 publish-definition 方法；vendored `DefinitionRegistryPort.register` / `AtomicDefinitionRegistryPort.register_batch` 是协议，现存 `InMemoryDefinitionRegistry` 不能作为 PG 发布实现。测试可以复用明确的合成 control-plane 配置，但上线 Action 的正式发布/审批装配缺口必须另交付，不能把 fixture INSERT 说成产品发布完成。

当前也没有通用 external effect public API。以下 `accept_effect`、`claim_effect`、`authorize_dispatch`、`record_effect_result`、`reconcile_effect` 均为本测试要求的**待实现能力名称**，需先由实现者固定签名；不是已存在可调用接口。必须经 Backend lifecycle/current-auth 与受限 PG definer，不能从测试 adapter 用 SQL 代写业务账本。

## 独立持久 HTTP provider 协议

采用单独进程、loopback 随机端口、测试私有目录中的 SQLite（WAL/FULL）作为合成 provider。它与 NexLoop PG、Worker、Host 都独立，Worker SIGKILL 后 provider 继续存在；provider 自身重开也保持操作账本。该 SQLite 是外部测试系统的存储，不能用它代替 NexLoop 正式业务 PG。

- `POST /operations`：固定 `Idempotency-Key`，规范化请求 digest。首次受理将 key、digest、reference、状态和实际 effect 行在**同一个 provider 事务**提交，再发 HTTP 202。相同 key/digest 返回同 reference；不同 digest 返回 HTTP 409，不覆盖记录。
- `GET /operations/by-key/{key}`：持久账本中查询原 key，返回 reference、payload digest 与 `accepted|fulfilled|confirmed`；未发现返回明确 `not_accepted`（限定此测试 provider 的线性一致语义）。HTTP timeout/500/无 query 能力不是 `not_accepted`。
- 另设私有测试控制通道（匿名 Pipe/Unix socket），只提供固定 barrier 与故障模式；不注册成产品业务 endpoint。barrier `accepted_committed` 必须在 provider commit 后发给父进程，Worker 的结果写回 PG 前必须仍被阻塞；父进程观察两个条件后 SIGKILL Worker，不靠 sleep 猜窗口。
- 分开记录 `dispatch_request_count`（收到 POST，包括重放）、`effect_count`（实际副作用唯一行）与 query 序列。AT-033 的强断言是恢复首次为 query、恢复 POST 增量为零、effect_count=1，不能仅靠 provider 幂等掩盖盲目重发。
- 单独验证 provider commit 后断连接/响应延迟，以及 query 超时/500、不可查询、仍 pending、明确 not_accepted。provider 故障不伪造成 success；receipt 明确区分 provider accepted 与 fulfilled/confirmed。

凭据全部在运行时生成并写 0600 临时文件或走匿名 stdin/socket；不能放 fixture 常量、argv、prompt、stdout/stderr。测试 client 不读 `.env`。provider 入口固定 loopback 地址/路径，不接受 Runtime 提交任意 URL。每个进程的实际 stdout/stderr与PG技术账本/Artifact检查 synthetic secret sentinel 不泄漏。

## 故障与验收矩阵

| ID / 场景 | 编排与必须观察的证据 |
|---|---|
| AT-033 外部受理后 kill | ActionIntent+outbox commit-beforeACK；受限 Worker claim、重验 authority、dispatching attempt commit；真实 POST；provider `accepted_committed` 后、本地结果提交前 kill。新 Worker 重开同意图，先查询原 key/reference，更新原 receipt；POST 数不增，effect_count=1。没有 query 能力保持 unknown 且零重发 |
| AT-034 payload 409 | 同 stable business key，不同规范化 payload，顺序及并发两种；入口 HTTP 409（不能只测内部 enum），原 digest/receipt/outbox/attempt 不改，provider 不收到冲突请求。匹配规范化语义的等价输入必须 replay |
| AT-035 新 tool_call_id | 同一可信业务意图，模拟两次不同模型工具调用 ID，经实际工具网关/API 查找原 intent；同 receipt/provider reference、effect_count=1。不同真实意图即使文本相同仍可有两个合法 effect，防全局文本 hash 去重 |
| AT-036 dispatch 前撤权 | 受理后、发送前固定 barrier；撤销 grant、Source release/application ceiling、发布 active、预算/control revision/consumer contact constraint分别测。释放 barrier 后真实当前 EIOS 检查拒绝，provider POST=0。PG unavailable 同样不发送；旧 permit 快照不够 |
| AT-037 两主体同意图 | 同租户两实际 SERVICE/AGENT 身份、各有当前合法授权；在 admission 完成前 barrier 并发，以相同可信业务键提交。共享 intent/receipt（或一个 admitted、另一个有权限读取相同 replay），一个 dispatch owner、effect_count=1；另测无读取权主体不能借 key 获取他人 receipt |
| fence 旧结果 | Worker A 持有旧 Action fence，provider 受理但延迟响应；lease 到期 Worker B 获新 fence、query 同 key。释放 A 的旧响应，A 的结果写回拒绝，不覆盖 B 的 receipt/revision；provider 已发生事实不被 DB fence 撤回 |
| 分界故障 | admission PG rollback 前无 ACK、无 provider；dispatching commit 前 kill 无发送；dispatching commit 后发送前 kill 必须 query-first；结果 commit 后 ACK 前 kill 重放同 receipt；unknown 查询仍未知保持证据并不无限 retry |

**跨主体的关键阻塞**：现存 `0007_action_claims` 比较 principal 与完整 binding，同 intent 由第二主体 reserve 当前会 conflict。AT-037 不能通过删除 principal 检查解决；需可信 intent 服务固定执行绑定、每个参与主体独立授权访问/提交同意图，再由一个受治理执行身份拥有 Action claim。两主体权限配置后刷新 session，测试需明确 principal 确实不同，而非两个 token 实际同主体。

## 证据和实现顺序

先固定 published external Action 的 schema/capability、稳定 business key 与 receipt语义，再实现受治理 PG Intent/outbox/attempt/独立 Action lease。之后实现固定 provider dispatch/query 协议及已受理未知状态，再写上述真实故障测试；最后接 Pi 工具并证明新 tool_call_id不改变业务 intent。execute_action 在这些幂等证据成立前不能声明 replay safe。

测试报告必须给进程 PID/exit SIGKILL（不含凭据）、真实命令、PG receipt/intent/fence/revision、provider commit/reference/query 顺序与计数、首次失败/重跑。合成独立 HTTP provider 的真实网络/持久/故障证据与真实外部渠道履约证据分别报告；缺渠道凭据仅阻塞后者。任何等待 barrier 超时都失败，不 fallback Memory，不 skip critical，也不改 done/passed。

本次实际读取：上述 docs、planning NX-015/AT 表、`tests/conftest.py`、Action claim/definition/Agent/runtime fixtures、`backend.py`、`object_actions.py`、`action_definitions.py`、`action_governor.py`、vendored definition registry protocol；使用 `cat`、`sed`、`rg`。未执行拟议测试，也未访问 production、私有凭据或上游生产目录。
