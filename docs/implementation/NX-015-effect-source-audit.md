# NX-015 — 业务意图与真实效应源码审计

本次仅阅读当前 NexLoop 与冻结 NEX-EIOS 通用源码；没有执行迁移、provider 调用、测试、生产操作或状态更新。NX-015 和 AT-033～037 当前仍为 `not_started` / `not_run`。Pi submission 的 `done` 不是业务效应成功证据。

## 依据与源码事实

依据：冻结 `docs/handoff/docs/06_ACTION_RUNTIME_AND_RECOVERY.md` §2～5、§8～9，`14_TEST_ACCEPTANCE.md` AT-033～037，`15_BACKLOG_AND_MILESTONES.md` S2/NX-015，以及 live `planning/tasks.json` / `acceptance-tests.json`。目标交付是 payload 冲突、unknown、provider 查询及 dispatch 撤权重验；AT-037 要求两个角色同意图的重复副作用为零。

实际已有：

| 位置 | 已有行为 | 不能据此证明 |
|---|---|---|
| `nexloop_eios/postgres_action_claims.py` | 真实 EIOS resolver 决策；当前 server session 绑定 tenant/world；有界 HMAC 命令；受限角色只调用 protected SQL；reserve/retryable/finalize；错误映射 | 不执行外部效应，没有 provider 查询、unknown 状态或业务 HTTP 409 映射 |
| `eios/actions/models.py` `ActionReservationKey` / `ClaimBindingPayload` | key 为 tenant/action/idempotency_key；binding 含 invocation_id、Action 版本、request_digest、capability、adapter、target_system | 不会替业务生成稳定 intent；改变 invocation_id 或其他 binding 字段会造成冲突 |
| `eios/migrations/0007_action_claims.sql` | PG 唯一键 tenant/world/action/intent；advisory lock + row lock；当前授权等待后再检查；active lease、revision/fence；terminal replay；不同 binding 返回 conflict；terminal outcome 不允许覆盖 | 不代表外发与本地结果原子；lease 过期可重新 reserve，没有“先查询未知外发”的保护 |
| `0033` / `0034` 的 `nexloop_assert_action_authority` 最终覆盖 | 完整 SERVICE/AGENT fact vector、当前父级授权链、Source/Run identity、scope、epoch、过期、锁定后再次核验 | Run/task 许可不能代替 Action 自身许可，也不能撤回已经离开本系统的外部请求 |
| `nexloop_eios/object_actions.py` + `0010_governed_object_create.sql` 及后续治理加固 | 当前发布契约/准入检查；业务对象写入与 claim terminal outcome 同一 PG 事务；intent replay | 是受治理 PG 对象创建，不是 HTTP/provider accepted→delivered/fulfilled 的外部闭环 |
| `tests/test_postgres_action_claims.py` | 文件中已有并发 reserve、terminal replay、payload conflict、retry fence、claim 后撤权、决策与 SQL 间撤权测试 | 本审计没有重跑它们；这些测试没有“provider 受理后 kill”或两个不同主体的真实发送证明 |

当前 `GovernedEffects` / `governed_effects.py` 模块在 NexLoop extracted core 与冻结上游 `src/` 均未找到。不能将一个预期名称当成现成可接入实现。当前没有可调用的通用效应 provider dispatch/query/reconcile port。

### 上游迁移号复核

冻结提交 `1e363db982daeca74058453ad12fc4a6cac333da` 中 **0314 是 `0314_tennis_on_demand_budget_read.sql`**，只定义 tennis 按需采集额度 read definer；涉及专用 operations 表和上游 application role，**不是通用效应账本，不进入 NexLoop lineage**。仅为核对编号读取这一 SQL，没有读取专用系统配置或生产产物。

可参考的通用账本实际是 `0040_external_write_attempts.sql`（其文件首注释仍写 0037，必须以文件/catalog 为准）与 `eios/adapters/postgres/external_write_attempts.py` / `eios/actions/write_attempts.py`：

- states 包含 started/completed/rejected/unknown/safe_to_retry/retry_claimed 等，使用 attempt_revision CAS 与 retry token；STARTED orphan 需要核对，UNKNOWN 可再核对。
- upstream key 是 tenant/request_digest，不是 NexLoop 稳定业务意图；metadata 带 facility_block_id/external_units，unknown 强制 SyncIssue ID，来自既有领域适配语义。
- 当前 NexLoop vendored `write_attempts.py` 仅保留契约与状态机；该上游 PG store 和迁移不在 extracted bootstrap。
- 上游 store 使用旧 runtime RPC、tenant GUC 与旧数据库角色/权限。不能整体复制迁移或假设其 RPC 在本仓库可用；需新的受治理、可验证 lineage 和 dispatch/query 权限边界。

## AT-033～037 的具体缺口

| ID | 已有可复用基础 | 缺失的直接证据/代码 |
|---|---|---|
| AT-033 | PG claim/fence 与 durable task | provider 已受理、本地记账前 SIGKILL；恢复查询同一 provider key/reference；发送计数不增加；无查询能力时保留 unknown 而不是重发 |
| AT-034 | claim binding/request_digest conflict | 业务入口规范化 payload 与稳定 intent；明确 409；原请求/receipt 不覆盖；并发差异 payload 也拒绝 |
| AT-035 | terminal replay | 可信意图服务从已有业务上下文找回 intent_id；新 tool_call_id 不改变业务 key 或稳定 invocation binding；工具返回同一 receipt |
| AT-036 | 当前 EIOS claim 权限重验 | external dispatch 当刻再次检查发布契约、grant/Agent ceiling、消费者约束/control revision/预算；受限 Action worker 在外发前拒绝且 provider 请求为零 |
| AT-037 | 同主体 claim 并发一个 owner | **当前 SQL 还比较 principal_id；两个不同主体同 key 会 conflict，并不是可共享 receipt 的多角色协议**。需可信意图归属/访问规则与同业务意图串行提交；不能删除 principal 检查来解决 |

`mark_retryable` 仅证明内部 claim 可以再领取，不能用来处理“外发成功但本地未记账”。`TerminalOutcomeStatus` 只有 succeeded/permanent_failure/compensated；unknown 应留在独立执行/attempt 状态，不伪造 permanent_failure 或 succeeded。provider reference、查询结论及证据不是 Pi tool 文本自报。

## 最薄可实施切片

1. **一个通用、明确注册的 Action + 一个 provider。** 使用既有已发布 Action 契约与 governed authorization；只选一个可验证的服务交付或消息效应。provider 协议声明 dispatch、query、幂等能力、状态语义与查询权。渠道/provider 凭据仅留在可信 Action worker 的专门端口，不进入 Runtime、prompt、日志或公共 fixture。无渠道凭据时先实现真实 HTTP 边界的受控 provider 故障测试；受控合成证据与真实渠道/实际服务交付分别记录，不能互相替代。
2. **可信稳定 Intent + 短事务受理。** Backend 从认证上下文与可信业务键建立/查找 business_intent_id，规范化 payload 固定哈希。新 tool_call_id、Pi 重开、owner 变化不另造意图。同 key 不同 payload 返回 409。Intent/Action receipt/dispatch outbox 原子落 PG 后才 ACK；尽量复用现有 claim/UoW，避免平行账本、root SQL 或假身份。
3. **独立 Action lease/fence 与 effect attempt。** 受限 Action worker 短事务 claim；在真正发送前重新验证当前 EIOS authority/发布契约和可变 controls，并持久化 dispatching attempt 与稳定 provider key。PG 事务结束后才进行 HTTP。Run/task lease 只限制调度，不自动授外发权。并发资源锁采用稳定顺序；结果提交检查同 attempt revision/fence。
4. **unknown 首先 query/reconcile。** 进程死亡留 dispatching/orphan，恢复必须查询原 provider key/reference。查询已受理/已完成则更新原 receipt；仍未知保持 unknown；只有可证明未受理且当前许可/预算/约束仍有效，才允许协议规定的重试。无幂等或查询能力则显式限制自动恢复，留待人工处理；不承诺通用 exactly-once。
5. **返回权威 receipt。** Pi execute_action 只提交/找回同意图，幂等与恢复证明后才声明 replay safe。receipt 区分 accepted、provider accepted、delivered/fulfilled/confirmed。取消/补偿是显式治理行为，不能抹去发生过的效应。
6. **先故障验收再扩场景。** clean PG + 独立持久受控 provider，覆盖 AT-033 kill 窗口、034 conflict、035新toolcall、036撤权、037不同主体并发；包含 fence 旧结果拒绝、provider查询失败、无幂等能力、PG不可达不外发。补充真实渠道或真实服务交付证据时只阻塞相关外部验证，不停掉独立代码与测试工作。

## 本次实际读取命令

- `rg -n 'NX-015|AT-033|AT-034|AT-035|AT-036|AT-037' planning/tasks.json planning/*.json`
- `cat docs/handoff/docs/06_ACTION_RUNTIME_AND_RECOVERY.md`；`rg -n` 阅读 docs/14、docs/15 的 S2/Action 条目。
- `cat packages/eios-core/src/nexloop_eios/postgres_action_claims.py`；`sed`/`rg` 阅读 Action models、0007、0010、0033、0034、现有 claim 测试。
- 首次读取预期 `nexloop_eios/governed_effects.py` 返回文件不存在；随后 `rg --files` 与 `rg -n 'governed_effects'` 仅搜索 NexLoop extracted core 和指定 frozen tree `src/`；未找到该模块。
- `head`/`cat` 阅读指定 frozen tree 的 0314、0040、generic external_write_attempts store；没有访问其他 NEX-EIOS 目录、production DB、凭据文件或执行 provider 调用。

这里只新增本审计文件。没有改 source/test、catalog/lock、planning、验收状态，也没有把任何未执行命令计为通过。
