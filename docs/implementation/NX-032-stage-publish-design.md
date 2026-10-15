# NX-032 A1/A2 设计：新租户的系统对象类型与 Action 发布（stage_secrets publish）

状态：设计稿（2026-10-15），未实现。来源：run-41 §3 的缺口 A1–A3。所有出处都写到文件和函数；“未确认”表示没有核实。

- `nx/` 指 `packages/eios-core/src/nexloop_eios/`
- `mig/` 指 `packages/eios-core/src/eios/migrations/`

## 0. 问题与约束

- **问题**：生产代码读取约 38 个 Action 和 3 个 Function，它们建立在约 28 个对象类型之上。仓库里没有任何生产工具发布它们：生产调用 0050 时全部传空列表；`business-actions.v1.json` 只声明了 32 个业务 Action；其余都是测试夹具以管理员直接插表（run-41 §3）。
- **唯一合规的写入路径**是 0050 `control.nexloop_configure_manifest`（最新版本在 `mig/0162`），由配置者执行。它的语义（0162 第 50–90 行）是：
  - 对象类型只能发布 v1；同名同版本的内容必须完全相同，否则报 40001；
  - Action 与 Function 用 `on conflict do nothing`，然后要求库中的 `definition` 与 `capability` **逐字节相等**，否则报 40001；
  - Action 的能力快照必须 `has_side_effects = true`；
  - 定义里的 `capability_binding`（名称、版本、`schema_hash`）必须与快照一致。
- **推论**：
  1. 编译结果必须**确定**。`created_at`、`created_by`、所有 digest 都要由清单决定，不能取当前时间；否则重复发布会撞 40001，无法幂等。
  2. 每个只读 Action（例如 `nexloop.conversation.read`）的快照也必须声明有副作用，这与测试夹具一致：夹具统一 `has_side_effects = True`。
  3. 0050 只给 world `real` 写定义。

## 1. 新租户必须有的清单（按使用方分组，全部 `:1`）

“出处”列是目前唯一的定义来源，几乎都是测试夹具。“直插”指夹具直接写 `control.nexloop_action_definitions`，“0050”指夹具走 0050 清单。

### 1.1 对象类型

| 类型 | 生产里的 schema 构造函数 | 是否已在声明文件中 |
|---|---|---|
| Consumer | `nx/consumer_profile.py`（约第 10 行）、`consumer_resolution.py`（约第 52 行），**两处需要统一**；夹具用的是裸定义 | 否 |
| Goal、PlanStep、EffectControl | `nx/effect_contexts.py:effect_plan_schemas` | 否 |
| Conversation、Message、ConsumerOwnership | `nx/conversation_messages.py:conversation_schemas` | 否 |
| MessageAssignment | `nx/message_relay.py:message_assignment_schema` | 否 |
| ServiceOffering、ConsumerServiceOffering | `nx/service_offerings.py:offering_schemas` | 否 |
| IncentiveOffering、ConsumerIncentiveOffering | `nx/incentives.py:offering_schemas` | 否 |
| RoleDefinition、ConsumerRoleLink | `nx/role_mapping.py`（约第 36 行） | 否 |
| RoleExecutionCeiling、RoleAssignmentScope | `nx/role_policies.py`（约第 42 行） | 否 |
| ContextStrategy、ContextManifest | `context_engine/strategy.py`、`audit.py` | `deploy/ontology/system-object-types.v1.json` |
| Commitment、CommercialRecord | — | `deploy/ontology/business-object-types.v1.json` |

### 1.2 Action 与 Function

**治理要求的统一缩写**：
- “无治理”= 风险 low，approval `none`，`policy_refs` 为空，没有 preconditions 和 parameters；
- 读取方会核对这些条件：`message_relay.py` 第 128–129 行、`effect_execution.py` 第 154–161 行、`effect_contexts.py` 第 100–111 行、`context_artifacts.py` 第 108–112 行、`conversation_messages.py` 第 99–101 行。

| 组 | Action / Function（对象类型） | 能力名 | 治理与其他要求 | 现有出处 |
|---|---|---|---|---|
| relay | `nexloop.conversation.route`（Conversation） | 同 Action 名 | 无治理 | `tests/message_runtime_fixture.py:message_plan`（直插）；`tests/local_message_assembly_fixture.py:assembled_message`（0050） |
| relay | `nexloop.service.request`（Consumer） | 同名 | 无治理；`target_systems=['service']`，输入 `{message}` | 同上 |
| relay | `Goal.create`、`PlanStep.create`（Goal、PlanStep） | `ontology.object.create` | 无治理 | 同上 |
| relay | `MessageAssignment.create` | `ontology.object.create` | 由生产函数 `nx/message_assignment_contract.py:message_assignment_action` 构造 | `tests/test_message_relay.py:relay_plan`（直插） |
| relay | `nexloop.plan.bind_effect_context`（Consumer、Goal、PlanStep、EffectControl） | 同名 | 无治理；`registrar_schema('bind')` | `message_plan` |
| relay | `nexloop.context.bind`（同上） | 同名 | 无治理；`context_binding_schema()` | `message_plan` |
| relay | `NexLoop.queue.operations`（Consumer） | 同名 | 作为 Action 发布 | `assembled_message`（0050） |
| relay | Function `nexloop.conversation.service_receipt` | `nexloop.conversation.receipts.read` | 无副作用；scope `function.execute` | `tests/test_conversation_effect_receipts.py:receipt_plan` |
| conversation / webchat | `Conversation.create`、`Message.create`、`ConsumerOwnership.create`；`nexloop.conversation.read` | create 系列用 `ontology.object.create`；read 用同名 | 无治理，无 parameters | `message_plan` 及相关夹具 |
| effect / action-worker / webchat-delivery | `EffectControl.create`；`nexloop.effect.configure_control`（`registrar_schema('configure')`）；`nexloop.service.query`；`nexloop.service.receipt_reconcile`（`target_systems=['service']`，key `['intent_id']`）；`Goal.edit`、`EffectControl.edit`；`nexloop.enterprise.deliver`（`DELIVER_SCHEMA`） | create/edit 用 `ontology.object.*`；其余同名 | 无治理；`effect_execution` 要求定义与快照逐字节等于受理时的元数据 | `tests/runtime_effect_fixture.py:runtime_effect_plan`、`tests/effect_execution_fixture.py:execution_plan`、`tests/test_enterprise_slot_relay_pg.py:publish_enterprise_action` |
| business / catalog / enterprise setup | `Consumer.create`、`ServiceOffering.create`、`ConsumerServiceOffering.create`（加上上面的 EffectControl 与 configure_control） | `ontology.object.create` | 无治理 | `tests/test_business_setup_pg.py:business_plan`（0050）、`runtime_effect_fixture.install_runtime_catalog`、`tests/message_offering_fixture.py:definitions`（**这里用了 `consumer.create` 能力名，与其他夹具不一致**）、`tests/generic_offering_fixture.py` |
| outbound-recorder | `Message.agent_create` | `ontology.object.create` | 业务清单已有 | `tests/test_outbound_messages_pg.py`（`compile_actions`） |
| commitment-keeper | `Commitment.create/edit`；`Commitment.cancel/extend/attest/condition_met/mark_communication` | `ontology.object.*`；`commitment.*` | 业务清单已有；`commitment.*` 在 `control.nexloop_governed_capabilities`（`mig/0124`）中登记 | `tests/commitment_fixture.py:publish_commitment_type` |
| commercial-recorder | `CommercialRecord.create/edit/observe` | `ontology.object.*`；`commercial.observe` | 业务清单已有 | `tests/commercial_fixture.py:publish_commercial_type` |
| plan-reevaluator（Role-run） | `RoleDefinition/ConsumerRoleLink/RoleExecutionCeiling/RoleAssignmentScope.create`（scope `ontology.roles.manage`）；`ConsumerRoleLink.edit`（能力 `consumer.edit`）；`RoleExecutionCeiling.edit`、`RoleAssignmentScope.edit`；`nexloop.context.bind_role` | 见左列 | 无治理 | `tests/role_run_fixture.py:role_runtime_plan` |
| 人发起的受治理入口 | `nexloop.plan.request_reevaluation`、`service.query_request`、`conversation.takeover/handback`、`message.staff_send`、`commitment.bind_commercial`、`cost.record`、`consumer.erase/export`、`message.erase`、`retention.hold/release_hold`、`incentive.issue`、`restore.open_scope`、`real_dispatch.set`、`contact_key.revoke`、`alert.silence` | 同名，**必须**在 `governed_capabilities` 中登记（0124–0154，已由迁移写入） | 业务清单已有（`contact_key.revoke` 未确认） | `commitment_fixture.publish_request_actions`、`commercial_fixture.publish_registry_actions` |
| 工作台目标 | `Metric.approve`、`Goal.publish`、`Goal.propose`、`Control.set`、`Budget.set`、`Contact.release` | `goals.*`（已在 0124 登记） | 夹具把它们挂在 Consumer 上；**不在 PROFILES 中，现在无法从清单编译** | `tests/goal_fixture.py:register_goal_actions`、`tests/load_chain.py:publish_workbench_actions` |
| context | `nexloop.context.strategy.publish`（ContextStrategy）、`nexloop.context.audit` | 同名 | 业务清单已有 | `tests/load_chain.py:load_plans` |
| claim | `Consumer.edit`（`ontology.object.edit`）；匹配用到的 create/edit 视 `claim-match.json` 而定 | — | 业务清单已有 `Consumer.edit` | `tests/test_claim_matching_pg.py:publish_action` |
| recall | 不读 Action（`recall_index_worker.py`） | — | 只需要已发布的类型 | — |

以下资源**只是授权 id，不需要定义**：`NexLoop.feed.*`（0093、0126）、`NexLoop.queue.<q>`（0035），以及 `nexloop.reply.guarantee`、`plan.reevaluate`、`commitment.keep/read`、`commercial.record/read`、`cost.read`、`takeover.expire`、`relay.enroll/withdraw`、`alert.evaluate` 等。

例外：放进 Run 的 `allowed_resources` 的资源会被 `runtime_activation.py` 第 108 行读取定义，所以必须发布。目前已知的是 `NexLoop.queue.operations`，其余要逐个核对。

## 2. 声明文件与能力快照（A2）

### 2.1 `deploy/configuration/system-actions.v1.json`（新增，schema `nexloop-system-actions/1`）

不扩展 `business-actions.v1.json` 的格式：它的 `validate` 只接受 `target_systems=['postgres']` 与 key `['request_id']`，表达不了 `service.request` 或 `receipt_reconcile`。

很多输入 schema 由生产代码生成（`registrar_schema`、`context_binding_schema`、`DELIVER_SCHEMA`），所以声明文件**引用生产构造器**，不复制 JSON：

```
{"schema_version": "nexloop-system-actions/1", "manifest_version": 1, "valid_from": "<固定 ISO 时间，作为所有定义的 created_at>",
 "created_by": "nexloop-system-catalog",
 "object_types": [{"type_name": "Goal", "version": 1, "builder": "nexloop_eios.effect_contexts:effect_plan_schemas"}, ...],
 "actions": [{"stable_name": "nexloop.conversation.route", "version": 1, "object_types": [["Conversation", 1]],
              "capability": ["nexloop.conversation.route", "1.0.0"], "target_systems": ["service"], "idempotency_key_fields": ["request_id"],
              "input": {"builder": "<模块:函数>"} | null, "consumers": ["message-relay"]}, ...],
 "functions": [{"stable_name": "nexloop.conversation.service_receipt", "version": 1, "capability": ["nexloop.conversation.receipts.read", "1.0.0"], ...}]}
```

- `builder` 只允许指向白名单模块中已有的生产函数，统一做成 `nexloop_eios.system_catalog.BUILDERS` 一张表，不做任意导入。
- 夹具里的定义逐项迁成生产构造函数（新模块 `nx/system_catalog.py`），以 §1 的出处为准：
  - 同一 Action 在不同夹具中有差异时（例如 `consumer.create` 与 `ontology.object.create`），**以生产读取方的核对为准**并记录取舍；
  - Consumer 的两个 schema 必须先统一。
- 业务清单保持不变。发布时两份清单合并，按 `(stable_name, version)` 去重，重复就拒绝。
- 工作台目标的六个 Action 进系统清单，并决定挂在哪个类型上（夹具挂在 Consumer 上，**需要设计确认**）。

### 2.2 `deploy/configuration/capabilities.v1.json`（新增，A2）

- **每项字段**：`{capability_name, capability_version, kind: "atomic", has_side_effects, idempotent: true, required_scopes, risk_level: "low", contract}`。
- **`schema_hash`** = `sha256(canonical_payload({"capability": name, "version": ver, "contract": contract}))`，由工具计算，不手写。`contract` 是能力的输入/输出说明，可以引用 §2.1 的同一个 builder。
- **版本化**：快照不可变。改动 contract 必须升 `capability_version`，因而产生新的 Action 版本（0050 不允许改已发布的定义）。清单本身有 `manifest_version`。
- **与库内的 `control.nexloop_governed_capabilities`（0124 起只追加）的关系**：受治理入口的能力名必须已在该表中，发布前由工具核对，不在表中就拒绝；该表本身仍由迁移维护。
- **0050 的硬约束**：Action 的快照必须 `has_side_effects = true`。只读 Action 也照此声明，在清单里注明原因。Function 不受这条限制。

## 3. 发布路径：`stage_secrets.py publish`

```
stage_secrets.py publish --out <树> --tenant <id> (--bootstrap-dsn-file F | --configurator-dsn-file F) --signing-key-id ID [--select <组>] [--dry-run]
```

1. **加载并校验**：`system-actions.v1.json`、`capabilities.v1.json`、`business-actions.v1.json`、两份对象类型清单。校验去重，能力名都在快照清单中，受治理能力都在 `governed_capabilities` 中。最后一项要读库，由配置者的只读 inventory 提供，见下面的“需要新增”。
2. **编译**：
   - 对象类型：`system_object_types.trusted_object_types`、`business_object_types.trusted_object_types`，加上 builder 生成的类型；
   - Action：业务 Action 用 `business_actions.compile_actions`，系统 Action 用新的 `system_catalog.compile_system_actions`。`created_at = valid_from`，`created_by` 取固定值，`tenant` 代入。
   - 编译结果只由清单和租户决定，同一输入重复编译得到逐字节相同的结果。
3. **差异计算**：用 `G.action_lineage(dsn, tenant)` 读取已发布的 Action（0071 系列已提供给配置者），只发送缺少的 Action。
   - 对象类型和 Function 没有配置者可读的 inventory，**需要新增**一个只读函数，例如 `control.nexloop_catalog_inventory(tenant)`，按 0071 的模式写，`session_user` 必须是配置者。这需要一条迁移（**会触发迁移闸门和备份校验**）。
   - 不加迁移的替代方案：每次都整份发送。0050 对相同内容是“存在且相等即通过”，结果正确，但每次都会把 `authority_revision` 加 1，并多一条发布记录。**建议先用替代方案上线，迁移另排。**
4. **发布**：组成 0050 清单，`expected_revision` 取 inventory 的当前值，`manifest_id = uuid5(tenant + 摘要 + revision)`，经 `configurator_window`（`--bootstrap-dsn-file`）调用 `apply_manifest_with_secrets(..., service_secrets={})`。
   - 分批：0050 每类上限 4096，一次就能发完；按 §1 的分组用 `--select` 可以只发一部分。
5. **幂等**：内容不变时，有 inventory 就什么都不发；没有 inventory 就发一次，结果相同。
6. **回滚**：0050 只追加，没有删除路径；定义一旦发布就不可变。回退手段：
   - 停用：0050 没有 `active=false` 的入口（**需要确认**，0162 只写 `active=true`），这是一个缺口；
   - 撤销调用方的授权：用 service_grants 的 `retired`，或从授权清单删掉后执行 register；
   - 发布新版本替代。
   - 首次部署出错时，最干净的做法仍是在演练库或合成租户上先验证。**正式 stage 发布前必须先在本地干净 PG 上跑完整测试，见 §5 T5。**

## 4. 发布之后

1. **effect_categories**：执行 `real_tenant_init.py configure --bootstrap-dsn-file …`（run-41 §4.2）。`effect_categories.apply` 只对已发布的 Action 生效，所以必须在 publish **之后**再执行一次。核对：输出里 `deferred` 只剩确实不需要的类别。
2. **`recall-types.json`（A4）**：由 publish 的输出生成，即已发布类型中需要召回的那部分，`{类型: null}`（`load_types` 允许 spec 为 null）。哪些类型需要召回要由 NX-019/NX-021 的设计确定（**未确认**）；最小可启动集可以先用 `{"Consumer": null}`。由 `stage_secrets.py publish --emit-config` 写出，属主 10001，权限 0600。
3. **`relay-run-profile.json`（A5）**：键是 `role_ref`、`context_manifest_ref`、`runtime_profile`、`budget`、`runtime_owner_epoch`、`queue`（`message_relay.validate_run_profile`）。
   - `role_ref` 与 `context_manifest_ref` 引用的是租户组装时创建的 RoleDefinition 与 ContextManifest **对象**，这些对象要用 Action 来建，所以只能在 publish、business_setup、catalog_setup、角色组装之后生成。
   - 生成工具建议放进 NX-037 tenant assembly（`enterprise_setup` 之后输出），不放在 publish 里。
4. **服务主体（A3）**：`business_setup` 的 owner/executor、`catalog_setup` 的 maintainer 主体与授权补进 `stage-principals.v1.json`，然后重跑 `register`。授权项按 §1 的 setup 组核对。

## 5. 工作量与拆分

| 子任务 | 内容 | 估计 | 适合谁 | 依赖 |
|---|---|---|---|---|
| T1 | 把夹具里的系统类型与 Action 构造迁成 `nx/system_catalog.py` 的生产构造器（§1 全表），统一 Consumer schema | 1.5 天 | L2 | — |
| T2 | `capabilities.v1.json` 与 `system-actions.v1.json` 两份声明文件，以及校验器（schema_hash 计算，与 governed_capabilities 核对） | 1 天 | L4（与 T1 对表） | T1 的名单 |
| T3 | `stage_secrets.py publish`（编译、差异、窗口 apply、dry-run、`--emit-config`）及 PG 测试：干净 PG → register → publish → 每个 worker 的首轮调用不再报 unavailable；重复执行幂等 | 1 天 | L2 | T1、T2 |
| T4 | 测试夹具改为读同一份清单（§6），先改 relay、effect、commitment 三组，其余逐步 | 1–2 天，可分批 | L3、L5 并行（按夹具文件分，互不重叠） | T1、T2 |
| T5 | stage 演练：本地干净 PG 走完整路径（bootstrap、register、browser、publish、configure、setup 三件），再写 run-41 的执行命令 | 0.5 天 | L5 | T3 |
| T6 | A3 服务主体、A5 run profile 生成器（tenant assembly） | 1 天 | L3 | T3 |
| T7（可选） | 配置者可读的 catalog inventory 迁移（§3 第 3 步） | 0.5 天，外加迁移窗口 | L4 | — |

关键路径是 T1、T2、T3、T5，约 3.5–4 人天；T4 与 T6 可以并行。今天之内只能完成 T1 的一部分，**周六试运行的业务流程依赖 T3 和 T5 完成**。

## 6. 风险

| 风险 | 防护 |
|---|---|
| 夹具定义与生产清单漂移（同一 Action 两处定义） | T4：夹具改为调用 `system_catalog` 与清单（`compile_system_actions(tenant=…)`），不再手写 JSON。加一个守护测试：扫描 `tests/` 中 `insert into control.nexloop_action_definitions` 的出现次数，只允许名单内的旧夹具，名单只减不增，用法同 AT-064 的白名单 |
| 编译不确定导致重复发布报 40001 | `created_at = valid_from`，`created_by` 取固定值；测试断言两次编译逐字节相同 |
| 已发布定义不可变，第一次发错难以收回 | §3 的 T5 演练，加上 `--dry-run` 输出 digest 供人工核对；0050 没有停用入口，列为后续缺口 |
| 只读 Action 被迫声明 `has_side_effects=true`，与语义不符 | 清单注明；`effect_categories` 中这些 Action 归为 internal_read，不当作外部接触（run-41 G2） |
| 夹具之间的差异（例如 `consumer.create` 与 `ontology.object.create`）被错误统一 | T1 逐项以生产读取方的核对为准，差异写进 T1 的提交说明并让 L4 复核 |
| 工作台目标 Action 挂在哪个类型上未定 | 设计确认后再进清单；不阻塞 relay、effect、commitment |
| 迁移（T7）触发备份校验与迁移闸门 | 先用整份发送的替代方案，T7 另排窗口 |
