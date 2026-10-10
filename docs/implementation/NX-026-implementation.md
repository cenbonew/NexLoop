# NX-026 承诺与价值履行：实现说明

设计稿 `NX-026-design.md`（调度员已审，D1–D8 已裁定，D3/D6 负责人已定）。分支 `nx026-impl`，从 main `29035c8` 切出，合入了 main `f7e391d`（负责人对 Commitment 受限属性组的决定）。迁移号 0111–0113 为临时编号，合并时由调度员统一重编号。

## 1. 迁移

| 临时号 | 内容 | 依赖的已有对象 |
|---|---|---|
| 0111 `nx026_commitments` | 设置表（`commitments.v1.json` 逐字种入）、登记表、事件/证据/异常账本（append-only，FORCE RLS）、对象守卫、Claim / 更正 / Message 删除 / 联系限制触发器、effect intent 与外发投递的证据触发器、兜底回复 Run 拒绝承诺引用、两个 work feed、登记推导、keeper 端口、读取端口、人类 Action 函数；`authz.nexloop_work_feed` 与 `authz.nexloop_goal_governed_action` 以 0109 最新函数体 create or replace，只加新 feed / 新 capability | 0066 claims、0079 外发记录、0093 work feed、0106 `runtime.nexloop_plan_commitment_due`（T7）与 `nexloop_active_plans`、0068 控制面与 run goal bindings、0109 联系限制、0110 `nexloop_is_fallback_run`、0084 属性授权派生 |
| 0112 `nx026_contact_effect_categories` | D6：effect 类别旁表（只能由 `nexloop_configurator` 经 `control.nexloop_configure_effect_categories` 写入，按已发布定义摘要冻结）；0110 的 `control.nexloop_contact_assert_intent` 改名为 `..._before_effect_category_v0110` 保留，外包一层：声明为非触达且摘要一致、无外发记录、声明的通知参数全空才直接放行，带通知整条拒绝（NXC05 `attached_notification`），其余照 0109/0110；承诺证据种类改按类别判定 | 0109/0110 联系检查、0040 effect intents、0079 外发记录 |
| 0113 `nx026_commitment_context` | 切片三：v6 open_work 承诺子段。`authz.nexloop_context_read` 的 open_work、`nexloop_context_v6_source_in`、`nexloop_context_v6_sections` 以 0108 函数体加承诺部分；`runtime.nexloop_commitment_context` | 0108 |

已发布迁移一字未改。新函数一律 `search_path=pg_catalog,pg_temp`，SECURITY DEFINER 函数 owner 为 `nexloop_owner` 并撤销 PUBLIC。

## 2. 关键语义（与设计稿一致的部分不再重复）

- **登记**：只登记企业一方（speaker=agent）、肯定、asserted/conditional 的 commitment Claim，且其来源是渠道已接受并物化的外发消息；其余记 `skipped` 事件。去重键 = (tenant, world, 来源消息, correlation_key)；同一承诺出现在后来的已送达消息中：期限相同记 `reaffirmed`，期限不同则新承诺 supersede 旧承诺（旧承诺转 cancelled，已违约的保持 breached）。
- **对象守卫**：创建只接受登记表里预先推导的属性；编辑只接受 `runtime.nexloop_commitment_target` 由当前对象与账本推导出的补丁（守卫在写入时重新推导）；内容字段因此不可改；任何角色都不能删除 Commitment。keeper 的逐对象编辑授权由 0084 属性规则派生，只覆盖 `commitment_lifecycle` 组（status / late / fulfillment_basis / fulfillment_evidence）。
- **合格证据**：声明为非触达服务且无通知的 effect 回执、已核验商业事件、人类 attest；送达消息只在人类标记为沟通类承诺之后、且送达时间晚于标记时才合格（D3，不追认）。顾客口头确认与 Agent 报告只显示、永不合格（AT-040）。
- **到期**：`lead_seconds` 前标记该 Consumer 的 active 计划（T7）；到期仍无合格证据转 `breached`，写异常 `breached`（含是否暂停、是否联系受限）并再次标记计划；之后的合格证据转 `fulfilled(late=true)`，违约事件保留。条件承诺到期不违约，写 `condition_unresolved_at_due`。无期限承诺在登记时标记计划（澄清），超过 `unspecified_max_open_seconds` 写 `no_due_date`。没有 active 计划写 `no_active_plan`。
- **联系限制**：受限客户的触达类履约意图派发被拒时，monitor 写 `blocked_by_contact_restriction`；受限期间作出的承诺写 `made_under_contact_restriction`；兜底回复 Run 不能携带 `commitment_ref`（SQL 拒绝其提交）。
- **D7**：来源 Message 删除后，读取端口与 v6 不再返回原文（`quote` 为空，`source_available=false`），状态、证据、事件保留，写异常 `source_deleted`；更正（Claim superseded）写 `source_superseded`，不自动取消。

## 3. 部署材料

- `deploy/ontology/business-object-types.v1.json`（新，Commitment v1；D1 选择新建而非并入 system-object-types，理由写在清单 decision 与设计稿 §12）。
- `deploy/configuration/business-actions.v1.json` v5：Commitment.create / Commitment.edit（service，commitment_keeper）与五个 human_owner Action；`business_actions.PROFILES` 增加对应 human_owner profile。
- `deploy/authorization/service-grants.v1.json` v10：commitment_keeper 主体、7 项授权、`commitment_lifecycle` 属性规则。负责人对 Commitment 不设受限组的决定在 `owner-property-restrictions.json`（main `f7e391d`，本分支未改此文件）。
- `deploy/configuration/commitments.v1.json`（设置）、`deploy/configuration/action-effect-categories.v1.json`（D6 声明；`nexloop.service.request:1` 为 customer_contact）。`python -m nexloop_eios.effect_categories --apply` 只能以 configurator 连接写入。
- 后台入口 `nexloop-commitment-keeper`（容器作业 `commitment-keeper`，compose 可选 profile `background`）。

## 4. 与设计稿的差异

1. 登记时不做目录外判定（设计 §3.2 第 1 项、测试 21）：`assess_request_scope` 只评估结构化请求范围，无法对承诺原文做确定性判定；预防点仍在派发前（AT-010）。
2. registrar 与 monitor 合并为一个服务主体 `commitment_keeper`（两个 feed、一个 worker）。
3. Agent 的 `nexloop.commitment.report` Host 工具未实现（设计中为默认关闭的可选项）；账本保留 `agent_report` 种类，只显示不合格。
4. 顾客确认（`consumer_confirmation`）没有自动关联：顾客陈述与具体承诺的对应无法确定性推导，账本保留该种类（只显示）。
5. `read_open_commitments` 以 v6 open_work 承诺子段提供（Run 在 Context 中读取），未新增 Host 工具；契约 `context-pack-v6` 无需修改（subsection 为自由字符串，open_work 已允许 `formal_object`）。

## 5. 测试（真实 PG，合成数据）

实际执行（Mac，PG 18.4，`LC_ALL=en_US.UTF-8`）：
- NX-026 新测试：`test_commitments_pg.py` 11、`test_commitment_guards_pg.py` 7、`test_contact_effect_categories_pg.py` 7、`test_commitment_context_v6_pg.py` 3、`test_commitment_e2e_pg.py` 1（真实 Host/Pi，`--guard-workers 4`），全部通过。
- 回归批 A（`-n 4`，27 个文件，含上述与 service-grants / 后台入口 / 容器契约 / claim store / 联系限制 / 目标控制 / work feed / 匹配 / 计划复评与结果 / effect 派发与 SQL / NX-022 派发 / 属性派生 / 审核类型 / 可信配置 / bootstrap / 消息读派生 / 闭环知识）：首次 256 passed、2 failed（`test_business_actions` 与 `test_system_object_types` 的清单一致性断言未含 Commitment），更新后 21 passed。
- v6 回归（`test_context_v6_pg`、`test_role_context_v6_pg`、`test_plan_wake_pg`、`test_context_v6_relationships_pg`）35 passed；`test_plan_role_launcher_pg` 与承诺 v6 测试一起 7 passed。
- 回归批 B（真实 Host/Pi，串行：`test_contact_reply_dispatch_pg`、`test_reply_fallback_pg`、`test_closure_refusal_versions_pg`、`test_outbound_messages_pg`、`test_closure_plan_run_pg`、`test_commitment_e2e_pg`）22 passed。
- service-grants：合入 main `f7e391d` 前 4 例因负责人对 Commitment 的受限组决定缺失而失败；合入后又有 2 例因断言只考虑一条属性规则而失败，测试改为按类型区分后 33 passed。

文件：`tests/test_commitments_pg.py`、`tests/test_commitment_guards_pg.py`、`tests/test_contact_effect_categories_pg.py`、`tests/test_commitment_context_v6_pg.py`、`tests/test_commitment_e2e_pg.py`（真实 Host/Pi，guard 4 进程），以及更新的 `test_background_services_pg.py`、`test_community_container_contract.py`、`test_service_grants_pg.py`。夹具 `tests/commitment_fixture.py` 基于真实受治理 effect 执行器；NX-047 外发账本行与 Claim 由 admin 播种（真实链路由 e2e 覆盖）。
