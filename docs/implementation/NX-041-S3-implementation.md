# NX-041 S3：合成数据与结构生成（D8，§2.5，§11 S3）

迁移文件为 `0261_nx041_sim_generation.sql`，编号是临时的，只在文件名和 catalog 里出现。

## 做了什么

- **governed owner Action**：`sim_world.generate` / `generate_sim_world_data`。
  - 类型为 human，只有 owner 能调用，并且只能从 owner 的 real 世界发起。
  - 目标世界必须是本 tenant 已登记、状态为 active 的 `sim:` 世界。real、test、shadow 世界一律拒绝。
- **两种生成方式**：
  - `synthetic`：根据参数（consumers 数量、`events_per_consumer` 的 min/max、`segments` 权重）生成 Consumer、对象和会话事件。
  - `structural`：只读取两类结构信息：
    - tenant 已发布的 `ontology.object_type_versions` 中每个类型的最新定义（取属性名和 `value_type`）；
    - real 世界里每个类型对象的 `count(*)`，按 `max_objects` 缩放后使用。

    属性值只按 `value_type` 抽样生成，从不复制 real 的值、标识或消息。
  - 脱敏快照不在本批次，归 NX-042 D2。
- **输出位置**：
  - `runtime.nexloop_sim_objects`（`origin='synthetic'`）；
  - `runtime.nexloop_sim_session_events`：只记录事件种类、偏移秒数和长度，不含文本；
  - `control.nexloop_consumer_personas`：persona 为 `synthetic`。

  三张表的 world 都必须是 simulation 类型，由 CHECK 约束保证。以下表都不写入：`ontology.objects`、会话、消息、Claim。因此抽取链路、工作流和事实路径都看不到生成数据。
- **可复现**：
  - 所有值都由 `sha256(seed|label)` 决定。
  - `control.nexloop_sim_generations` 记录以下内容：
    - generator 版本 `nexloop-sim-generator/2`
    - seed
    - 参数及其 digest
    - definitions digest
    - 计数
    - 输出 digest（与世界无关）
  - 同一 request 重放时返回原结果；参数不同则报 `request_reused`。
  - 同一世界里，同一 kind 和 seed 只能生成一次，否则报 `seed_used`。
- **删除**：上述表都有 world 列，S2 的整体删除会一并清掉。子表引用 generation 时用了 `on delete cascade`。

## 测试

`tests/test_nx041_sim_generation_pg.py` 覆盖以下内容：

- 同一 seed 跨世界生成，输出 digest 一致；不同 seed 则不同。
- 输出都带合成标记，并且不进入 Claim、`ontology.objects` 和消息表。
- 拒绝 real、test、shadow、未登记的世界、其他 tenant 的世界，以及正在删除的世界。
- 结构生成不含 real 的内容和 id，并且 real 数据前后一致。
- 整体删除后零残留，real 和另一个 sim 世界不受影响。
- Action 只授予 owner，Python 侧的校验与 SQL 一致。

## 审查修复（L6 审查 `nx041-s3-review.md`）

0261 尚未发布，以下修改直接在 0261 中进行。generator 版本升为 `nexloop-sim-generator/2`，因为 /1 从未发布过，所以不做兼容。

| 编号 | 修复 |
| --- | --- |
| 3.3 | 所有 id 和抽样标签改用「seed + kind」，同一个 world 中 synthetic 和 structural 可以使用同一个 seed。补了测试。 |
| 5.3 | structural 改用最大余数法分配：总数不超过 `max_objects`，Consumer 也计入总数。补了边界测试：7 个类型、上限 10，总数正好是 10。 |
| 1.2 | 真实计数为 1 到 4 时一律按 5 处理，所以小租户的确切小数目不会被复制。**限制：structural 仍会暴露每种类型的数量级**，比例关系和计数 ≥5 的类型的规模都看得出来。owner 在发放 sim 凭据之前应当知道这一点。 |
| 1.3 | 只复制满足以下条件的已发布属性：`visibility='normal'`（NX-040 中被 `hide` 的不复制），并且不属于该类型受限的属性组（`property_group_restriction` 事实）。受限属性的名字也不会进入 sim world。补了测试。 |
| 3.2 | 生成前对每个 world 加 `pg_advisory_xact_lock`，同一个 world 的生成串行执行。并发的同一请求会等前一个完成后重放结果，不再出现 23505。补了两个连接交错执行的测试。 |
| 2.3 | 先检查 world 状态，再处理重放：deleting 或 deleted 的 world 一律返回 `world_not_active`。测试只接受这一种结果。 |
| 4.3 | 生成表上的外键去掉了 `on delete cascade`。S2 的 purge 分别删除 runtime 中的两张子表和 control 中的记录，各自受 limit 约束，不会因为级联删除而越过 limit。 |
| 5.1 | Consumer、persona 和事件改为集合式批量插入。上限组合的实测结果（开发机，负载约 8）：synthetic 2000 个 Consumer × 50 条事件，共约 10.2 万行，4.1 秒；structural 1 万个对象，0.4 秒。两者都远低于超时，所以不改成异步执行。 |
| 5.2 | 每个 world 最多生成 10 次，超出时返回 `quota_exceeded`，因此单个 world 最多约 104 万行。补了测试。租户级的 world 数量上限和 S2 审查 4.4 一起，留待 D2 的产品决定。 |
| 2.2 | `control.nexloop_consumer_personas` 加上了 `world_valid` 约束，属于 S1 遗留清单的一项。补了测试。 |
| 4.2 | 不需要改动。生成的 Consumer 不进入 `ontology.objects`，所以对这些 Consumer 调用 `consumer.set_persona` 会被拒绝，不会产生 persona_events 行。即使将来有别的路径写入，persona_events 的 append-only 触发器（0227）也认 erasure scope，S2 的 purge 可以删掉这些行。 |
| 1.1 / 2.1 / 3.1 / 4.1 | 审查结论为“无”，没有改动。 |
