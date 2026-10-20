/**
 * Workbench display labels (draft, 2026-10-20). Source of truth: docs/design/workbench-glossary.md.
 *
 * Not wired into any page yet: the pages keep their own maps until the L6 UI batch replaces them with these. Every status
 * code or enum value shows its Chinese label; the raw code belongs in a tooltip or a details view only (owner 2026-10-20).
 * Entity names stay in EIOS English (Object Type, Object, Property, Link, Event Type, Function, Action, Artifact, Claim).
 */
export type LabelMap = Readonly<Record<string, string>>;

/** The label of a code, with the raw code kept for the tooltip; an unlisted code is shown as 未知状态 and keeps its code. */
export function labelOf(map: LabelMap, code: string | null | undefined): {text: string; code: string} {
  const raw = code ?? '';
  return {text: Object.prototype.hasOwnProperty.call(map, raw) ? map[raw] : '未知状态', code: raw};
}

export const COMMITMENT_STATUS: LabelMap = {
  "conditional": "有条件",
  "open": "未履行",
  "in_progress": "处理中",
  "breached": "已违约",
  "fulfilled": "已履行",
  "cancelled": "已取消",
  "superseded": "已被取代",
};

export const COMMITMENT_STATUS_GROUP: LabelMap = {
  "open": "未结束",
  "ended": "已结束",
  "all": "全部",
};

export const COMMITMENT_ORIGIN: LabelMap = {
  "claim": "来自对话",
  "extend": "延期生成",
};

export const COMMITMENT_EVENT: LabelMap = {
  "registered": "已登记",
  "reaffirmed": "再次确认",
  "skipped": "已跳过",
  "superseded": "已被取代",
  "cancel_requested": "已请求取消",
  "extend_requested": "已请求延期",
  "condition_met": "条件已满足",
  "marked_communication": "标记为沟通类",
  "status": "状态变更",
  "due_soon": "即将到期",
};

export const COMMITMENT_EVIDENCE: LabelMap = {
  "requested": "已请求执行",
  "effect_fulfilled": "动作已完成",
  "delivered_message": "消息已送达",
  "consumer_confirmation": "客户已确认",
  "problem_resolution": "问题已解决",
  "commercial_event": "商业事件",
  "operator_attestation": "人工证明",
  "agent_report": "Agent 报告",
};

export const COMMITMENT_EXCEPTION: LabelMap = {
  "breached": "已违约",
  "due_in_past": "期限已过",
  "no_due_date": "没有期限",
  "condition_unresolved_at_due": "到期时条件未定",
  "no_active_plan": "没有进行中的计划",
  "blocked_by_contact_restriction": "因联系限制受阻",
  "made_under_contact_restriction": "受限期间作出",
  "source_deleted": "来源已删除",
  "source_superseded": "来源已被取代",
  "unauthorized_incentive_promise": "未授权的优惠承诺",
};

export const CLAIM_EPISTEMIC_KIND: LabelMap = {
  "user_statement": "用户陈述",
  "preference": "偏好",
  "hypothesis": "推测",
  "commitment": "承诺",
  "constraint": "约束",
  "intent": "意图",
  "need_problem": "需求或问题",
  "correction": "更正",
};

export const CLAIM_RESOLUTION: LabelMap = {
  "unresolved": "未解决",
  "resolved": "已解决",
  "superseded": "已被取代",
  "hypothesis_only": "仅推测",
  "needs_resolution": "待澄清",
  "awaiting_definition": "等待定义",
  "rejected_definition": "定义被拒",
};

export const RECALL_INDEX_KIND: LabelMap = {
  "definition": "定义",
  "instance": "实例",
};

export const CANDIDATE_KIND: LabelMap = {
  "object_type": "新 Object Type",
  "property": "新 Property",
  "vocabulary_value": "新词表值",
  "alias": "别名",
  "object_instance": "新 Object",
};

export const CANDIDATE_STATUS: LabelMap = {
  "pending_review": "待审核",
  "staged": "待粘合",
  "merged": "已并入",
  "published": "已发布",
  "rejected": "已拒绝",
  "superseded": "已被取代",
};

export const MATCH_OUTCOME: LabelMap = {
  "full_match": "完全匹配",
  "partial_match": "部分匹配",
  "no_match": "不匹配",
  "hypothesis": "推测",
  "needs_resolution": "待澄清",
  "noop": "无变化",
};

export const MATCH_PROPOSAL_STATUS: LabelMap = {
  "proposed": "已提议",
  "applying": "应用中",
  "applied": "已应用",
  "conflict": "冲突",
  "rejected": "已拒绝",
  "superseded": "已被取代",
};

export const RECALL_METHOD: LabelMap = {
  "lexical": "词匹配",
  "vector": "向量",
};

export const MERGE_CONFIGURATION: LabelMap = {
  "active": "已生效",
  "missing": "未生效",
};

export const ARTIFACT_PURPOSE: LabelMap = {
  "message_context": "消息上下文",
  "fallback_context": "兜底上下文",
  "role_context": "角色上下文",
  "other": "其他",
};

export const JOB_STATUS: LabelMap = {
  "pending": "待处理",
  "running": "运行中",
  "retry_wait": "等待重试",
  "succeeded": "完成",
  "failed": "失败",
  "dead_lettered": "死信",
};

export const QUEUE_NAME: LabelMap = {
  "claim-extraction": "消息提取",
  "operations": "运营任务",
};

export const FEED_NAME: LabelMap = {
  "recall-instance": "实例召回",
  "claim-match": "Claim 匹配",
  "plan-reevaluate": "计划复评",
  "reply-due": "来信待回复",
  "commitment-register": "承诺登记",
  "commitment-monitor": "承诺监控",
  "commercial-record": "商业记录",
  "takeover-expiry": "接管到期",
};

export const PROCESS_SAMPLE: LabelMap = {
  "guard": "守卫时延",
  "host": "Agent Host 并发",
  "pool": "连接池",
  "guard_op": "守卫操作",
  "relay_step": "relay 步骤",
};

export const OUTREACH_GATE: LabelMap = {
  "control_paused": "已暂停",
  "contact_restricted": "客户联系受限",
  "purpose": "联系目的",
  "persona_excluded": "非真实客户",
  "quiet_hours": "勿扰时段",
  "frequency": "联系频次",
  "consumer_preference": "客户偏好",
  "real_dispatch": "真实派发开关",
  "budget": "预算",
};

export const OUTREACH_GATE_RESULT: LabelMap = {
  "pass": "通过",
  "refuse": "拒绝",
  "not_wired": "未接通",
};

export const REAL_DISPATCH_DETAIL: LabelMap = {
  "open": "已开启",
  "deployment": "部署层关闭",
  "world": "环境关闭",
  "tenant": "租户层关闭",
  "action": "Action 层关闭",
};

export const PREFERENCE_REFUSAL: LabelMap = {
  "preference_quiet_hours": "客户勿扰时段",
  "preference_frequency": "超过客户频次",
  "preference_channel": "客户拒绝该渠道",
  "preference_topic": "客户拒绝该主题",
};

export const OUTREACH_PURPOSE: LabelMap = {
  "commitment_followup": "承诺跟进",
  "service_followup": "服务跟进",
  "marketing": "营销",
};

export const CONSENT_MODE: LabelMap = {
  "opt_out": "默认可发",
  "opt_in": "需明确同意",
};

export const CONTROLS_SOURCE: LabelMap = {
  "tenant": "租户设置",
  "default": "系统默认",
};

export const AI_DISCLOSURE_MODE: LabelMap = {
  "none": "不主动表明",
  "first_contact": "首次接触时表明一次",
  "always": "每次会话开始时表明",
};

export const AI_WHEN_ASKED: LabelMap = {
  "deflect": "婉转回避",
  "handoff": "转人工",
};

export const PERSONA_EMOJI: LabelMap = {
  "none": "不用",
  "few": "少量",
  "allowed": "允许",
};

export const PERSONA_STATUS: LabelMap = {
  "published": "已发布",
  "retired": "已停用",
};

export const PERSONA_SOURCE: LabelMap = {
  "owner_assign": "负责人指定",
  "rule": "分群规则",
  "default": "租户默认",
};

export const PERSONA_EVENT_SOURCE: LabelMap = {
  "initial": "初始",
  "owner_clear": "负责人清除",
  "rule_change": "规则变更",
  "persona_retired": "人设停用",
};

export const PREFERENCE_DIMENSION: LabelMap = {
  "quiet_hours": "勿扰时段",
  "frequency": "每 7 天最多联系次数",
  "channel": "联系渠道",
  "topic": "联系主题",
  "tone": "沟通语气",
};

export const CONSUMER_PERSONA: LabelMap = {
  "real": "真实客户",
  "simulated_agent": "模拟 Agent",
  "synthetic": "合成数据",
};

export const ALERT_SEVERITY: LabelMap = {
  "warning": "警告",
  "critical": "严重",
};

export const ALERT_EVENT: LabelMap = {
  "firing": "触发",
  "resolved": "恢复",
};

export const BACKUP_REPO: LabelMap = {
  "repo1": "主仓库",
  "repo2": "异地仓库",
};

export const INTENT_STATE: LabelMap = {
  "accepted": "已受理",
  "dispatching": "派发中",
  "unknown": "待核对",
  "fulfilled": "渠道已完成",
  "failed": "失败",
  "confirmed": "已确认",
  "observed_fulfilled": "观察到已完成",
};

export const OUTBOX_STATE: LabelMap = {
  "pending": "待派发",
  "leased": "处理中",
  "unknown": "待核对",
  "query_pending": "待查询",
  "fulfilled": "已完成",
  "failed": "失败",
};

export const PROVIDER_STATE: LabelMap = {
  "accepted": "渠道已接收",
  "fulfilled": "渠道已完成",
  "not_found": "渠道查无此单",
};

export const DELIVERY_STATE: LabelMap = {
  "persisted": "已保存",
  "dispatching": "发送中",
  "provider_accepted": "渠道已接收",
  "delivered": "已送达",
  "unknown": "待核对",
  "failed": "发送失败",
};

export const DETERMINATION_KIND: LabelMap = {
  "confirmed_not_delivered": "已确认未送达",
};

export const REPLY_FOLLOWUP: LabelMap = {
  "owner_follow_up": "负责人跟进",
  "fallback_reply": "兜底回复",
  "none": "无",
};

export const DETERMINATION_CHECK: LabelMap = {
  "eligible": "可以确认",
  "intent_unknown": "意图不存在",
  "already_determined": "已确认过",
  "not_unknown": "不是待核对状态",
  "effect_busy": "正在处理中",
  "no_attempt": "没有发送记录",
  "no_not_found_evidence": "缺少查无证据",
  "wait_period": "仍在等待期",
  "retention_unconfirmed": "未确认渠道保留期",
  "fallback_unavailable": "兜底回复不可用",
  "invalid": "填写无效",
};

export const EFFECT_CATEGORY: LabelMap = {
  "customer_contact": "客户联系",
  "non_contact_service": "非联系服务",
};

export const DISPATCH_PREDICTION: LabelMap = {
  "control_paused": "已暂停",
  "control_revision_stale": "控制已变更",
  "goal_version_stale": "目标已改版",
  "object_revision_stale": "对象已变更",
  "contact_restricted": "客户联系受限",
  "attached_notification": "附带通知被拒",
  "taken_over": "人工接管中",
  "not_queued": "不在派发队列",
  "unavailable": "预判暂不可用",
};

export const REFUSAL_CODE_ADDITIONS: LabelMap = {
  "NXC07": "真实派发未开启",
  "NXR01": "恢复后未核验",
  "NXE01": "未配置渠道路由",
};

export const REVIEW_DECISION: LabelMap = {
  "approve": "批准发布",
  "merge_into": "并入已有定义",
  "reject": "拒绝",
};

export const REVIEW_OUTCOME: LabelMap = {
  "rejected": "已拒绝",
  "merged": "已并入",
  "published": "已发布",
  "publication_failed": "发布失败",
};

export const REFLOW_STATUS: LabelMap = {
  "none": "无需回流",
  "pending": "回流中",
  "done": "已回流",
  "waiting": "等待中",
};

export const REVIEW_AWAITING: LabelMap = {
  "awaiting_grants": "等待授权",
  "authority_denied_awaiting_grant": "授权被拒，等待授予",
  "invalid_properties": "属性无效",
  "recipe_enterprise_unsupported": "当前配方不支持企业",
  "revision_conflict": "版本冲突",
};

export const REVIEW_GATE_FAILURE: LabelMap = {
  "instance_already_approved": "Object 已批准",
  "instance_already_exists": "Object 已存在",
  "type_create_action_unavailable": "缺少创建 Action",
  "identifying_properties_invalid": "标识 Property 无效",
  "instance_type_unavailable": "Object Type 不可用",
  "invalid_type_reference": "类型引用无效",
  "simulation_or_shadow_candidate_cannot_create_real_instance": "模拟候选不能建真实 Object",
  "new_type_actions_must_be_create_and_edit": "新类型需有创建与编辑 Action",
  "new_type_actions_bind_different_schemas": "Action 绑定的 Schema 不一致",
};

export const SCOPE_KIND: LabelMap = {
  "tenant": "整个租户",
  "consumer": "客户",
  "role": "角色",
  "strategy": "策略",
  "action_type": "Action 类型",
  "goal": "目标",
  "budget": "预算",
  "metric": "指标",
};

export const CONTROL_EVENT: LabelMap = {
  "pause": "暂停",
  "resume": "恢复",
  "budget": "预算变更",
  "goal_version": "目标改版",
  "agent_goal": "Agent 目标",
  "metric": "指标变更",
  "contact_restricted": "联系受限",
  "contact_released": "解除联系限制",
  "takeover": "人工接管",
  "handback": "交还 Agent",
  "real_dispatch": "真实派发开关",
};

export const REFUSAL_CERTAINTY: LabelMap = {
  "refuse": "明确拒绝",
  "uncertain": "疑似拒绝",
};

export const ESCALATION_REASON: LabelMap = {
  "fallback_unanswered": "兜底未回复",
  "reply_unknown": "回复结果待核对",
  "fallback_unavailable": "兜底不可用",
  "fallback_failed": "兜底失败",
  "fallback_not_started": "兜底未启动",
  "owner_follow_up": "负责人跟进",
};

export const TAKEOVER: LabelMap = {
  "conversation": "本会话",
  "consumer": "整个客户",
  "handback": "已交还",
  "expired": "已到期",
};

export const DATA_MODE: LabelMap = {
  "real": "真实",
  "test": "测试",
  "simulation": "模拟",
};

export const PLAN_STATUS: LabelMap = {
  "active": "进行中",
  "superseded": "已被取代",
  "stale": "待复评",
  "invalidated": "已失效",
  "closed": "已关闭",
};

export const PLAN_OUTCOME: LabelMap = {
  "no_action": "无需行动",
  "needs_information": "需补充信息",
  "waiting_external": "等待外部",
  "escalate": "升级人工",
  "plan_update": "计划更新",
  "action_intent": "发起动作",
};

export const GOAL_KIND: LabelMap = {
  "long_term": "长期",
  "stage": "阶段",
  "agent": "Agent",
};

export const BUDGET_KIND: LabelMap = {
  "model": "模型费用",
  "incentive": "激励",
  "evolution": "演进",
};

export const COST_KIND: LabelMap = {
  "model": "模型",
  "channel": "渠道",
  "discount": "折扣",
  "service": "服务",
  "labour": "人工",
};

export const HUMAN_REQUEST: LabelMap = {
  "plan_reevaluation": "手动复评",
  "effect_query": "查询执行结果",
};

export const RESTORE_STATE: LabelMap = {
  "restored": "已恢复",
  "replayed": "已重放",
  "verified_readonly": "只读核验",
  "open": "已开放",
};

export const ERASURE_STATE: LabelMap = {
  "pending_confirmation": "待确认",
  "blocking": "阻塞中",
  "settling": "结算中",
  "waiting_unknown_effects": "等待待核对动作",
  "purging": "清除中",
  "completed": "已完成",
  "completed_with_holds": "已完成（有保留）",
};

export const HTTP_ERROR: LabelMap = {
  "unauthenticated": "未登录",
  "forbidden": "无权限",
  "not_found": "不存在",
  "invalid_request": "请求无效",
  "not_allowed_in_state": "当前状态不允许",
  "conflict": "冲突",
  "request_timeout": "请求超时",
  "unavailable": "暂不可用",
  "dependency_unavailable": "暂不可用",
};

export const CONFLICT_DETAIL: LabelMap = {
  "invalid": "填写无效",
  "document_invalid": "人设内容无效",
  "version_exists": "版本已存在",
  "version_not_next": "版本号不连续",
  "unknown_version": "版本不存在",
  "not_published": "人设未发布",
  "not_a_preference": "不是联系偏好",
  "consent_mode_unconfirmed": "未确认同意模式",
  "persona_real": "真实客户不可擦除",
};

export const AUDIT_CATEGORY: LabelMap = {
  "action": "操作",
  "read": "读取",
};

export const AUDIT_TARGET: LabelMap = {
  "message": "消息",
  "consumer": "客户",
  "goal": "目标",
  "plan_step": "计划步骤",
  "commitment": "承诺",
  "service_offering": "服务项",
  "artifact": "Artifact",
  "candidate": "候选",
  "alert_rule": "告警规则",
  "conversation": "会话",
};

export const MEMBER_ROLE: LabelMap = {
  "owner": "负责人",
  "operator": "运营",
  "reviewer": "审核人",
};

// UI batch 3 (模型与目录): Action governance in the world model (docs/design/workbench-glossary.md; existing page wording).
export const ACTION_RISK: LabelMap = {
  "low": "低",
  "medium": "中",
  "high": "高",
  "critical": "极高",
};

export const ACTION_APPROVAL: LabelMap = {
  "none": "无需审批",
  "human": "人工审批",
  "human_review": "人工审批",
  "dual": "双人审批",
};

// ---- NX-039 V6 (L6 draft 2026-10-20; glossary TODO: L5 to confirm the wording) ----
export const EVO_CANDIDATE_STATUS: LabelMap = {
  "proposed": "已提出",
  "evaluating": "评估中",
  "accepted_for_release": "可发布",
  "rejected": "未通过",
  "incomplete": "未完成",
};

export const EVO_CONCLUSION: LabelMap = {
  "accept": "通过",
  "reject": "不通过",
  "incomplete": "未完成",
};

export const EVO_CHANGE_CLASS: LabelMap = {
  "patch": "补丁级",
  "minor": "次要级",
  "major": "重大（不可发布）",
  "refused": "拒绝发布",
};

export const EVO_GATE: LabelMap = {
  "references_valid": "G1 引用有效",
  "structure_valid": "G1 结构有效",
  "zero_overreach": "G2 零越权",
  "zero_cross_tenant": "G3 零跨租户",
  "zero_fact_pollution": "G4 零事实污染",
  "no_forbidden_regression": "G5 无禁止的行为回退",
  "semantic_regression": "G6 确定性语义回归",
  "cost_performance": "G7 成本与性能",
  "semantic_conflict": "G8 语义不冲突",
  "index_migration_impact": "G9 索引与迁移影响",
};

export const EVO_GATE_RESULT: LabelMap = {
  "pass": "通过",
  "fail": "未通过",
};

export const EVO_DIMENSION: LabelMap = {
  "semantic_content": "语义内容",
  "semantic_tool": "语义工具",
  "ontology_schema": "本体 Schema",
};

export const EVO_END_REASON: LabelMap = {
  "budget_exhausted": "预算用尽",
  "missing_data": "数据已删除或缺失",
  "user_interrupted": "人工停止",
  "external_block": "外部阻塞",
  "evaluator_timeout": "评估超时",
};

export const EVO_REPORT_REASON: LabelMap = {
  "hard_gate_failed": "有门槛未通过",
  "not_better": "没有改善",
  "inferior": "劣于基线",
  "missing_data": "数据缺失",
  "budget_exhausted": "预算用尽",
  "validation_leak": "验证集泄漏",
  "breaking_change": "破坏性变更",
};
