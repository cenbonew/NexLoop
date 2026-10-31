# I35 负责人读不到对象属性值：诊断与修复方案

状态：负责人 2026-10-31 选方案 A，已实现：workbench-roles v52 的 `read_types`，迁移 workbench_read_types（临时编号 0387）。owner 读全部 20 个已发布类型（含运行时类型）；受限组最后排除；已擦除 consumer 不可读；operator、reviewer 不变；每次读取写审计。只读诊断脚本 `scripts/ops/i35_owner_property_read.sql`，PG 测试 `tests/test_i35_owner_property_read_pg.py`。

## 1. 诊断

### 1.1 工作台成员的 READ 从哪里来

工作台成员（负责人、approver、operator、reviewer）读对象**不靠 grants**。读取时，SQL 按 `workbench-member-v1` 派生（0123，0224 扩展），依据只有一条：`control.nexloop_workbench_configurations` 当前版本里这个主体的角色（`authz.nexloop_workbench_member` → `authz.nexloop_workbench_member_may_read`）。所以 authority_facts 里没有任何人类主体持有 `eios:object_type:*` 或 `eios:property:*`，这是设计如此，不是 grant 丢了。nx-owner2、approver2、approver3 对应的主体，只要角色是 owner，就走同一条路径。脚本第 1、2 节列出当前角色和角色历史。

`property_access_rule` 的 3 条全部给服务主体：claim_matcher → Consumer（十个 ADR-019 组）、commitment_keeper → Commitment（commitment_lifecycle）、commercial_recorder → CommercialRecord（commercial_state）。这三条与工作台无关，也不能给人类主体：`service_grants` 只编译服务主体。

### 1.2 为什么负责人读不到 Consumer 的属性

0123 定的规则是：owner 和 operator 能读 Consumer 对象；**属性只有落在某个属性组里，且这个组没被负责人列入受限组时，才派生 READ**。原文是“a property outside every group … is never derived”。

stage 发布的 Consumer v1（`consumer_profile.consumer_profile_schema`）有 6 个画像属性：display_name、locale、timezone、lifecycle_status、external_id_refs、contact_preferences。v1 **没有声明任何属性组**，所以这 6 个属性对任何角色都不派生，工作台显示“! 受限”，「客户」列表显示“名称受限”。

前端把这种情况归为 `forbidden`，是对的：受限组是空的，不是 `withheld`。只有经人工审核发布的新属性才带 ADR-019 的组（`review_actions` 加组），这些属性 owner 能读。

### 1.3 是从来没授予，还是后来丢的

**从来没有授予。** 0123（NX-028）起就是这条规则，之后的版本都没改过它：0217 只改了 `present` 的显示，0224 增加了 Goal、PlanStep、Commitment、ServiceOffering 四个类型的固定字段清单。0217 的注释写“owner-restricted property (e.g. display_name)”，说明当时以为 display_name 会落在某个组里；而 Consumer v1 的画像属性根本没有组，这一点一直没被发现。

这跟 roles 或 manifest 的版本变化无关。脚本第 2 节（角色历史）和第 3 节（规则 fact）可以在 DATA_HOST 上核对。

## 2. 按类型盘点（owner 角色，依据代码；stage 实值以脚本第 5 节为准）

| 类型 | 对象可读 | 属性可读 / 总数 | 读不到的属性 | 规则来源 |
|---|---|---|---|---|
| Message | 是 | 5 / 5 | — | 0123 |
| Consumer | 是 | 0 / 6（v1）；审核发布的组内属性可读 | display_name, locale, timezone, lifecycle_status, external_id_refs, contact_preferences | 0123（只认组内属性） |
| Goal | 是 | 3 / 3 | — | 0224 清单 |
| PlanStep | 是 | 5 / 6 | submitter_principals | 0224 清单 |
| ServiceOffering | 是 | 12 / 12 | — | 0224 清单 |
| Commitment | 是 | 9 / 13 | made_by, content_ref, condition, fulfillment_evidence | 0224 清单 |
| CommercialRecord | 否 | 0 / 18 | 全部 | 无规则 |
| ConsumerOwnership | 否 | 0 / 2 | 全部 | 无规则 |
| ConsumerServiceOffering | 否 | 0 / 5 | 全部 | 无规则 |
| ConsumerIncentiveOffering | 否 | 0 / 5 | 全部 | 无规则 |
| IncentiveOffering | 否 | 0 / 9 | 全部 | 无规则 |
| Conversation | 否 | 0 / 2 | 全部 | 无规则 |
| EffectControl | 否 | 0 / 6 | 全部 | 无规则 |
| MessageAssignment | 否 | 0 / 15 | 全部 | 无规则 |
| ConsumerRoleLink | 否 | 0 / 6 | 全部 | 无规则 |
| RoleDefinition | 否 | 0 / 6 | 全部 | 无规则 |
| RoleAssignmentScope | 否 | 0 / 7 | 全部 | 无规则 |
| RoleExecutionCeiling | 否 | 0 / 9 | 全部 | 无规则 |
| ContextStrategy | 否 | 0 / 3 | 全部 | 无规则 |
| ContextManifest | 否 | 0 / 3 | 全部 | 无规则 |

脚本第 5 节在每个类型的一个 real 对象上，逐个属性调用 `authz.nexloop_workbench_member_may_read(...,'owner',...)`，结果就是工作台的实际表现。

## 3. 修复方案（建议）

原则：
- 不绕过 READ，仍由 SQL 在每次读取时按角色派生。
- 不给人类主体发 grants。`property_access_rule` 只编译服务主体，不改作它用。
- 范围变化由受治理的清单发布。

**建议 A（推荐）：workbench-roles 清单增加“角色可读类型”一节，SQL 按清单派生。**

1. `deploy/authorization/workbench-roles.v1.json` 新增 `read_types`，按角色给出类型和读范围（`all_properties` 或字段清单）。由配置员窗口 `workbench_roles --apply` 发布，写入工作台配置（受治理、带版本、可回退）。
2. 新迁移重写 `authz.nexloop_workbench_member_may_read`：
   - 先按现有规则判断；
   - 现有规则不允许时，再查当前配置里该角色的 `read_types`；
   - 受限组仍由 `property_group_restriction` 排除，这一步在最后，任何清单都不能覆盖；
   - 已擦除的 consumer 照旧不可读（0383）。
3. owner 的建议范围：全部 20 个已发布类型的对象和属性值。属性不在任何组里时（如 Consumer v1 画像），对 owner 视为可读。
4. operator 和 reviewer 保持现状，另行决定。

**备选 B：发布 Consumer v2，给画像属性加一个组（例如 `profile`）。** 这只修好 Consumer。新组会被 claim_matcher 的规则（`include_review_published`）连带影响，其他类型也没解决；对象版本迁移还会动到业务对象。不推荐。

## 4. 需要负责人决定

1. owner 的范围是“全部已发布类型的全部属性值”，还是逐类型列出？例如 ContextManifest、MessageAssignment 这类运行时类型，要不要也对 owner 开放？
2. 组外属性（Consumer v1 画像）对 owner 是否视为可读？受限组仍可排除，但组外属性无法单独限制。如需单独限制，就要另设一个“画像”组。
3. operator 是否也读 Consumer 画像（display_name 等）？目前 operator 与 owner 读的范围相同，这次会出现分歧。
4. reviewer 维持“只读待审定义所依赖的证据 Message”，是否不变？
5. 新增的读取同样每个对象写一条审计（`runtime.nexloop_workbench_read_audit`），确认可以接受。

## 5. 实现与部署单片段（方案 A，负责人 2026-10-31）

**实现**

- `deploy/authorization/workbench-roles.v1.json` v52 新增 `read_types`：
  - owner 逐个列出 20 个类型，不用通配，按字母排序；
  - operator 和 reviewer 为空列表；
  - Action 不变，owner 仍是 97 个。
- 迁移 workbench_read_types（临时编号 0387，依赖 0123、0224、0226、0357、0383）：
  - 新表 `control.nexloop_workbench_read_types`（按角色版本和角色记录，只追加），由配置员函数 `control.nexloop_configure_workbench_read_types` 写入。版本必须等于当前工作台配置的 roles_version；重放同样的清单不写，不同的清单拒绝。
  - `authz.nexloop_workbench_member_may_read` 改名后包一层新逻辑：
    - 先查角色：不是三种工作台角色之一就拒绝；再查已擦除：对象的任一 consumer 正在擦除或已擦除，任何派生方式都拒绝（见 I35-a）；
    - 再走原来的派生，结果不变；
    - 原来的派生拒绝时，查角色当前的 `read_types`：类型在清单里、且对象所在的类型版本已发布（object_type_versions 中有定义），就可读对象和该版本声明的属性；
    - 最后不论来自哪条派生，属于负责人受限组（property_group_restriction）的属性一律不可读。
  - 审计读取（`authz.nexloop_assert_workbench_member_read`）接受任何类型：新可读类型的 object_kind 为 `object`，原有六种不变；审计表的两个约束相应放宽。
- `workbench_roles --apply`：在 `control.nexloop_configure_workbench` 之后、同一事务里写入可读类型，输出多一项 `read_types_recorded`。

**I35-a 擦除：每个类型关联客户的字段**

新包装函数在所有派生之前做擦除判断，原有的 0123、0224 派生也包括在内。对象关联的客户，任一个正在擦除或已擦除（逐个问 `runtime.nexloop_consumer_erasing`；0383 起假名也算已擦除），任何角色都读不到这个对象和它的属性。关联字段由共享解析 `runtime.nexloop_object_consumers_read` 给出（0315/0357，与 assert 守卫、object_fields 同一套；它也跟随 relation 关联），再补上 RoleExecutionCeiling 的数组字段。assert 路径直接调用 may_read，所以这个判断不依赖调用方。

| 类型 | 关联客户的字段 | 由谁解析 |
|---|---|---|
| Consumer | 对象 id 本身 | 包装函数直接判断，共享解析也覆盖 |
| Conversation | `runtime.nexloop_conversations.consumer_id`（对象 id 即 conversation_id） | 共享解析 |
| Message | 所属 Conversation 的 consumer_id | 共享解析 |
| Commitment | made_to | 共享解析 |
| CommercialRecord | consumer_ref | 共享解析 |
| Goal、PlanStep、EffectControl、MessageAssignment、ConsumerOwnership、ConsumerServiceOffering、ConsumerIncentiveOffering、ConsumerRoleLink、RoleAssignmentScope | consumer_id | 共享解析（“其他类型的 consumer_id”） |
| RoleExecutionCeiling | consumer_ids（数组） | 包装函数单独判断（共享解析不跟随数组） |
| ServiceOffering、IncentiveOffering、RoleDefinition、ContextStrategy | 无客户关联（目录或定义） | — |
| ContextManifest | 无客户关联（run_id、call_sequence、request_digest，只记 Run 的上下文清单） | — |

另外，任何类型只要有 relation 指向正在擦除的客户，共享解析也会把它列为关联。

**I35-b 断言路径**：三处一起扩展，覆盖任何类型名，也就包括这 20 个：
- `authz.nexloop_assert_workbench_member_read` 中 v_object 的正则；
- 审计 object_kind 的 case：原有六种不变，其余类型记为 `object`；
- 审计表的 object_kind 与 target_resource 两个 check 约束。

copy guard 登记了这一处差异。PG 测试按真实路径走：owner 经工作台读取 Conversation，写入一行 `object` 审计；operator 被拒绝。

**I35-c 边界**（测试中有断言）：
- 清单只在原有派生拒绝时补充，原有派生允许的结果不变。reviewer 对 Message 的规则仍是“只读待审定义所依赖的证据”：没有待审依赖时，reviewer 读不到 Message。
- 角色为空或不在三种角色之内时一律拒绝，例如 customer、大小写不同的写法。customer principal 本来就不是工作台成员，assert 路径会先拒绝；包装函数在入口再挡一次。0123 曾让空角色读到 Consumer 对象，这次一并封住。
- 受限组仍在最后排除，不论来自哪条派生，测试也覆盖了这一条。

**I35-d operator 和 reviewer**：
- 清单只写 owner；operator 与 reviewer 是空清单，不继承 owner 的清单。
- operator 读到的范围与之前完全相同：Consumer 画像仍不可读，新增的 14 个类型仍是 readable=false。
- 前端已有这种显示：object_fields 返回 readable=false 时显示“受限”，读取值时 403 也按受限处理，与改动前相同，不需要前端改动。

**20 个类型（清单中逐项列出，按字母排序）**：Commitment、CommercialRecord、Consumer、ConsumerIncentiveOffering、ConsumerOwnership、ConsumerRoleLink、ConsumerServiceOffering、ContextManifest、ContextStrategy、Conversation、EffectControl、Goal、IncentiveOffering、Message、MessageAssignment、PlanStep、RoleAssignmentScope、RoleDefinition、RoleExecutionCeiling、ServiceOffering。

**I35-e 清单与 roles 版本绑定**

- 可读类型按 roles 版本记录。以后 roles 升版时，如果只写了配置、没写清单，owner 会无声地退回 0123/0224 的范围，必须能被发现。
- `workbench_roles --apply` 在同一事务里依次写配置和清单。写完后调用 `control.nexloop_workbench_read_types_state` 自检：当前 roles 版本必须有三个角色的清单，缺任何一个就报 `read_types_missing: roles version N has no read lists`，整个事务回滚，配置也不会留下。
- 指标快照（real）新增 `workbench_read_types`，内容为 `{configured, roles_version, present, missing}`。
- I35-g：告警评估器只比较数字，布尔 `present=false` 永远不会触发，所以另给数字 `missing`：有工作台配置且当前 roles 版本缺清单时为 1，否则为 0。没有工作台配置的租户报 `{configured: false, missing: 0}`，是看得见的 0，不是缺失。告警规则 v27 的 `workbench_read_types_missing` 读这个键。
- `nexloop-doctor --workbench-read-types --metrics-database-url-file F` 走只读的 metrics 角色：任何有工作台配置的 real 租户，只要 `present` 为 false 就退出码 1。
- PG 测试覆盖两种情况：
  - 跳过清单写入时，`--apply` 报错且没有任何提交；
  - 直接写入一个缺清单的配置时，doctor 检查失败，owner 退回原有派生。

**部署单片段（DATA_HOST，配置员窗口；迁移照常先备份）**

1. 迁移到 workbench_read_types（合批定号后的编号）；`check_definer_search_path` 预期 0 problems。
2. `workbench_roles --apply`（命令同 R54 S5），roles v52。预期：
   - `ok`；
   - owner 授权数仍为 **97**，operator 12、reviewer 1，没有 Action 增减；
   - 输出 `read_types_recorded: 3`（三个角色各一行，operator 和 reviewer 为空清单）；
   - 重跑为 `0`。
3. `nexloop-doctor --workbench-read-types --metrics-database-url-file <metrics DSN 文件>`：预期 `workbench_read_types_present: true`，该租户 `roles_version: 52`。之后每次 roles 升版都跑一次。
4. 只读核对（能执行 owner-only 函数的只读窗口）：

   ```sql
   select control.nexloop_workbench_role_read_types('<TENANT_ID>','owner');   -- 20 个类型
   select control.nexloop_workbench_role_read_types('<TENANT_ID>','operator'); -- {}
   ```

   再跑 `scripts/ops/i35_owner_property_read.sql` 第 5 节，预期：
   - 每个有 real 对象的类型 `object_readable = true`，`readable = properties`；
   - 例外是负责人受限组里的属性（当前三个类型的受限组都为空，所以没有例外）；
   - 已擦除 consumer 的对象不出现在可读结果里。
5. 工作台：负责人登录后，「客户」列表显示名称，「企业世界模型 › Object」里 Consumer 的 6 个画像属性显示值。operator 登录后 Consumer 画像仍为受限（行为不变）。

**回退**：roles 版本不能后退，所以要回退就发布 v53：`read_types` 的 owner 改为空清单，其余照 v52。各角色回到 0123、0224 的派生。迁移本身不回退。
