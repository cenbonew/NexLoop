# NX-031 负载中的重复回复：原因、修复与检查

- **分支与迁移**：分支 `duplicate-replies`，从 main `9cbc2bf` 切出。迁移 `0440_nx031_one_reply_per_inbound_message.sql`（临时号）。
- **背景**：CI 主机 200 客户整轮（`d819d7d`，run `20261012T150722Z-d819d7d`）的 `duplicate_effects` 检查发现，同一条来信有多于一条 `delivery_state` 为 `provider_accepted` 或 `delivered` 的出站消息：C4-low 格 21 条，C8 格 1 条。
  - 积压下 Run 端到端 p95 达数百秒。
  - 这违反“外部效应不得重复”，属于 D-i 的立即停止条件。

## 1. 原因

一条来信最多可以有两个 Run：
- 原 Run：`authz.nexloop_message_run_issuances`，以消息为主键（0049）；
- 兜底 Run：`authz.nexloop_message_fallback_issuances`，同样以消息为主键（0110）。

两者的回复都写入 `runtime.nexloop_outbound_messages`。“一条来信只回复一次”的判定在派发时进行，位于 `control.nexloop_contact_assert_intent`（0110:742–753）：
1. 对消息取 advisory 锁；
2. 如果同一来信已有另一条回复处于 dispatching、provider_accepted、delivered 或 unknown，就拒绝（NXC05）。

但这条判定**只对兜底回复或被限制联系的消费者生效**（`if restricted or fallback`）。普通消费者的原 Run 回复在派发时不做这项检查。

积压下的时序：
1. 回复保障在来信被接受 300 s 后发现仍未回复；
2. 原 Run 的回复还在 `persisted`，在效应队列里排队，尚未派发；
3. 兜底签发检查（0110:422）只看 provider_accepted 和 delivered，于是放行；
4. 兜底回复先被派发并送达；
5. 之后原回复出队派发，没有任何检查，再次送达。

结果是两条已送达的回复。表上也没有每条来信最多一条的约束，`trigger_message_id` 上没有唯一索引。

**排除的路径**：
- 两个原 Run 或两个兜底 Run：两张签发表都以消息为主键，不可能；
- 投递重试或对账：更新的是按 `intent_id` 的同一行，不会产生第二行（0079 `nexloop_outbound_advance`）。

## 2. 修复（0440）

1. **函数内判定，适用于所有消费者**：最外层的 `control.nexloop_contact_assert_intent` 对每一条绑定来信的 Agent 回复（`runtime.nexloop_outbound_messages` 中有对应行，原回复和兜底回复都是）执行同一判定。
   - 位置：在接纳派发的同一个事务中，`authz.nexloop_assert_intent_dispatch_controls` → `admit`。
   - 锁：与 0110 相同的按消息 advisory 锁，所以原回复与兜底回复的接纳一先一后。
   - 拒绝条件：已有另一条回复处于 dispatching、provider_accepted、delivered 或 unknown，或员工已回复。
   - 拒绝结果：`inbound message already answered`（NXC05）。被拒的回复从未派发，执行器按 0152（`nexloop_fail_refused_effect`，原因 `already_answered`）把它和它的 outbox 置为 failed。它作为受治理的内部记录保留，不外发。
2. **数据库约束兜底**：部分唯一索引 `nexloop_outbound_messages_one_reply`，作用于 `(tenant_id, world, trigger_message_id)`，条件为 `delivery_state in ('dispatching','provider_accepted','delivered','unknown')`。
   - 函数先给出明确的拒绝码；
   - 任何绕过函数的路径，在进入可能到达客户的状态时都会因唯一冲突失败；
   - 已含此类重复的旧库，迁移会失败并回滚，需负责人先复核。负载模板每次由 catalog 重建，不受影响。

## 3. 测试

- **三种到达顺序**：`tests/test_one_reply_per_message_pg.py`。走真实链路：HTTPS 来信、relay、Pi Run、回复保障、本地 JSON 投递、effect worker。
  - **兜底先到、正式后到**（CI 上的情形）：用测试夹具延后原回复的 outbox，模拟积压。结果：兜底送达；原回复在派发时被拒（NXC05），置为 failed；只产生 1 份投递；再跑一次 worker 不产生新投递；对已送达回复本身重新检查不会被拒。
  - **同时到达**：两个 effect worker 并发接纳。恰好一条 delivered，另一条 failed（NXC05），1 份投递。
  - **正式先到、兜底后到**：兜底被拒，1 份投递。另外用测试夹具把被拒回复强行改为 dispatching 时，唯一索引报 `UniqueViolation`。
  - 每个测试本地连续运行 2 次，都通过。
  - **修复前对照**：临时移除 0440 后运行“兜底先到”用例，原回复在兜底之后再次 fulfilled，测试失败。这就是 CI 上的重复，在本地确定性地复现出来了。
- **共享夹具**：`tests/local_message_assembly_fixture.py` 为这三个用例各给 2 个效应单位，写法与已有的两 Run 用例相同。
- **夹具修正**：`tests/commitment_fixture.py` 之前让所有合成的已送达回复共用同一个 trigger（`'c'*64`），不符合“一条来信一条回复”，被新索引拒绝；现在每条合成回复对应自己的合成来信。
- **统计分拆**：`tests/test_load_duplicate_replies_pg.py`，见 §4。

## 4. 负载检查区分模板历史与窗口（`scripts/load/collect.py`）

- `duplicate_effects` 仍对整个租户统计。`violations` 是总数，report 的判定沿用它，因为任何重复都不可接受。新增两个计数：
  - `window_violations`：触发来信在本格窗口开始之后被接受；
  - `history_violations`：触发来信在窗口开始之前（模板历史）。
- 只报计数。
- 测试：构造模板历史、窗口内各一条重复，以及一条“送达加失败”（不算重复）。数据在事务内临时去掉索引写入，事务回滚后索引仍在。结果：总数 2，窗口 1，历史 1。

## 5. 本机负载复现

- 用于对照的 200 客户模板：构建在加入 0440 之前就开始了。
  - 历史回复阶段正常完成：299 条回复，0 条未回复。本机负载较低，没有形成 CI 上那样的积压，也就没有出现兜底先送达的时序。
  - 后续阶段 API 就绪检查返回 503。原因是构建途中工作树加入了 0440，磁盘上的 catalog 领先于集群。这是本次操作顺序造成的，与修复无关；该模板已删除。
- 没有再跑完整的 C4-low 格（预热 120 s、稳态 600 s、排空最长 600 s）。双投递的路径已由 §3 的确定性测试在真实链路上复现，修复前测试失败，修复后通过。
- 建议 CI 主机下一轮整轮在含 0440 的提交上核对：`duplicate_effects` 的 `violations`、`window_violations`、`history_violations` 都应为 0。

## 6. 影响与限制

- **行为变化**：普通消费者的一条来信，现在最多一条 Agent 回复能派发。原 Run 若对同一来信提交第二条不同的回复，第二条在派发时被拒并置为 failed。此前同一 Run 内的第二条回复已由意图槽位冲突拒绝（0040），因此正常流程不受影响。
- **员工回复**：员工已回复的来信，Agent 回复在派发时被拒，与 0127 对兜底和受限消费者的规则一致，现在扩展到所有消费者。
