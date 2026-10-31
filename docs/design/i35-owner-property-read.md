# I35 负责人读不到对象属性值：诊断与修复方案

状态：诊断完成、方案待负责人决定（2026-10-31）。只读诊断脚本 `scripts/ops/i35_owner_property_read.sql`（PG 测试 `tests/test_i35_owner_property_read_pg.py`）。尚未改授权。

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
