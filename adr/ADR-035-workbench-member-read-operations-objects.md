# ADR-035｜工作台成员读取运营对象实例（补充 ADR-025）

状态：**草稿**，待调度员审核。负责人 2026-10-19 选择方案 A：扩展 ADR-025 的成员读取，让负责人和运营可以浏览运营对象实例；其中涉及个人信息的属性，仍按受限组处理。

关联：

- ADR-025（工作台成员读取消息原文与客户资料）；
- 企业世界模型设计稿 §5 W2（实例浏览与邻域，`docs/design/workbench-world-model-design.md`）；
- AT-003（无 READ 时字段不可见）；
- NX-029（删除传播）；
- ADR-023（联系限制）。

## 1. 背景

ADR-025 的派生函数 `authz.nexloop_workbench_member_may_read`（0123）只认 `Message` 和 `Consumer` 两种类型。W2 的实例浏览和邻域对其他类型逐个核对 READ 时一律读不到，只能显示“不可见 n 个”。负责人和运营因此看不到目标、计划步骤、承诺和服务项目这些运营对象，而这些正是理解“企业现在在做什么”的主体。

核对实际 object type 后，有以下几点事实：

- **没有 `KeyResult` 对象类型。** NX-022 的关键结果存在 `control.nexloop_key_results`，已经经由工作台 `goals` verb 返回（受 `nexloop.workbench.read` 治理），不需要本 ADR。
- 运营对象实际是四个类型：`Goal`、`PlanStep`（系统目录，`system_catalog.object_types`）、`Commitment`（业务目录，`business-object-types.v1.json`）、`ServiceOffering`（系统目录）。
- 系统目录的类型**没有声明属性组**。ADR-025 对 Consumer 用的规则是“属性必须属于某个组，且该组未被负责人标为受限”。按这条规则，系统类型的属性永远不会被派生。所以这四个类型改用**固定字段表**，再叠加负责人受限组的排除。

## 2. 决定

1. **类型与角色。**
   - 新增四个类型：`Goal`、`PlanStep`、`Commitment`、`ServiceOffering`。
   - 角色只开放 **owner** 和 **operator**。
   - **reviewer 不开放**：reviewer 的职责只是审核待审条目的证据（ADR-025 §2.2），浏览运营对象超出了这个职责。需要时另行决定。
2. **对象 READ 与属性 READ。**
   - 对象 READ：本租户、本 world 内存在的对象，可以派生对象级 READ，即能看到 ID、版本和时间，并能作为 W2 的邻居。
   - 属性 READ：只派生下表中“可读”的字段。表外的字段一律不派生。
   - 如果字段所在的属性组被负责人标为受限（`property_group_restriction`），即使在表中也不派生。

   | 类型 | 可读字段 | 不派生的字段及理由 |
   |---|---|---|
   | Goal | consumer_id、state、valid_until | 无 |
   | PlanStep | consumer_id、goal_id、control_id、action_name、state | submitter_principals：服务主体清单，属授权配置，不是运营信息 |
   | Commitment | made_to（即 consumer_id，与客户列表同级的假名 ID）、promised_at、due_at、due_precision、related_goal_ref、supersedes、status、late、fulfillment_basis | condition（客户原话中的条件）、content_ref（指向 Claim 和 Message 的片段）：属于对客户的内容，只经由承诺视图读取，即 `nexloop.commitment.read` 加来源 Message 的 READ（ADR-025 §2.2）；made_by（含员工或 Run 的标识）、fulfillment_evidence（证据引用）：由承诺视图以汇总形式给出 |
   | ServiceOffering | 全部 12 个字段 | 无（目录元数据，不含个人信息） |

3. **对客户的内容。**
   - Commitment 的承诺正文与条件，不经由对象浏览开放；仍只在承诺视图中、凭来源 Message 的 READ 显示。
   - `made_to` 和各类型的 `consumer_id` 只是假名 ID，与消费者列表同级。名称按 Consumer 的 `display_name` 规则读取（含受限组与 0217 的“受限时不暴露有无”）。
4. **删除传播（NX-029）。**
   - 对象所属的 Consumer（`consumer_id`；Commitment 用 `made_to`）正在擦除（`runtime.nexloop_consumer_erasing`）时，对象 READ 和属性 READ 都不派生。
   - 擦除完成后，对象本身按 NX-029 的规则清除或打标。派生在每次读取时重新判定，不留缓存。
5. **联系限制（ADR-023）。** 联系限制约束的是对外联系，不约束成员阅读，与 ADR-025 对 Consumer 的处理一致。客户受限，不改变这四类对象的可读性。
6. **审计。**
   - 每次派生读取都写一条读取审计（ADR-025 §2.3），一个对象每个事务一行。
   - 新增 `object_kind`：`goal`、`plan_step`、`commitment`、`service_offering`。
   - `read_purpose` 由调用方给出：W2 用 `world_model`，其他页面用各自的用途。
   - 审计写入失败时，本次读取一并失败。
7. **只走派生，不直接授予。**
   - 不写任何逐对象的授权事实，不改工作台角色清单（`workbench-roles.v1.json` 不升版本）：owner 和 operator 已经持有 `nexloop.workbench.read`，这次只是扩展该读取在 SQL 中的派生范围。
   - 实现方式沿用“改名保留 + 新包装”：原派生函数改名保留，新包装只对上述四个类型走新规则，其余类型原样转交。
   - 断言函数 `authz.nexloop_assert_workbench_member_read` 以 0123 的定义为底逐字复制，只放宽审计对象的类型匹配。

## 3. 后果与测试

- **影响面**：W2 的实例浏览和邻域可以显示这四类运营对象及其可读字段。Consumer 与 Message 的规则不变。
- **测试至少覆盖**：
  - owner 和 operator 能读四类对象及表中的字段；
  - 表外的字段读不到，例如 Commitment 的 condition 和 content_ref、PlanStep 的 submitter_principals；
  - 负责人受限组中的字段读不到；
  - reviewer、外部人员和客户读不到；
  - 跨租户读不到；
  - 所属 Consumer 正在擦除时读不到；
  - 每次读取都写审计行，`object_kind` 正确。
- **迁移**：一条，临时编号：放宽审计表的两个 check，派生函数改名后包装，并替换断言函数。

## 4. 不在本 ADR 范围

- reviewer 浏览运营对象；
- 其他类型，例如 EffectControl、MessageAssignment、Role 系列、CommercialRecord；
- 任何写入。
