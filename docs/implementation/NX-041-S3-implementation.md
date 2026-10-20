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
    - generator 版本 `nexloop-sim-generator/1`
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
