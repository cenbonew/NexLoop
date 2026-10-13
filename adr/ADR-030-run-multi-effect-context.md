# ADR-030｜Run 按 slot 绑定多个效应上下文

状态：**已批准**（Accepted，负责人 2026-10-13，无修改意见；记入 ROADMAP §7）。分支 `l2/nx037-enterprise-deliver`（NX-037 缺口 B，切片 2）。

关联：
- AGENTS.md：只有受治理的 EIOS Action 改变正式业务对象或产生外部效应；派发时按当前权限复查。
- ADR-023：联系限制在派发时检查；兜底回复 Run 只有一个回复工具。
- ADR-028：效应工具调用路径。
- 迁移 0400：部署级效应 Action 允许表 `control.nexloop_effect_actions`，每个 Action 一个计划 slot。
- `docs/implementation/NX-037-enterprise-deliver.md`。

## 1. 背景

试运行的 `real_delivery` 场景是：消费者在对话中提出需要真实交付的请求，Agent 在同一次对话中既要回复，又要发起企业交付（`nexloop.enterprise.deliver:1`）。

现在一个 Run 只有一个效应上下文：`control.nexloop_effect_run_contexts` 以 `run_id` 为主键，意图受理按 `run_id` 取这唯一的上下文。消息 Run 签发时也只为回复建一个计划步骤，并只授予回复 Action。

另一种做法是消息 Run 只回复，再由另一个 Run 执行交付。那要跨 Run 转写交付内容，还要多一次模型调用或固定转写，审计链更长，也会多出一种 Run。调度员 2026-10-13 选定本方案。

## 2. 决定

1. **键改为 `(run_id, slot)`。** 一个 Run 可以有多个效应上下文，每个 slot 至多一个。slot 取自允许表（每个 Action 对应一个 slot，例如 `service.request:primary`、`enterprise.deliver:primary`）。没有企业交付的 Run 只有回复这一行，与现在相同。
2. **意图按所声明 Action 的 slot 选上下文。** 意图受理取的是该 Run 在“所声明 Action 的 slot”上的上下文；声明的资源必须与该上下文的 Action 一致。Run 凭据必须包含该 Action。查询（find）同样按 slot 取上下文。
3. **各 slot 相互独立**：
   - 预算：每个上下文有自己的 EffectControl 和预算单位，两个 slot 互不占用；
   - 租约与派发：每个意图有自己的外发租约和 Action claim；
   - 联系限制：在派发时对每个意图分别检查。企业交付的类别是 `customer_contact`，所以客户拒收联系后它会被拒；回复仍按 ADR-023 第 6 条，只允许绑定本次来信的回复；
   - 目录：每个 slot 对应自己的 offering。回复使用导出 offering（context pack 的 `supply`），企业交付使用企业 offering（context pack 的 `effect_supplies` 中 `action` 为该 Action 的那一项）。offering 的 `delivery_action` 必须等于意图的 Action。
4. **只在需要时绑定第二个上下文。** 消息 Run 签发时，只有消费者有生效的企业 offering（一条有效的 ConsumerServiceOffering，且其 offering 的 `delivery_action` 是企业 Action），才为企业 slot 建计划步骤、绑定上下文，并把企业 Action 加入路由的 `allowed_actions` 和 Run 凭据。没有生效企业 offering 时，签发、路由、context pack 和授权路径都与现在逐字节一致，由测试钉住。
5. **锁的顺序是固定的。** 绑定第二个上下文与第一个在同一事务中进行，按 slot 的固定顺序加锁：先回复 slot，再企业 slot。各类对象的锁顺序沿用现有的计划绑定规则：先计划步骤，再目标，再控制，最后消费者。不引入新的跨 slot 交叉锁顺序。
6. **兜底回复 Run 不变。** 它仍然只有一个回复工具和一个回复上下文（ADR-023 第 7 条）；企业交付不会出现在兜底回复 Run 中。
7. **Agent 工具按凭据提供。** 只有 Run 凭据包含 `nexloop.enterprise.deliver:1` 时，Host 才提供 `nexloop.enterprise.deliver` 工具。工具参数与 Action 定义的 `input_schema` 一致：`summary`（必填，不超过 200 字）、`details`（可选，不超过 2000 字）、`consumer_ref`（NexLoop 的不透明客户引用）；不带 tenant_id，未知键会被拒绝。派发时仍复查当前权限和联系限制。

## 3. 后果

- **性能**：
  - 没有企业 offering 的消息 Run 走现有的单行路径；
  - 签发时只多一次读取，即判断消费者有没有生效的企业 offering，走 ConsumerServiceOffering 的已有索引；
  - 有企业 offering 时，签发同一事务内多建一个计划步骤、多绑定一个上下文；
  - 意图受理的查找从“按 `run_id` 取一行”变为“按 `(run_id, slot)` 取一行”，仍然走主键。
- **死锁**：新增的锁都按第 2 节第 5 条的固定顺序获取。两个 slot 的意图在受理和派发时各锁各自的上下文和外发行，不交叉持有对方的行。各 slot 的预算列在各自的 EffectControl 上，两个 slot 之间没有共享计数器。
- **契约**：context-pack-v6 有两处变化。
  - `current_constraints.action` 和 `supply.properties.delivery_action` 从 const 改为 enum：回复 Action 或企业 Action；
  - 新增可选字段 `effect_supplies`：数组，每项是“`action` 加上与 `supply` 相同的形状”；不得包含回复 Action，Action 不得重复，最多 4 项。
  
  `supply` 仍是回复用的导出 offering，现有生产者和消费者不受影响。
- **测试**：
  - 零影响：同样输入下，context pack 和 Run 凭据的规范化字节不变；
  - 多 slot：同一个 Run 既回复又交付；
  - 客户拒收联系后，企业交付在派发时被拒，回复按 ADR-023 处理；
  - 企业 offering 在签发后、派发前失效，交付在派发时被拒，回复不受影响；
  - 三层真实派发开关只作用于企业交付。
- **回滚**：执行一个新迁移，恢复单上下文的键并移除第二个 slot 的签发逻辑。已经存在的企业 slot 行在回滚前必须已结清（意图为终态），否则拒绝回滚。

## 4. 不做

- 不支持同一 Action 在一个 Run 中有多个上下文：一个 slot 最多一个。
- 不改兜底回复 Run 和本地 JSON 投递。
- 不在计划或角色 Run 中自动绑定企业 slot：切片 1 已支持它们以单 slot 的形式执行企业交付。
