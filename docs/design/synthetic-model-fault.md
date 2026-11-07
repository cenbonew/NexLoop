# 设计：测试顾客的模型调用故障开关（ADR-045）

- **状态**：v3 已实现（v3 1d13726e 由 L6 判条件 PASS，C1/C2 在实现中完成，见 §8 实现记录）。作者：L7。依据：main 7cee60cc。迁移临时号 0423（0421 为第 129 批，0422 为 L2 的预算健康）。
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
  4. **只在首次 start 时判定（D2）**：只有 start 时存在生效的开关，才把判定结果（命中或未命中）按 run_id 写入 `runtime.nexloop_synthetic_fault_decisions(run_id, injected boolean)`；resume 一律沿用首次结果，**查不到判定行就当作未命中**，不重新判定、不扣次数。没开开关时完全不写表。这样 start 时开关关闭、之后才打开开关的 Run，resume 时也不会中途变成故障。
- 命中时，在同一个事务里：`remaining` 减 1，审计表写一条 `injected` 事件（run_id、consumer 的摘要、剩余次数），准入应答加上 `synthetic_model_fault: true`。
- 次数扣到 0 时写一条 `exhausted` 事件。到期时间过后第一次被判定时写一条 `expired` 事件，同时把开关记为关闭。
- **开关"是否生效"一律实时计算（D1）**：判定、快照和 Action 都按"`closed_at` 为空、现在早于 `expires_at`、`remaining > 0`"计算，不只看 `closed_at` 这一列。过期以后即使没有任何 Run 来触发它，也已经不生效。
- 兜底回复 Run（`runtime.nexloop_fallback_context_bindings`）、角色 Run 和计划 Run 都不判定，保证"每条来信都有回复"（§5）。

## 2. Host：模型请求之前

- **失败位置（v3）**：在 `pi-runtime-adapter.ts` 的 `guardedModels.streamSimple` 里，**在 `consume('model')` 之前**检查：这次模型调用不经守卫登记、不编号、不连 provider，Run 直接以 `RuntimeError('synthetic_model_fault')` 失败。worker 把它写进 job 结果，S6 证据的 `failure`（`runtime.nexloop_nx037_run_evidence`，按失败 job 的 failure_code）不为空，归因按现行口径判为失败（`attribution._failed` 的 failures 分支）。`model.calls`、`model.failed` 都是 0，不伪造任何 provider 结果记录，模型费用预留也不涉及。
- **新增运行时配置键 `synthetic_model_fault`**（布尔值）：
  - 只接受 true；
  - 只在 runtime_profile 为 deepseek-flash、world_group 为 real 时接受，其他组合在启动时拒绝；
  - 只出现在 stage 的运行时配置模板里。
- **准入应答带 `synthetic_model_fault: true` 时**：
  - Host 没开这个配置键：拒绝这次准入，报 `invalid_runtime_request`。fail closed：配置不一致时 Run 不开始，比静默忽略标记更安全；
  - Host 开了这个配置键：在这个 Run 的**第一次**模型请求之前（`model-fetch.ts`），直接以模型错误结束这次调用，错误码 `synthetic_model_fault`。
- **结果**：
  - Run 的 failure 为 `synthetic_model_fault`，`model.calls = 0`，按现行口径算 error 失败；
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
  - **打开前先清理旧开关（D1）**：已过期或次数已用完、但 `closed_at` 还为空的旧开关，先记为关闭（close_reason expired 或 exhausted）并写审计，然后再新建。只有仍然生效的开关存在时，才拒绝（`switch_open`）。部分唯一索引只约束 `closed_at is null` 的行，所以必须先关旧的；
  - 关闭操作幂等。
- **Action 只发布到 stage（v3，调度员 2026-11-07 选定 (A)）**：
  - 新建 **stage 专用能力清单** `deploy/stage/capabilities.stage.v1.json`，格式与 `capabilities.v1.json` 相同，只含 `nexloop.synthetic_fault.set:1`。系统目录不含它；
  - `stage_secrets.py publish --stage-capabilities deploy/stage/capabilities.stage.v1.json`：`catalog_publish.compile_catalog` 增加一个可选参数 `stage_capabilities_path`，把它合并进编译结果，名字重复就拒绝。发布结果和发布记录（catalog publication 的审计）写上 `stage_manifest: {path, sha256}`。不带这个参数时，与现在完全一样；
  - 其他调用方拒绝：`compile_catalog` 只接受位于 `deploy/stage/` 下、文件名为 `capabilities.stage.v*.json` 的 stage 清单；只有 `stage_secrets` 这个入口会传这个参数，其他入口传了就拒绝；
  - check_publication 新增规则：`deploy/` 之下、`deploy/stage/` 之外的文件提到 `capabilities.stage`、`synthetic_model_fault` 或 `nexloop.synthetic_fault.set` 就拒绝（docs、adr、tests 不受限）；
  - 以后其他 stage 专用的演练能力也放进这份清单。
- **全局注册的处理（v3）**：
  - `workbench_actions_http` 的参数校验、`goal_controls` 的 operation 映射和 `api-routes` 的路由都是全局的，生产上也有这条路由；
  - 但 Action 在 SQL 层不存在时，工作台入口按现有逻辑返回 **404 `Action not published`**，不会 500，也不会静默通过；
  - route inventory 的测试要覆盖这一点：未合并 stage 清单的租户调这条路由，得到 404 且什么都不写。
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
  2. **B5 不做扣减，只单列（D3，L6）**：0418 的 `outcomes` 统计的是派发拒绝、效应意图和队列死信，本身不含模型失败的 Run；再减去被注入的 Run，会低估失败率，甚至算出负数。所以 B5 不改，只靠上面的 `synthetic_fault` 块单列；B5 的证据里注明那段时间开关打开着。
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

0. **D1**：开关过期后，没有任何 Run 触发它，快照照样显示 `active = 0`；再打开新开关能成功，旧开关被记为 expired 关闭并有审计。
0a. **D2**：start 时开关关闭（判定为未命中并记录），之后打开开关，再 resume 这个 Run：不受影响，次数不变。
1. **真实顾客不受影响**：persona 为 real 的 Consumer，即使在名单里（打开时就会被拒绝，这里直接写表构造），也在有效期内、剩余次数大于 0，准入照常，Host 不报错，次数不变。
2. 测试顾客命中：准入应答带标记；Host 在第一次模型请求前失败，`model.failed = 1`，没有工具调用，没有效应意图；审计表有 injected；次数减 1。
3. 次数用完、到期、已关闭、不在名单里：都不命中。
4. resume 同一个 Run：结果相同，不重复扣次数。
5. 兜底 Run、角色 Run、计划 Run 不判定。
6. Host 没有配置键而准入带标记：拒绝准入；配置键只在 deepseek-flash 和 real 时被接受。
7. Action：只有负责人能执行；名单里有 real 就拒绝；已有打开的开关时拒绝；关闭幂等；审计只追加。
8. 快照：`synthetic_fault` 块（active 实时计算）；B5 的 outcomes 不变（D3）。
8a. **v3**：命中的 Run 在 S6 归因里判为失败（`failures` 非空、`model.calls` 为 0）；
    - 只有 `stage_secrets publish --stage-capabilities` 会发布这个 Action，发布结果里写有 stage 清单的 sha256；
    - `compile_catalog` 收到其他路径的 stage 清单会拒绝；
    - 不带 stage 清单发布时，这条路由返回 404 `Action not published`；
    - check_publication 拒绝 `deploy/` 下、`deploy/stage/` 外的引用。
9. check_publication 规则：stage 之外的文件出现这个键或 Action 就拒绝。
10. 擦除覆盖；copy guard；bootstrap、published migrations、claim_store、tenant_fk、search_path、Host vitest。

## 7. stage 运行单草稿（实现后写进 `docs/release/run-synthetic-model-fault.md`）

**前提**
- **生产隔离核对**：生产渲染之后，只读确认生产库没有发布这个 Action，两张开关表都是空的：

```bash
ro "select count(*) from control.nexloop_action_definitions where resource_id like 'eios:action:nexloop.synthetic_fault.set:%'"
```
```bash
ro "select count(*) from control.nexloop_synthetic_fault_switches"
```
  两条都应为 0。另外判定表和审计表也应为 0 行：

```bash
ro "select (select count(*) from runtime.nexloop_synthetic_fault_decisions), (select count(*) from runtime.nexloop_synthetic_fault_events)"
```
  这一步写进生产部署单，每次生产发布都要做。
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

## 8. 修订记录

### v2（2026-11-07，L6 对 v1 的条件 PASS：D1–D4）

- **D1 开关过期后会卡住**：打开前先把已过期或次数用完、但 `closed_at` 还为空的旧开关记为关闭再新建；"是否生效"一律按"未关闭、未到期、剩余次数大于 0"实时计算（§1、§3）。用例见 §6 第 0 项。
- **D2 resume 中途变成故障**：只在首次 start 时判定，结果（命中或未命中）按 run_id 记下，resume 一律沿用（§1）。用例见 §6 第 0a 项。
- **D3 B5 不做扣减**：只单列 `synthetic_fault` 块，B5 证据里注明开关打开的时段（§5）。
- **D4 persona 的写入方**（main 7cee60cc，git grep `control.nexloop_consumer_personas`）：
  - 写入：
    - 0220 `control.nexloop_governed_set_consumer_persona`，即受治理 Action `nexloop.consumer.set_persona`，只有负责人能执行，带修订号和审计（`nexloop_consumer_persona_events`）；
    - 0261 仿真生成（`runtime.nexloop_sim_*`），只写 sim 世界的合成 Consumer，0261:255 的 world 约束保证它不写 real。开关只在 real 判定，所以这条路径与本开关无关。
  - 删除：0227:716、0358:262，都在擦除清理里，删除后视为 real。
  - Python、脚本里没有直接写这张表的语句（`webchat_consumers.py`、`entry_tickets.py` 只读 persona）。
  - 所以 persona 只能由负责人的受治理 Action 改动，顾客消息、渠道输入和上下文包都影响不了它。
- **D4 已知局限（0061 consumer identity link）**：
  - `nexloop.consumer.link_authenticated_identity` 只允许已登录的本人，把自己的浏览器身份关联到**自己名下**的 Consumer（`control.nexloop_consumer_owners` 的 principal 与登录会话一致）。所以一个真实的人不能被关联到别人名下的模拟顾客上。
  - 但持有测试账号口令或入口票据的人，就是以这个模拟顾客的身份在说话。如果真人登录了测试账号，开关打开期间他的消息 Run 会失败。
  - 这时兜底回复在 300 秒后照常回复，影响有限；运行单要求演练期间测试账号只由执行者使用。
- **生产隔离的真正依据（L6）**：
  - 是"**生产库没有这个 Action，两张表是空的**"：没有 Action 就打不开开关，没有打开的开关就不会有任何判定命中。
  - Host 的运行档案限制挡不住生产：生产也可能是 deepseek-flash 加 real。
  - check_publication 只看仓库里的公开文件，看不到私有的运行时配置。
  - 这两道只是补充；生产部署单要加一步只读确认（§7 前提）。

### v3（2026-11-07，调度员选定 (A)；L6 对 v2 的实现前提）

- **生产隔离的机制**：stage 专用能力清单 `deploy/stage/capabilities.stage.v1.json`，只有 `stage_secrets publish --stage-capabilities` 显式合并它，并写进发布记录；其他入口拒绝；check_publication 禁止 `deploy/stage/` 之外引用它（§3）。
- **隔离依据不变**：生产库里没有这个 Action。
- **否决的 (B)**：把 Action 放进系统目录、靠部署开关挡住，隔离依据就退化成"靠开关挡住"，攻击面留在生产上（调度员 2026-11-07）。
- **全局注册**：Action 未发布时，路由返回 404 `Action not published`，route inventory 测试覆盖（§3）。
- **Host 的失败位置**：在登记模型调用之前，Run 的 failure 为 `synthetic_model_fault`，不再记 `model.failed`（§2）；补测试证明 S6 归因仍判为失败（§6 第 8a 项）。
- **判定表不随每个 Run 增长（L6 实现前提 1）**：只有 start 时存在生效的开关才写判定行；resume 查不到判定行就当作未命中。没开开关时完全不写表。判定表不含顾客数据（只有 run_id、是否命中、开关 id），在擦除覆盖里注明，保留 30 天后由 retention keeper 清理；审计表含 Consumer 摘要，纳入 consumer 和 member 擦除覆盖。
- **生产只读确认（L6 实现前提 2）**：Action 定义、开关表、判定表、审计表都应为 0 行（§7 前提）。

### 实现记录（2026-11-07，L6 对 v3 的 C1、C2 及实现中定下的细节）

- **C1 事前拦截**：
  - `stage_secrets.py publish --stage-capabilities` 必须同时给出 `--deployment-marker <私有文件>`；
  - 标记文件为 0600 JSON：`{"schema": "nexloop-deployment-marker/1", "environment": "stage" | "production"}`。仓库里原来没有现成的部署环境标记（stage_secrets 的私有 params 没有 environment 字段），所以新增这个文件，由调度员放在 stage 的私有部署目录里；
  - 不是 stage、文件缺失、读不了或形状不对，一律在编译和发送之前拒绝，退出码非 0；
  - 只看命令行参数和这个文件，不读任何环境变量。
- **C2 404 不写入**：
  - 没发布这个 Action 的租户调 `set_synthetic_fault`，得到 404 `not_found`；
  - authz、control、runtime、ontology 四个 schema 的**每一张表**行数都不变（含 `runtime.audit_events`、`runtime.nexloop_action_claims`、受治理入口的意图和回执表、审批表，以及开关、名单、判定、审计四张表）；
  - **实际写入**：工作台会话的活动时间（`last_seen_at` / `idle_expires_at`，更新已有的会话行），每个已登录的工作台请求都会这样；这条路由没有速率计数。测试 `tests/test_synthetic_fault_route_pg.py`。
- **Host 的失败码**：失败的 Run 记 `failure_code: runtime_task_failed`、`failure_detail: synthetic_model_fault`。Host 的 provider 边界有一个错误码白名单（`runtime-provider-boundary.ts` 的 `safeCodes`），白名单外的码都会被换成 `runtime_model_unavailable`，所以把 `synthetic_model_fault` 加进白名单；证据包白名单（`scripts/nx037/evidence_pack.py`）也加上了。S6 归因按 failures 判为失败（`tests/test_stage_capabilities.py`）。
- **表结构**（与 §3 的差异）：
  - 开关的 Consumer 名单拆成子表 `control.nexloop_synthetic_fault_members(consumer_id)`；Consumer 擦除登记（`control.nexloop_consumer_erasures` 的插入）由触发器 `control.nexloop_synthetic_fault_erasure_members` 立即删掉这个 Consumer，纳入 consumer 擦除覆盖；
  - 审计表**不存 Consumer 摘要**，只有开关、Run、剩余次数和负责人的 principal，没有顾客数据；
  - 开关表和审计表的 `principal_id` 是负责人，作为员工操作登记进 consumer 和 member 两份擦除覆盖。
- **判定表的保留**：只存 run_id、开关 id 和是否命中，不含顾客数据，没有身份列，所以不在擦除范围内，也不清理；只有开关生效期间才写，每个开关最多 50 次命中。原 §8 v3 里"保留 30 天"的说法作废。
- **判定的接入点**：不重定义准入函数 `authz.nexloop_runtime_activation_command`，它包了很多层，又授权给 sim 等多个角色。改为在服务端 `nexloop_eios.runtime_activation.RuntimeActivationPort.authorize` 里：SQL 授权 start 或 resume 通过之后，在 real 世界调用 `authz.nexloop_synthetic_fault_decide`（授权给 nexloop_api 和 nexloop_domain_worker：真实守卫跑在 runtime-worker 进程里，角色是后者），命中才在应答里加 `synthetic_model_fault: true`。判定仍完全在服务端由数据库完成，Host 改不了。
- **请求体**：受治理能力登记表里一个能力只对应一个 operation，所以开和关合成一个 operation `set_synthetic_fault`，用 `state: open | closed` 区分，键集合固定：`state, consumer_ids, remaining, duration_seconds, reason`；关闭时 `consumer_ids` 为 `[]`，另两个数为 0。
- **负责人的授权**：全局角色清单 `workbench-roles.v1.json` 不含这个 Action，否则生产库也会写入一条指向不存在 Action 的授权。stage 清单里的 `owner_actions` 由 `nexloop_eios.workbench_roles --apply --stage-capabilities --deployment-marker` 合并进负责人角色，规则与发布相同（标记必须是 stage，否则在应用之前拒绝）。
