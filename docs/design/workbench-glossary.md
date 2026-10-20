# 工作台术语表（草案，2026-10-20）

- **用途**：工作台 UI 整理的统一用词。L6 的 UI 巡检和规范直接引用本表。前端可直接引用的映射草案在 `apps/web/src/workbench/labels.ts`；该文件只新增，**还没有接入任何页面**，接入留给 L6 的实施批次。
- **规则**（负责人 2026-10-20 已定）：
  1. **实体名统一用 EIOS 英文**，以上游 EIOS 冻结版本为准，例如 Object Type、Object、Property、Link、Function、Action、Event Type、Artifact、Claim。
  2. **说明性文字用中文**。实体名可以夹在中文句子里，例如“＋ 新建 Object Type”，这也是上游的写法。
  3. **状态码和枚举值一律显示中文标签**；原始码只出现在悬停提示或详情里。
- **来源**：前端 `apps/web/src`、后端读接口与迁移中的 CHECK 约束（基线 main `7a153a29`），以及上游 EIOS 的 `apps/portal/src/features/world-model/concepts.ts`。“建议”表示目前前端没有标签，由本表提出。

## 1. 实体术语

| 英文（显示用） | 中文释义（只用于说明文字） | 出现的页面 | 说明 |
|---|---|---|---|
| **Object Type** | 对象类型：稳定的名字、不可变的版本和一组 Property 契约 | 企业世界模型 | 现在页面上混用“对象类型”（图、空态、列名），统一为 Object Type |
| **Object** | 对象：一个具体的企业对象，带 Schema 版本和来源 | 企业世界模型（原“实例”标签页） | **不再叫“实例”**。上游的 UI 标签就是 Object |
| **Property** | 属性：Object Type 拥有的字段定义，带数据类型和约束 | 企业世界模型、客户详情 | 现在混用“属性” |
| **Link** | 关系：类型之间的关系及其基数约束 | 企业世界模型 | 上游 UI 用 Link（README 中叫 Relation Type）。现在混用“关系”“关系类型”，统一为 Link |
| **Event Type** | 事件类型 | 企业世界模型 | 现在的标签页叫“事件类型”，改为 Event Type，与相邻的标签页一致 |
| **Function** | 只读计算契约，带版本，绑定 Capability | 企业世界模型 | — |
| **Action** | 写入契约，带版本，含治理与副作用声明 | 企业世界模型、能力目录 | 只指**定义**。运行中的执行见下一行 |
| Action 执行 | 一次 Action 的运行记录（意图、派发、结果） | “Action / 异常”页 | 页面名建议改为“Action 执行”，与 Action 定义区分；能力目录中的“业务 Action”统一为“Action” |
| **Schema Version** | Schema 版本 | 企业世界模型、Object 列表 | 列名统一为 Schema Version，值显示为 `v{n}` |
| **Artifact** | 产物：Run 产生、带保留期的文件 | 数据资产 | 只用英文，不用“产物”“附件” |
| **Claim** | 从对话中提取、带原文区间的陈述 | 数据资产、知识工作台 | 只用英文 |
| Candidate Definition | 候选定义：提取侧未匹配、待审核的定义 | 知识工作台 | 页面上可简称“候选” |
| Capability | 能力：可被授权执行的操作 | 能力目录 | — |
| Run | 一次短生命周期、有预算的处理 | 计划与运行 | — |
| 企业世界模型 | 整个本体的页面名 | 导航 | 页面名用中文；“Ontology”只出现在代码和权限名里，与上游一致 |
| Object Set / Object View / Interface | 上游有的概念 | — | NexLoop 目前没有对应页面，暂不使用 |

“实例”一词今后只用于知识条目类型（`instance`），不再用来指 Object。

## 2. 状态码与枚举 → 中文标签

表中“现有”表示前端已有的标签；“建议”表示新提出的标签；**粗体**表示需要修正（见 §4）。原始码放在悬停提示或详情里。

### 2.1 承诺（Commitment）

| 字段 | 原始码 → 中文标签 |
|---|---|
| status（现有） | conditional 有条件 · open 未履行 · in_progress 处理中 · breached 已违约 · fulfilled 已履行 · cancelled 已取消 · superseded 已被取代 |
| status_group（现有） | open 未结束 · ended 已结束 · all 全部 |
| origin（建议） | claim 来自对话 · extend 延期生成 |
| 事件 kind（建议） | registered 已登记 · reaffirmed 再次确认 · skipped 已跳过 · superseded 已被取代 · cancel_requested 已请求取消 · extend_requested 已请求延期 · condition_met 条件已满足 · marked_communication 标记为沟通类 · status 状态变更 · due_soon 即将到期 |
| 证据 kind（建议） | requested 已请求执行 · effect_fulfilled 动作已完成 · delivered_message 消息已送达 · consumer_confirmation 客户已确认 · problem_resolution 问题已解决 · commercial_event 商业事件 · operator_attestation 人工证明 · agent_report Agent 报告 |
| 异常原因（建议） | breached 已违约 · due_in_past 期限已过 · no_due_date 没有期限 · condition_unresolved_at_due 到期时条件未定 · no_active_plan 没有进行中的计划 · blocked_by_contact_restriction 因联系限制受阻 · made_under_contact_restriction 受限期间作出 · source_deleted 来源已删除 · source_superseded 来源已被取代 · unauthorized_incentive_promise 未授权的优惠承诺 |

### 2.2 Claim 与提取

| 字段 | 原始码 → 中文标签 |
|---|---|
| Claim 认知类型 epistemic_kind | user_statement 用户陈述 · preference 偏好 · hypothesis 推测 · commitment 承诺 · constraint 约束 · intent 意图 · need_problem 需求或问题 · correction 更正 |
| Claim 解析状态 resolution_state | unresolved 未解决 · resolved 已解决 · superseded 已被取代 · hypothesis_only 仅推测 · needs_resolution 待澄清 · awaiting_definition 等待定义 · rejected_definition 定义被拒 |
| 召回索引 index_kind（现有） | definition 定义 · instance 实例 |
| 候选类型（现有，review-api） | object_type 新 Object Type · property 新 Property · vocabulary_value 新词表值 · alias 别名 · object_instance 新 Object |
| 候选状态（建议） | pending_review 待审核 · staged 待粘合 · merged 已并入 · published 已发布 · rejected 已拒绝 · superseded 已被取代 |
| 匹配结果 outcome（建议） | full_match 完全匹配 · partial_match 部分匹配 · no_match 不匹配 · hypothesis 推测 · needs_resolution 待澄清 · noop 无变化 |
| 匹配提议状态（建议） | proposed 已提议 · applying 应用中 · applied 已应用 · conflict 冲突 · rejected 已拒绝 · superseded 已被取代 |
| 召回方式（建议） | lexical 词匹配 · vector 向量 |
| 合并配置（现有） | active 已生效 · missing 未生效 |
| Embedding 预算原因（建议） | not_recorded 未记录 |
| Artifact 用途（现有） | message_context 消息上下文 · fallback_context 兜底上下文 · role_context 角色上下文 · other 其他 |

### 2.3 队列

| 字段 | 原始码 → 中文标签 |
|---|---|
| 任务状态（现有） | pending 待处理 · running 运行中 · retry_wait 等待重试 · succeeded 完成 · failed 失败 · dead_lettered 死信 |
| 队列名（建议） | claim-extraction 消息提取 · operations 运营任务 |
| 后台 feed（建议） | recall-instance 实例召回 · claim-match Claim 匹配 · plan-reevaluate 计划复评 · reply-due 来信待回复 · commitment-register 承诺登记 · commitment-monitor 承诺监控 · commercial-record 商业记录 · takeover-expiry 接管到期 |
| 进程样本（部分现有） | guard 守卫时延 · host Agent Host 并发 · pool 连接池 · guard_op 守卫操作 · relay_step relay 步骤 |

### 2.4 外联

| 字段 | 原始码 → 中文标签 |
|---|---|
| 闸门 gate（建议） | control_paused 已暂停 · contact_restricted 客户联系受限 · purpose 联系目的 · persona_excluded 非真实客户 · quiet_hours 勿扰时段 · frequency 联系频次 · consumer_preference 客户偏好 · real_dispatch 真实派发开关 · budget 预算 |
| 闸门结论（建议） | pass 通过 · refuse 拒绝 · not_wired 未接通 |
| 真实派发细节（建议） | open 已开启 · deployment 部署层关闭 · world 环境关闭 · tenant 租户层关闭 · action Action 层关闭 |
| 偏好拒绝（建议） | preference_quiet_hours 客户勿扰时段 · preference_frequency 超过客户频次 · preference_channel 客户拒绝该渠道 · preference_topic 客户拒绝该主题 |
| 目的（建议） | commitment_followup 承诺跟进 · service_followup 服务跟进 · marketing 营销 |
| 同意模式 | opt_out 默认可发 · opt_in 需明确同意（详情里保留现有的长文案） |
| 控制来源（建议） | tenant 租户设置 · default 系统默认 |
| AI 披露 mode（现有） | none 不主动表明 · first_contact 首次接触时表明一次 · always 每次会话开始时表明 |
| 被问到时 when_asked（建议） | deflect 婉转回避 · handoff 转人工 |
| 表情 emoji（建议） | none 不用 · few 少量 · allowed 允许 |
| 人设状态（现有） | published 已发布 · retired 已停用 |
| 人设来源（现有） | owner_assign 负责人指定 · rule 分群规则 · default 租户默认 |
| 人设事件来源（建议） | initial 初始 · owner_clear 负责人清除 · rule_change 规则变更 · persona_retired 人设停用 |
| 偏好维度（现有） | quiet_hours 勿扰时段 · frequency 每 7 天最多联系次数 · channel 联系渠道 · topic 联系主题 · tone 沟通语气 |
| 客户人设 persona（建议） | real 真实客户 · simulated_agent 模拟 Agent · synthetic 合成数据 |

### 2.5 告警

- **级别**（现有）：warning 警告 · critical 严重。
- **事件**（现有）：firing 触发 · resolved 恢复。
- **规则名**（现有，`pages/alertInfo.ts`，覆盖 `alert-rules.v5.json` 全部 32 条）：沿用现有名称，原始 rule_id 只放在悬停提示里。
- **selector**（建议）：队列名和 feed 名用 §2.3 的标签；repo1 主仓库 · repo2 异地仓库；budget_kind 用 §2.8 的标签。

### 2.6 效应（Action 执行）

| 字段 | 原始码 → 中文标签 |
|---|---|
| 意图状态（现有，补一项） | accepted 已受理 · dispatching 派发中 · unknown 待核对 · fulfilled 渠道已完成 · failed 失败 · confirmed 已确认 · **observed_fulfilled 观察到已完成** |
| outbox 状态（建议） | pending 待派发 · leased 处理中 · unknown 待核对 · query_pending 待查询 · fulfilled 已完成 · failed 失败 |
| 渠道观测 provider_state（建议） | accepted 渠道已接收 · fulfilled 渠道已完成 · not_found 渠道查无此单 |
| 出站投递 delivery_state（建议） | persisted 已保存 · dispatching 发送中 · provider_accepted 渠道已接收 · delivered 已送达 · unknown 待核对 · failed 发送失败 |
| 确认未送达（建议） | confirmed_not_delivered 已确认未送达 |
| 回复跟进（建议） | owner_follow_up 负责人跟进 · fallback_reply 兜底回复 · none 无 |
| 确认前检查（建议） | eligible 可以确认 · intent_unknown 意图不存在 · already_determined 已确认过 · not_unknown 不是待核对状态 · effect_busy 正在处理中 · no_attempt 没有发送记录 · no_not_found_evidence 缺少查无证据 · wait_period 仍在等待期 · retention_unconfirmed 未确认渠道保留期 · fallback_unavailable 兜底回复不可用 · invalid 填写无效 |
| 效应类别（建议） | customer_contact 客户联系 · non_contact_service 非联系服务 |
| 派发预判原因（现有，补一项） | control_paused 已暂停 · control_revision_stale 控制已变更 · goal_version_stale 目标已改版 · object_revision_stale 对象已变更 · contact_restricted 客户联系受限 · attached_notification 附带通知被拒 · taken_over 人工接管中 · not_queued 不在派发队列 · **unavailable 预判暂不可用** |
| 拒绝码（现有，补三项） | NXC01–NXC06、NXB01–NXB03、NXM01 沿用现有标签 · **NXC07 真实派发未开启 · NXR01 恢复后未核验 · NXE01 未配置渠道路由** |

### 2.7 审核

| 字段 | 原始码 → 中文标签 |
|---|---|
| 审核决定（现有） | approve 批准发布 · merge_into 并入已有定义 · reject 拒绝 |
| 审核结果（现有，写在句子里） | rejected 已拒绝 · merged 已并入 · published 已发布 · publication_failed 发布失败 |
| 回流状态（建议） | none 无需回流 · pending 回流中 · done 已回流 · waiting 等待中 |
| 等待原因（部分现有） | awaiting_grants 等待授权 · authority_denied_awaiting_grant 授权被拒，等待授予 · invalid_properties 属性无效 · recipe_enterprise_unsupported 当前配方不支持企业 · revision_conflict 版本冲突 |
| 门槛失败（建议） | instance_already_approved Object 已批准 · instance_already_exists Object 已存在 · type_create_action_unavailable 缺少创建 Action · identifying_properties_invalid 标识 Property 无效 · instance_type_unavailable Object Type 不可用 · invalid_type_reference 类型引用无效 · simulation_or_shadow_candidate_cannot_create_real_instance 模拟候选不能建真实 Object · new_type_actions_must_be_create_and_edit 新类型需有创建与编辑 Action · new_type_actions_bind_different_schemas Action 绑定的 Schema 不一致 |

### 2.8 控制、联系与计划

| 字段 | 原始码 → 中文标签 |
|---|---|
| 暂停范围 scope_kind（现有，未统一使用） | tenant 整个租户 · consumer 客户 · role 角色 · strategy 策略 · action_type Action 类型 · goal 目标 · budget 预算 · metric 指标 |
| 控制事件（建议） | pause 暂停 · resume 恢复 · budget 预算变更 · goal_version 目标改版 · agent_goal Agent 目标 · metric 指标变更 · contact_restricted 联系受限 · contact_released 解除联系限制 · takeover 人工接管 · handback 交还 Agent · real_dispatch 真实派发开关 |
| 拒绝确定度（建议） | refuse 明确拒绝 · uncertain 疑似拒绝 |
| 升级原因（建议） | fallback_unanswered 兜底未回复 · reply_unknown 回复结果待核对 · fallback_unavailable 兜底不可用 · fallback_failed 兜底失败 · fallback_not_started 兜底未启动 · owner_follow_up 负责人跟进 |
| 接管（部分现有） | conversation 本会话 · consumer 整个客户 · handback 已交还 · expired 已到期 |
| 真实派发 data_mode（建议） | real 真实 · test 测试 |
| 计划状态（建议） | active 进行中 · superseded 已被取代 · stale 待复评 · invalidated 已失效 · closed 已关闭 |
| 计划结果（建议） | no_action 无需行动 · needs_information 需补充信息 · waiting_external 等待外部 · escalate 升级人工 · plan_update 计划更新 · action_intent 发起动作 |
| 目标类型（建议） | long_term 长期 · stage 阶段 · agent Agent |
| 预算类型（建议） | model 模型费用 · incentive 激励 · evolution 演进（S6） |
| 费用类型（建议） | model 模型 · channel 渠道 · discount 折扣 · service 服务 · labour 人工 |
| 数据模式（建议） | real 真实 · test 测试 · simulation 模拟 |
| 人工请求（建议） | plan_reevaluation 手动复评 · effect_query 查询执行结果 |

### 2.9 恢复、删除与其他

| 字段 | 原始码 → 中文标签 |
|---|---|
| 恢复状态（建议） | restored 已恢复 · replayed 已重放 · verified_readonly 只读核验 · open 已开放 |
| 备份仓库（建议） | repo1 主仓库 · repo2 异地仓库 |
| 删除状态（建议） | pending_confirmation 待确认 · blocking 阻塞中 · settling 结算中 · waiting_unknown_effects 等待待核对动作 · purging 清除中 · completed 已完成 · completed_with_holds 已完成（有保留） |
| HTTP 错误码（现有） | unauthenticated 未登录 · forbidden 无权限 · not_found 不存在 · invalid_request 请求无效 · not_allowed_in_state 当前状态不允许 · conflict 冲突 · request_timeout 请求超时 · unavailable 暂不可用（只读接口的 dependency_unavailable 也按此显示） |
| 409 细节原因（建议） | document_invalid 人设内容无效 · version_exists 版本已存在 · version_not_next 版本号不连续 · unknown_version 版本不存在 · not_published 人设未发布 · not_a_preference 不是联系偏好 · consent_mode_unconfirmed 未确认同意模式 · persona_real 真实客户不可擦除 |
| 审计类别（现有） | action 操作 · read 读取 |
| 审计目标（建议） | message 消息 · consumer 客户 · goal 目标 · plan_step 计划步骤 · commitment 承诺 · service_offering 服务项 · artifact Artifact · candidate 候选 · alert_rule 告警规则 · conversation 会话 |
| 成员角色（现有） | owner 负责人 · operator 运营 · reviewer 审核人 |

## 3. 页面与导航名称

| 路由 id | 现在的名称 | 统一写法 | 分组 |
|---|---|---|---|
| overview | 总览 | 总览 | 运营 |
| consumers | 消费者 | 消费者 | 运营 |
| commitments | 承诺 / 交付 | 承诺 / 交付 | 运营 |
| actions | Action / 异常 | **Action 执行**（与 Action 定义区分） | 运营 |
| plans | 计划与运行 | 计划与运行 | 运营 |
| goals | 目标与对齐 | 目标与对齐 | 运营 |
| contact | 联系限制 | 联系限制 | 运营 |
| ontology | 企业世界模型 | 企业世界模型 | 模型与资产 |
| data_assets | 数据资产 | 数据资产 | 模型与资产 |
| knowledge | 知识工作台 | 知识工作台 | 模型与资产 |
| alerts | 告警 | 告警 | 监控 |
| operations | 运行状态 | 运行状态 | 监控 |
| audit | 审计 | 审计 | 监控 |
| settings | 设置与治理 | 设置与治理 | 管理 |
| personas | 运营人设 | 运营人设 | 管理 |
| capabilities | 能力目录 | 能力目录 | 管理 |
| experiments | 实验空间 | 实验空间（未启用） | 管理 |

**企业世界模型的内部结构**：侧栏按可展开的分组列出 Object Type、Object、Property、Link、Event Type、Function、Action；Table 与 Graph 是 Object Type 页内的次级视图，显示为“Table / Graph”，不再用“表格 / 关系图”。

**其他统一写法**：

| 现在 | 统一为 |
|---|---|
| 实例、{type} 实例 | Object、{type} 的 Object |
| 对象类型（图、空态、列名） | Object Type |
| 属性（详情标题、空态） | Property |
| 关系、关系类型 | Link |
| 事件类型 | Event Type |
| 业务 Action | Action |
| 版本（Object Type 列） | Schema Version |

## 4. 盘点中发现、需要在实施批次修正的问题

1. **Claim 认知类型**：`DataAssets.tsx` 的标签里有后端不存在的 `verified_fact`，又缺少 `constraint`、`intent`、`need_problem`、`correction`。
2. **Claim 解析状态**：前端有后端不存在的 `rejected`，又缺少 `hypothesis_only`、`needs_resolution`、`awaiting_definition`、`rejected_definition`。
3. **意图状态**：缺 `observed_fulfilled`，页面显示为“未知状态”；客户摘要（`Consumers.tsx`）直接显示原始码。
4. **拒绝码**：缺 `NXC07`、`NXR01`、`NXE01`。
5. **派发预判**：联系偏好拒绝（`NXC06` 路径）被预判成 `taken_over`，页面显示“人工接管中”，与实际原因不符；`unavailable` 没有标签。这需要后端区分，不只是改标签。
6. **409 细节**：前端只显示通用的“当前状态不允许”，没有读取服务端返回的 `details.reason`。
7. **暂停范围标签**：`SCOPE_TEXT` 只用在暂停摘要里，其他地方显示原始码。
8. **实体名混用**：见 §1 和 §3 的对照。
9. **派发预演没有页面**：外联闸门的标签（§2.4）目前没有任何页面使用，等预演页面上线时使用。
10. **审计**：动作名（`control.*`、`commitment.*`、`request.*`、`takeover.*` 等）直接显示原始码；建议用 §2.8 的标签拼接。

## 5. 维护

- 新增枚举值时，必须同时在本表和 `labels.ts` 中补上标签。
- 本表以后端 CHECK 约束为准；前端有、后端没有的值要删除，不要保留。
- 没有列入本表的值（例如指标聚合方式、商业录入结果、优惠状态、Context manifest 的 section）目前没有工作台读取接口返回它们；等有页面使用时再补。
