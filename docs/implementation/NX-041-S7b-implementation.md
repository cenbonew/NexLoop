# NX-041 S7b：sim world 中 Run 的上下文来源

迁移 `0267_nx041_sim_context.sql`（临时编号，集成时由调度员重排）。契约 `packages/contracts/context-pack-sim.schema.json`。

## 方案与选择

**问题**：real 的装配入口在函数体里都写死了 `p_world='real'`，涉及 context v6、role context v6、fallback、manifest read、relationship context 和 consumer conversations。契约 v6 也是 real 专用：`bindings.world_id` 固定为 `real`，supply、goal、relationship 这些必填段都来自 real。所以 sim world 里的 Run 拿不到上下文。

**选择：新增一个只服务 sim/shadow 的装配入口和独立契约，real 的入口一字不改。** 理由有三条：

1. 如果在 real 入口里按 world 分流，就得证明 v6 的六个函数在 real 路径上逐字等价，而且 v6 契约本身也得放开 `world_id`。改动面大，real 回归风险高。
2. 独立入口只读模拟数据源，「不能读 real」可以直接用源码守护测试来固定。
3. 两种包的契约分开，就不会出现「模拟条目被当成 real 证据」的混用。

**来源**，只有以下三处：
- 合成 Consumer 的生成画像：`runtime.nexloop_sim_objects`，来自 S3；
- 人设：`control.nexloop_consumer_personas`，取该 sim world 中的行，值为 synthetic；
- 孪生经消费者端口写入的事件：`runtime.nexloop_sim_consumer_events`，来自 S7。

不读 real 的会话、消息、Claim、对象、记忆、recall 或关系数据。

**契约 `nexloop.context-pack.sim.v1`**：
- 每个条目沿用 v6 的条目形状：ref、revision、content、content_hash、at、tags。
- 每条都标 `source: simulated`。evidence_kind 只能取 `simulated_input`、`synthetic_profile`、`synthetic_persona`，都不是事实类。
- 上限：事件最多 256 条，consumer_state 最多 16 条。
- 被截掉的旧事件记在 `budget_report` 中，并在 `insufficient` 里给出 `older_events_omitted`；没有任何事件时给出 `no_simulated_events`。
- 世界时间取 `world_now`（S5）。

**入口** `authz.nexloop_sim_context_command(digest, world, consumer, limit)`：
- 调用方：nexloop_api 和 nexloop_domain_worker，用 sim world 唯一的凭据认证。
- real 和 test 在任何查询之前就被拒绝（S4 的 `sim_effect_tenant`）。
- Consumer 必须是该 world 里生成的合成 Consumer。

## 测试

`tests/test_nx041_s7b_pg.py`：

- **装配内容**：先生成数据，再经端口写入 4 条事件，然后装配（上限 3）：
  - 结果符合契约；
  - 画像和人设都来自模拟数据；
  - 证据是最新的 3 条事件，按从旧到新排列，最新一条标 latest_consumer_turn，也作为 current_event；
  - budget 和 insufficient 正确；
  - 重复装配结果一致。
- **负例**：在 real 里放一个同 id 的 Consumer 对象、一段会话和一条消息，都带标记。sim 装配结果里不出现这个标记。
- **没有事件的 Consumer**：给出显式的 insufficient；这条用 domain_worker 角色装配。
- **拒绝**：real、test、另一个 sim world、未知 Consumer、越界的 limit、端口角色和替身角色，都被拒绝。有阳性对照。
- **守护**：
  - sim 入口的源码里只出现模拟表和辅助函数，不出现任何 real 来源；
  - 本迁移及以后的迁移都没有重新定义 real 的六个装配入口。阳性对照：扫描确实能在更早的迁移里找到 `context_v6_command` 的定义。

## 限制与后续

- Agent Host 和 runtime worker 还没有走 sim 包的 Run 输入协议（`context_input_protocol`）。真正在 sim world 里跑一个 Run，仍然要等 D10 定下来、S6c 完成角色授权，并加上 Host 的 sim 输入协议。本切片只解决「上下文从哪里来」。
- 包里没有 goal、supply、relationship 段。这几类在 sim world 里没有模拟来源，要等 NX-042、NX-043 定义实验目标和模拟供给之后再加。

## 审查修复（L6 审查 `nx041-s7b-review.md`）

| 编号 | 修复 |
| --- | --- |
| 1.3 / 1.1 | **以行为测试为主**：`seed_real_marks` 现在还会在 real 的 persona 表里，为同一个 Consumer 写一行（persona 为 real，revision 7，带标记）。测试断言 sim 包里既没有对象、会话、消息的标记，也没有 real persona 的标记，读到的 persona 是 synthetic，revision 不是 7。**守护测试补了三处**：一是入口和它直接调用的五个 helper（递归一层：`sim_consumer_known`、`consumer_persona`、`sim_effect_tenant`、`world_now`、`commercial_ts`）里都不能出现 `execute`，也就是不允许动态 SQL；二是它们引用的名字只能是模拟来源、身份辅助函数，或者彼此；三是不能出现 real 的会话、消息、Claim、对象、记忆、recall 或关系来源，也不能有 `world='real'` 这样的读取条件。另外固定一条规则：凡是读 persona 共用表的语句，都必须带 `world=p_world`。`consumer_persona` 在 Consumer 没有标记时返回默认值 `'real'`，这是一个值，不是读取 real，守护测试只拦「按 real 读取」的写法。 |
| 2.2 | 参数校验移到凭据认证之前：格式不对的请求，无论凭据如何，得到的都是同一个错误。 |
| 3.3 | `content_hash` 改为对条目里实际给出的 `content` 计算，即它的 jsonb 文本的 sha256，与 consumer_state 的算法一致。契约里也写明了计算对象。测试会对每个条目用 `content` 重算哈希，结果必须一致。 |
| 3.4 | Host 输入协议本身暂时不做，等 D10 定下来。四项前置验收写成了 vitest 的 `it.fails`，作用相当于 strict xfail，测试文件是 `apps/agent-host/test/nx041-sim-context-protocol.test.ts`：simulation 组的 Host 只接受 sim 包；real 组的 Host 拒收 sim 包；渲染时必须带固定前缀「【模拟环境】以下均为模拟输入，不是真实客户或真实事实。」；sim 包不能回退到 v6，读不了的 sim 包要拒绝，不能拿 v6 顶替。接口名在测试里固定为 `contextProtocolAccepts` 和 `renderSimulationContext`。协议实现并满足这些验收后，`it.fails` 会变红，提示改成普通测试。另有一条普通测试，断言 Host 现在还不读取 sim 包。这和 `pi_run_blockers` 里的 Host 前提（Host 是否读取 `nexloop.context-pack.sim.v1`）同时翻转。 |
| 4.2 | **依赖链说明**：real 的六个装配入口，函数体一字未改，但它们调用的 helper 已经在 S1、S5 中重定义过：`authz.nexloop_claim_contract` 和 `ontology.nexloop_recall_tenant` 在 0259（S1），`runtime.nexloop_outreach_gates` 和 `runtime.nexloop_outreach_metrics` 在 S5。这些改动在 real 和 test 下与原来等价，已由 S1、S5 的审查逐字核对过。**金样回归** `tests/test_nx041_s7b_v6_golden_pg.py`：用 v6 夹具的固定输入（一条客户消息、内置策略、没有 Claim）走 real 路径装配，把 ID、digest、时间按首次出现的顺序替换成占位符，再和 `tests/support/golden/nx041-context-v6-real.json` 逐字节比较。金样是在本分支上生成的，这里的 real 路径和集成前一致，连跑两次输出相同。以后只有经过评审的改动，才能用 `NEXLOOP_WRITE_GOLDEN=1` 重写金样。 |
