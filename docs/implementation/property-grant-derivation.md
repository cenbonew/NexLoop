# 服务主体逐对象属性授权的受治理派生（设计稿，待审核）

状态：**设计稿，未实现**。分支 `property-grant-derivation`，BASE `66d2849`（dispatch/integration-s3k）。实现时的临时迁移从 0084 起。本稿涉及授权模型，需要调度员审核，必要时提交负责人。参照 L1 的 0077（Message READ 派生，`docs/implementation/NX-018-message-read-derivation.md`）。

## 1. 问题

- NX-044 approve 发布了新属性（如 `Consumer.payment_method`）之后，服务主体（`claim_matcher`）若要通过受治理编辑应用等待中的 Claim，需要以下授权：
  - `eios:object:Consumer/<oid>` 的 EDIT（以及读取当前值所需的 READ）；
  - `eios:property:Consumer/<oid>/payment_method` 的 READ 与 EDIT；
  - 召回门槛用的定义级 `eios:property:Consumer/payment_method` READ（NX-021）。

  这些都是新的、或按对象动态产生的 resource。
- **缺口其实更大**：NX-048 清单把匹配器对 `Consumer|Product/<id>` 的**全部**逐对象读写列为暂缓项（“依赖实例数据，需要受治理派生，不能用静态清单”）。也就是说，生产环境里匹配器连**已有**属性都没有逐对象授权来源，测试里的授权全靠夹具写入。新发布属性只是同一缺口最显眼的一例。
- **不可行的路线**：
  - 逐对象把授权事实物化写进 `authz.nexloop_authority_facts`：每写一条都会推进 authority epoch，令所有会话失效（0077 §1 已论证）。
  - 让 EIOS 通用授权按类型或父资源继承：`ApplicationFacts.resources` 按精确的 (tenant, type, resource_id) 匹配，application 事实与目标无关，无法按对象派生（0077 §9.1）。
  - NX-048 的 follow_latest_version 只适用于有版本号的 Action，不适用于按对象产生的 resource。

## 2. 方案：按类型声明的属性访问规则 + 每次使用时派生的类型化证明

与 0077 同构：可信配置只写一条**按类型**的规则，每次使用时在 SQL 内从当前受治理事实推导，生成单独的“派生证明”；不写任何逐对象授权，因此新对象或新属性出现时 authority epoch 不变。

### 2.1 两个新 fact kind（只能由可信配置写，`control.nexloop_configure_manifest` 校验其精确形状）

1. **`property_access_rule`**，key `[principal_id, type_name]`，每个服务主体每个类型一条：
   ```json
   {"tenant_id":…, "principal_id":…, "type_name":"Consumer",
    "operations":["read","edit"],                 // ⊆ {read, edit}；不含 create/delete/execute
    "property_groups":["preference","spending_power","purchase_behavior"],
    "include_review_published":true,              // 是否覆盖审核后新增到这些组的属性（见 §4.3）
    "basis_schema_version":1,                     // 规则签发时该类型的 Schema 版本
    "active":true, "valid_until":"…"}
   ```
   - 主体必须已是 `subject_kind='service'` 的 `subject_authority`，与 `message_read_rule` 的要求相同；人类主体与 agent 主体都不能有此规则。
   - 前提条件：该主体已有可信配置授予的 `eios:object_type:<type>` READ（NX-048 已为 matcher 授予 Consumer/Product）。没有就不派生。
2. **`property_group_restriction`**，key `[type_name]`，由负责人标记的受限组：
   ```json
   {"tenant_id":…, "type_name":"Consumer", "restricted_groups":["demographics","spending_power_sensitive"], "revision":…}
   ```
   - 它是独立的事实，任何规则都**不能**覆盖它：受限组里的属性一律不派生，即使规则把该组列入，也不例外。
   - 若需要访问受限组属性，只能走可信配置为具体 resource 显式写入的授权。
   - NX-048 的 `service_grants` 校验也会拒绝规则中列出受限组（静态防呆）。

**v1 清单的落地方式**：在 `service-grants` 清单中新增可选的 `property_access_rules` 段，由 `service_grants --apply` 编译为上述事实，与 `follow_latest_version` 一样走 0050 路径、留审计、幂等。受限组的标记由负责人提供，作为单独的清单段或单独的配置文件，调度员不能替负责人决定。

### 2.2 派生条件（每次使用时在 SQL 内重读，关键行加 `for share` 锁）

服务主体 P 对资源 R 执行操作 op 的派生证明成立，当且仅当**全部**满足以下条件：

1. **调用者**：P 是 real world 中、没有 run_context 的服务凭据，身份快照（tenant、principal、credential、directory_hash、world、到期时间）与证明一致。Run 凭据和浏览器人类会话一律不派生。
2. **资源形状**：R 只能是 `eios:object:<T>/<oid>`、`eios:property:<T>/<oid>/<p>` 或定义级的 `eios:property:<T>/<p>`；op ∈ {read, edit}。定义级资源只派生 read。
3. **规则**：`property_access_rule[P, T]` 存在、`active`、未过期，op 属于其 `operations`；证明中的规则 record hash 与当前一致；证明期限不超过规则期限。
4. **类型授权前提**：P 当前对 `eios:object_type:<T>` 持有已配置的 READ，通过原 `nexloop_assert_read_authority` 校验，与 0077 嵌套 Consumer READ 的做法相同。
5. **对象**：`ontology.objects` 中存在 (tenant, real, T, oid)。对象删除或不在 real world 都不派生。
6. **属性归属**：p 在该对象**当前** `schema_version` 对应的 `object_type_versions` 定义中声明，并且属于某个 `g ∈ rule.property_groups`。
7. **受限组排除**：g 不在 `property_group_restriction[T].restricted_groups` 中。证明带上受限组事实的 record hash（没有该事实时为空标记），使用时重新比对。
8. **审核新增属性**：如果 p 不在 `basis_schema_version` 的定义中（即在规则签发之后新增），还要求：
   - `include_review_published=true`；
   - 存在一条 `ontology.nexloop_review_decisions` 记录，outcome 为 `published`，其 `published_refs` 包含 `eios:property:<T>/<p>`，并且该决定中 p 的分组就是 g（来自候选的 property_group）。

   规则签发之后、不是经人类审核新增的属性（例如走其他 Schema 路径）不派生。
9. **已配置优先**：只要 P 对 R **自己**有已配置的 `grants` 事实（即使为空），就走已配置路径，派生被拒（`superseded`），与 0077 §9.3 相同。显式写入的空授权不会被派生补回。

派生出的证明只有 `{对象 READ/EDIT, 属性 READ/EDIT, 定义级属性 READ}` 三种形状，不产生 CREATE、DELETE 或 EXECUTE。Action 的 EXECUTE 仍按 NX-048 清单与 follow_latest_version 授予。

### 2.3 机制（与 0077 一致，不改已发布的迁移和 0081）

- **证明格式**：与现有 READ/EDIT 证明外形和 HMAC 相同（`nexloop-object-read-v1` 的 claims 形状），另带 `derivation='type-property-v1'`、`facts=[]`，以及 `derivation_basis`：规则 hash、受限组 hash、对象的 schema_version、属性所在组、审核决定 id（仅新增属性）、object_type READ 的嵌套证明。
- **SQL 包装**：
  - `authz.nexloop_assert_read_authority`（当前为 0077 版本）与 `authz.nexloop_assert_edit_authority` 各加一层外包装：带 `derivation='type-property-v1'` 的证明转交新的 `authz.nexloop_assert_derived_property_access`，其余原样交给前一版本。按现有约定用“改名为私有函数 + 新建公开包装”的方式追加。
  - 0081 的 create/edit 包装和 0054 的编辑函数体都**不改**，它们照常调用 `assert_edit_authority`。
- **路径选择**：`authz.nexloop_property_access_basis(digest, world, resources[])` 对每个资源返回 `configured` 或 `derived`，以及派生依据。它不授予任何权限，只帮助 Python 选择证明路径。
- **Python 侧**：
  - `object_reads.AuthorizedObjectReader._authority`、`object_edits.GovernedObjectEditor`、`recall.EiosRecallAuthorizer` 在 basis 返回 `derived` 时构造派生证明，否则仍走 EIOS 决策服务。
  - `EiosRecallAuthorizer` 目前只用 EIOS 决策服务，需要增加派生分支，否则召回的定义门槛与实例后置校验都看不到新属性。
  - 这三个文件都不在禁区内。

## 3. 撤权、删除与变更的传播

| 变化 | 传播 |
|---|---|
| 规则停用、过期或删去某个组（清单重发） | 规则 hash 变化 → 已签发证明在 SQL 尾检被拒；同时推进 epoch，旧会话失效 |
| 负责人把某组标为受限 | 受限组事实 hash 变化 → 该组所有派生立即失效；推进 epoch |
| 撤销 object_type READ | 嵌套证明失效 → 派生失效 |
| 对象删除或移出 real world | 对象行不存在 → 拒绝 |
| 对象升级到新 Schema 版本（NX-044 发布） | 按当前 schema_version 重新判定属性归属；已有属性的分组不会被发布改变（NX-044 门槛保证分组只增不改） |
| 审核决定被更正或撤回（目前没有此操作；将来若有） | 依据中的决定 id 失效 → 新增属性不再派生 |
| 显式写入空授权 | 已配置优先，派生被拒 |

## 4. 为什么不会扩权（证明要点）

设规则为 R = (P, T, Ops, Groups, include)，受限组为 X。派生授权集合满足：

> Derived ⊆ { (T/oid, op), (T/oid/p, op), (T/p, read) | op ∈ Ops, p ∈ props(T, 当前版本) 且 group(p) ∈ Groups \ X，若 p 为规则签发后新增则需 include 且有审核发布记录 }

1. **规则是唯一来源，且只能由可信配置写入**（`nexloop_configurator`、0050 审计、epoch 推进）。NX-044 发布在同一事务内比对授权事实指纹，保证发布**不能**修改规则或受限组；审核人也无法通过发布改变任何授权事实。
2. **发布只能“把新属性放进已有或新的组”**，不能改变已有属性的分组，也不能删除属性（NX-044 SQL 门槛加 EIOS `assert_object_compatible`）。因此对已有属性而言，派生集合在发布前后完全相同；只有新属性可能加入，而且前提是它被放进了规则**已经**覆盖、且不受限的组。主体能读写的属性“组”的集合从不增加。
3. **新属性的分组由有 `ontology.schema.review` 权限的人类在审核时确认**，并记录在审核决定中，可审计。模型提议的组只是候选内容，必须经人类 approve 才会生效。若负责人认为某一类信息敏感，应把对应组标为受限：受限标记优先于任何规则，且该标记本身只能由负责人通过可信配置设置。
4. **不越过类型**：派生要求 P 已经持有该类型的 object_type READ，资源限定为同一个 T 的对象和属性；规则不能覆盖其他类型，也不会产生 CREATE、DELETE 或 EXECUTE。
5. **不扩展到人类、agent 或 Run**：规则只能属于 service 主体；浏览器人类会话和 Run 凭据不走派生（条件 1）；审核权限、外发效应、REAL_DISPATCH 都不是属性资源，派生形状覆盖不到。
6. **不破坏显式拒绝**：已配置优先（条件 9）。
7. **租户与 world 隔离**：一切取自服务端身份快照，并在 RLS `eios.tenant_id` 下读取；只派生 real world。

**残余风险（需要负责人知悉）**：如果审核人把一个本应敏感的新属性放进了非受限的已授权组，服务主体会获得它的读写权限。缓解办法：
- 负责人维护受限组；
- `include_review_published` 可按规则关闭，关闭后新增属性一律需要显式授权；
- 审核工作台在 approve 前显示“该组会对哪些服务主体派生读写”（实现时增加提示）。

## 5. 待决定的点（请调度员或负责人确认）

1. 是否接受新增两个 fact kind（`property_access_rule`、`property_group_restriction`）。这改变授权模型，与 0077 的 `message_read_rule` 同级。
2. `include_review_published` 的默认值。建议**规则里必须显式写出**，清单没有默认值；初版 matcher 的规则建议写 true，以满足 AT-067 第二步。
3. 受限组初始名单由负责人提供。建议至少把 `demographics` 标为受限，`spending_power` 是否受限由负责人决定。九大类见 ADR-019 §2。
4. 对象范围：初版按“该类型在租户 real world 内的全部对象”派生，与 object_type READ 的语义一致。若需要更窄（例如仅限匹配器正在处理的 Claim 所属 Consumer），可以在条件 5 增加“对象是某个 awaiting/unresolved Claim 的 subject”，但这会把授权与 Claim 状态耦合，不建议在 v1 采用。
5. `EiosRecallAuthorizer` 增加派生分支会改变 NX-021 召回门槛的行为，即服务主体能看到由规则覆盖的属性定义；需要同意。

## 6. 实现计划（确认后执行）

1. 临时迁移 0084：两个 fact kind 的约束与 manifest 形状校验；`nexloop_assert_derived_property_access`；包装 `assert_read_authority` 与 `assert_edit_authority`；`nexloop_property_access_basis`。
2. Python：`property_access.py`（构造派生证明）；修改 `object_reads`、`object_edits`、`recall.EiosRecallAuthorizer`；`service_grants` 清单增加 `property_access_rules` 段与受限组校验。
3. 测试（真实 PG，失败均 fail closed、零写入）：
   - 正向：NX-044 approve 之后，**不手工逐对象授权**，matcher 回流把 Claim 应用到新属性（AT-067 第二步闭环）；已有属性同样通过派生应用（关闭 NX-048 的暂缓项）。
   - 负向：
     - 未声明规则；
     - 规则不含该操作或该组；
     - 受限组（即使规则列出）；
     - 规则签发后非审核新增的属性；`include_review_published=false`；
     - 他租户、他 world；对象已删除；
     - 人类、agent 或 Run 凭据请求派生；
     - 显式空授权优先；
     - 规则或受限组变更后，旧证明在 SQL 尾检被拒；
     - 新增对象或属性不推进 epoch（旧会话仍可用）。
4. 证据与报告，按五项格式。
