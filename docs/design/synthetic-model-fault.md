# 设计：测试顾客的模型调用故障开关（ADR-045）

- **状态**：设计稿 v1，待 L6 复核，通过后实现。作者：L7。依据：main 7cee60cc。迁移临时号 0423（0421 为第 129 批，0422 为 L2 的预算健康）。
- **目标**：
  - 在 stage 上让指定测试顾客的若干个消息 Run 真的失败（outcome error），失败口径不变，从而满足 NX-038/039 验收的前提 P5；
  - 不发效应，不碰对账，真实顾客不受任何影响，不进入生产配置。

## 1. 判定放在哪里：Run 准入时，由 SQL 判定

- **为什么不在 Host 里判定**：
  - 上下文包里没有 Consumer 的 persona。v6 的 `operator_persona` 是运营方的说话人设，不是 ADR-034 的测试人设；
  - 往包里加字段要改已发布的契约（字节冻结）；
  - Host 也没有数据库访问权限。
- **为什么放在准入**：
  - 每个 Run 开始（start/resume）时，Host 都要先经守卫向 SQL 申请准入（`authorizeAdmission` → backend → 0039/0107 的准入函数，应答里带 `ever_execution_authorized`）；
  - 这条通道是服务端签名的内部通道，不在 `packages/contracts` 里；
  - 顾客输入不经过它，Host 也改不了它。
- **SQL 判定**：`runtime.nexloop_synthetic_fault_take(p_tenant, p_world, p_run_id)`，在准入函数里调用，返回 true 或 false。同时满足下列条件才返回 true：
  1. world 为 real，Run 是消息 Run（`runtime.nexloop_message_routes` 里有它）；
  2. 这个 Run 的 Consumer 经 `runtime.nexloop_evo_run_consumer` 得出，persona 经 `runtime.nexloop_consumer_persona` 得出，属于 `synthetic` 或 `simulated_agent`；
  3. 租户有一个打开的开关：`closed_at is null`，现在早于 `expires_at`，`remaining > 0`，并且这个 Consumer 在开关的 `consumer_ids` 里；
  4. 这个 Run 还没有被判定过：按 run_id 幂等，resume 时返回同样的结果，不再扣次数。
- 命中时，在同一个事务里：`remaining` 减 1，审计表写一条 `injected` 事件（run_id、consumer 的摘要、剩余次数），准入应答加上 `synthetic_model_fault: true`。
- 次数扣到 0 时写一条 `exhausted` 事件。到期时间过后第一次被判定时写一条 `expired` 事件，同时把开关记为关闭。
- 兜底回复 Run（`runtime.nexloop_fallback_context_bindings`）、角色 Run 和计划 Run 都不判定，保证"每条来信都有回复"（§5）。

## 2. Host：模型请求之前

- **新增运行时配置键 `synthetic_model_fault`**（布尔值）：
  - 只接受 true；
  - 只在 runtime_profile 为 deepseek-flash、world_group 为 real 时接受，其他组合在启动时拒绝；
  - 只出现在 stage 的运行时配置模板里。
- **准入应答带 `synthetic_model_fault: true` 时**：
  - Host 没开这个配置键：拒绝这次准入，报 `invalid_runtime_request`。fail closed：配置不一致时 Run 不开始，比静默忽略标记更安全；
  - Host 开了这个配置键：在这个 Run 的**第一次**模型请求之前（`model-fetch.ts`），直接以模型错误结束这次调用，错误码 `synthetic_model_fault`。
- **结果**：
  - Run 记 `model.failed = 1`、`model.calls = 1`，outcome 为 error；
  - 没有工具调用，没有效应，没有不明结果；
  - 消息 Run 失败以后，按现有逻辑由回复保证在 300 秒后发起兜底回复。
- 演练只伪造模型这一侧的错误，不改变 Host 的其他行为。错误码 `synthetic_model_fault` 进入证据包白名单，便于和真实的模型故障区分。

## 3. 受治理 Action 与表（迁移 0423）

- **`control.nexloop_synthetic_fault_switches`**：
  - 列：tenant_id、world、switch_id、`consumer_ids text[]`（1–8 个）、`remaining`（0–50）、`expires_at`（不超过打开时间 + 2 小时）、opened_by、opened_at、closed_at、close_reason（closed / exhausted / expired）、reason（1–500 字）；
  - 每个租户、每个 world 同时最多一个打开的开关（部分唯一索引）。
- **`runtime.nexloop_synthetic_fault_events`**：只追加（append-only 触发器），记 opened、closed、injected、exhausted、expired。只记 run_id 和 Consumer 的摘要，不记消息正文。
- **Action `nexloop.synthetic_fault.set:1`**：
  - 只有负责人能执行；
  - 请求体 `{operation: open | close, consumer_ids, remaining, duration_seconds (60..7200), reason, request_id}`；
  - 打开时逐个核对 `consumer_ids` 的 persona 都是 synthetic 或 simulated_agent，有一个是 real 就整体拒绝（`consumer_not_synthetic`）；
  - 已经有打开的开关时拒绝（`switch_open`）；
  - 关闭操作幂等。
- **Action 只发布到 stage**：放进 stage 专用的发布清单（`deploy/stage/...`），系统目录和生产清单都不含它。check_publication 新增规则：公开文件里，stage 部署文件之外出现 `synthetic_model_fault` 或 `nexloop.synthetic_fault.set` 都拒绝。
- **准入函数**：在当前最新定义里加一处调用和一个应答字段，copy guard 按 `Allowed(stem, fn, why, reverts)` 登记。
- **擦除**：两张新表都含 consumer_id 或其摘要，要加进 consumer 擦除覆盖（erasure coverage）：擦除时从名单里移除该 Consumer，审计表只保留摘要。

## 4. 自动失效

- 剩余次数 N 和最长 2 小时两条都要，哪个先到就按哪个失效。
- 本次演练建议 N = 12（4 个顾客 × 3 轮），时长 60 分钟。
- 忘了关闭也会自动失效：失效后的判定一律返回 false，并写一条 expired 事件。

## 5. 后果与指标

- **兜底回复照常**：失败的消息 Run 在 300 秒后由兜底回复 Run 接手。兜底 Run 不判定故障（§1），用 `recent_plus_required`、真实模型回复，所以每条来信仍有回复（ADR-023）。
  - 兜底 Run 不做语义读取，但同一条消息的消息 Run 已经记过读取，所以检索未命中仍然记在失败的那个 Run 上。
  - 按现行口径，失败的消息 Run 既算 error，又算 reply_fallback，两者不冲突。
- **S6 归因**：按现行口径，这些 Run 记为失败。带检索未命中的，进入 retrieval_miss 草稿。S6 的证据要写明"失败来自 ADR-045 故障开关"，并引用审计表的 injected 事件。
- **错误类指标和 B5 失败率会被污染，处理办法**：
  1. **单列**：快照新增 `synthetic_fault` 块：`{active: 0|1, injected_last_hour, remaining}`，都是数字。告警说明和运行单写明：`active=1` 期间的错误类告警属于预期。
  2. **B5 门控排除**：
     - 0418 的 `outcomes` 统计的是派发拒绝、效应意图和队列死信，本身不含模型失败的 Run；
     - 但兜底和失败的 job 可能进入死信计数，所以 0423 让 outcomes 的 `decided` 和 `failures` 减去同一小时内 injected 的 Run，并在块里加 `excluded_synthetic_fault`；
     - 这一处按 0418 的 rename-and-wrap 方式实现，不重定义现有函数；
     - 如果 L6 认为这样太宽，就退回只做第 1 条"单列"。
  3. **error_rate 自动暂停**：NX-037 试运行入口（`run_interactions.py`）在错误率高时会自动暂停。本次演练不用它观察（直接用 `speak-turn.sh` 发言），运行单里写明不要同时开着 run 入口。
- **语义读取冷池超时**（2026-11-07 实测：两个 relay 各超时 1 次，时限 0.5 秒）：
  - relay 刚启动后的第一次读取可能超时，这个 Run 就没有 access 记录，也就没有检索未命中；
  - Run 照样会因故障而失败，只是不会进入草稿。
  - 影响：每个 relay 第一轮可能浪费一次。运行单里先让每个顾客发一轮"预热"消息，不扣开关次数：在打开开关之前发。每个顾客发 3 轮，至少有 2 轮带未命中的失败。
  - 不改 0.5 秒时限，那是 L6 定的读取上限。

## 5a. 否决的做法

| 做法 | 否决理由 |
|---|---|
| 参考端点延迟演练（`--delay-next-posts`） | 不明结果会被查询和对账收敛，Run 成功（2026-11-07 实测 ca29349d） |
| 再让对账失败（`--get-unavailable-seconds`） | 违背"不明结果先对账"，等于用"不明"演练伪造失败结局 |
| 在上下文包里标 persona，由 Host 判定 | 要改已发布的 context-pack 契约（字节冻结），而且 Host 不能自己核实 persona |
| 按 Consumer 路由到必然失败的模型配置 | 模型档案是整台 Host 的设置，没有按 Consumer 路由的入口 |
| 改失败口径 | 负责人 10-29 决定不改 |

## 6. 测试（实现时）

1. **真实顾客不受影响**：persona 为 real 的 Consumer，即使在名单里（打开时就会被拒绝，这里直接写表构造），也在有效期内、剩余次数大于 0，准入照常，Host 不报错，次数不变。
2. 测试顾客命中：准入应答带标记；Host 在第一次模型请求前失败，`model.failed = 1`，没有工具调用，没有效应意图；审计表有 injected；次数减 1。
3. 次数用完、到期、已关闭、不在名单里：都不命中。
4. resume 同一个 Run：结果相同，不重复扣次数。
5. 兜底 Run、角色 Run、计划 Run 不判定。
6. Host 没有配置键而准入带标记：拒绝准入；配置键只在 deepseek-flash 和 real 时被接受。
7. Action：只有负责人能执行；名单里有 real 就拒绝；已有打开的开关时拒绝；关闭幂等；审计只追加。
8. 快照：`synthetic_fault` 块；B5 门控排除注入的 Run（或只单列，按 L6 的意见）。
9. check_publication 规则：stage 之外的文件出现这个键或 Action 就拒绝。
10. 擦除覆盖；copy guard；bootstrap、published migrations、claim_store、tenant_fk、search_path、Host vitest。

## 7. stage 运行单草稿（实现后写进 `docs/release/run-synthetic-model-fault.md`）

**前提**
- 0423 已部署；stage Host 的运行时配置加了 `synthetic_model_fault: true`，重建 Host；Action 已发布。
- 两个 relay 用 `ontology_hybrid`（已核对，2026-11-07）。
- run 入口没有在运行。
- 模型和 evolution 预算都有效。

**第 4 个测试顾客**
- 失败发生在模型这一侧，不需要下单，所以不要求这个顾客能下单，只要能经 WebChat 发言、persona 为 simulated_agent 即可。
- 候选：
  - **entry-test-consumer**（1456b530，simulated_agent）：负责人签发入口票据后在浏览器里发言（ADR-043）；
  - 其余没被擦除的 simulated_agent Consumer（0c7c4410、6db782c3、8ecad01e、ac8a9011、e411740e）里，有 active 的 WebChat 账号的那个，按负责人保管的口令走 `speak-turn.sh`。
- 只读查询哪些 Consumer 有可登录的账号（DATA_HOST）：

```bash
ro "select left(o.consumer_id,8), p.persona, a.payload->>'status' account_status from control.nexloop_consumer_owners o join control.nexloop_consumer_personas p on p.tenant_id=o.tenant_id and p.world='real' and p.consumer_id=o.consumer_id join control.nexloop_browser_memberships m on m.tenant_id=o.tenant_id and m.principal_id=o.principal_id join control.nexloop_browser_accounts a on a.tenant_id=m.tenant_id and a.subject_id=m.subject_id where o.tenant_id='$TENANT_ID' and p.persona in ('simulated_agent','synthetic') order by 1"
```
  - `consumer_owners` 的列名请先在 stage 上 `\d` 确认。

**步骤**
1. **预热**（开关关闭）：s6c-1、s6c-2、s6c-3 和第 4 个顾客各发一轮普通问题，让两个 relay 的语义读取池热起来。
2. **打开开关**：负责人执行 `nexloop.synthetic_fault.set`（open），`consumer_ids` 为这 4 个，`remaining` 12，`duration_seconds` 3600，reason 写"NX-038/039 S6 P5（ADR-045）"。
3. **发言**：4 个顾客各发 3 轮，间隔不少于 1 分钟。
   - 每轮都带同一个未知词，例如"霁蓝釉陶片拼贴"；
   - 每个顾客再各加一个不同的词，例如 s6c-1 用"按北欧色卡"、s6c-2 用"榫卯拼接"、s6c-3 用"冷压工艺"、第 4 个用"盐釉"。
4. 每轮之后，用只读查询核对这个 Run：`failure` 或 `model.failed` 显示失败，并且 `misses` ≥ 1（查询见前一份说明）。
5. **关闭开关**（close），或者等次数用完；核对审计表有 12 条 injected 和 1 条 closed 或 exhausted。
6. 等兜底回复都发出后（约 5 分钟）：

```bash
uv run --frozen python scripts/acceptance/s6_stage_inputs.py --config ~/nx-s6/s6.json
```
   预期：`consumers_with_miss` ≥ 4，window 不再 blocked。

7. 按 run-nx038 的顺序执行 at023、r1；r5、r7 按 S6-stage-acceptance 的顺序和前提执行。
8. **证据**：injected 审计条数、run_id 前缀、各验收项的结果。明确写"失败由 ADR-045 故障开关注入"，执行者和判定者分开写。
