/**
 * F07 (owner walkthrough 2026-10-18): a firing alert in business words. Rule ids, metrics and thresholds are those of
 * deploy/configuration/alert-rules.v6.json; the raw rule id stays visible as secondary text. Thresholds come from the alert's own
 * firing event (the read model has no per-rule threshold yet); where to look is a workbench page.
 */
import {ageText,type AlertEvent,type FiringAlert} from '../api';
import type {PageKey} from '../route';

type Unit='seconds'|'count'|'ratio'|'ms';
export type AlertInfo={name:string;unit:Unit;window?:string;page:PageKey;help:string};
const I=(name:string,unit:Unit,page:PageKey,help:string,window?:string):AlertInfo=>({name,unit,page,help,window});
export const ALERT_INFO:Record<string,AlertInfo>={
  // v6 (D-G5): NexLoop's own data flows; details on the data health page.
  approval_waiting:I('审批请求等待超过 1 小时','seconds','approvals','在审批页查看待审批的请求；到期（默认 24 小时）未批即作废。'),
  approval_unconsumed:I('已批准的请求 1 小时未执行','count','approvals','发起人在审批页的“待我执行”中执行；到期未执行即作废。'),
  approval_solo_mode:I('单人模式已开启','count','approvals','唯一有资格的成员可以批准自己的请求；确认这是有意开启的。'),
  approval_solo_self_approved:I('出现自批','count','audit','在审计中筛选 approval.decided（self_approved）核对。','最近 24 小时'),
  approval_solo_eligibility_reduced:I('批准人数减少','count','audit','有成员的批准角色被移除或被停用；核对是否有意为之。','最近 24 小时'),
  approval_sweep_stale:I('过期审批未被清理','count','operations','查看保留期服务（retention keeper）是否在运行；它负责终结过期的审批。'),
  data_health_extraction_dead_letters:I('提取任务进入死信','count','data_health','在数据健康页查看 Claim 提取；这些消息在处理前不会产生 Claim。'),
  data_health_extraction_backlog:I('消息等待提取超过 1 小时','seconds','data_health','查看提取服务是否在运行。'),
  data_health_recall_missing_embeddings:I('召回条目缺少 embedding','count','data_health','运行一次回填；缺少向量的条目召回不到。'),
  data_health_recall_profile_drift:I('存在旧 profile 的向量','count','data_health','profile 变更后尚未完成回填或清理。'),
  data_health_reflow_stale:I('审核决定等待重流','seconds','data_health','查看知识工作台的审核重流状态。'),
  data_health_signature_rejections:I('商业事件签名被拒','count','data_health','核对连接器的签名密钥是否轮换，或是否有伪造的发送方。','最近 24 小时'),
  data_health_artifact_orphans:I('存在无引用的 Artifact','count','data_health','核对这些 Artifact 是否应当清理。'),
  data_health_artifacts_stuck_deleting:I('Artifact 删除卡住','count','data_health','查看 Artifact 清理服务。'),
  data_health_erasure_stalled:I('客户删除超过 1 天未完成','seconds','data_health','查看删除流程卡在哪个阶段。'),
  data_health_purge_retrying:I('清理项多次重试','count','data_health','查看清理失败的原因码。'),
  refusal_budget_exhausted:I('预算用尽，新工作被拒','count','goals','检查模型或激励预算的上限与消耗。','最近 5 分钟'),
  refusal_control_stale:I('控制已变更导致派发被拒','count','actions','复评未跟上控制变更；查看等待复评的意图。','最近 1 小时'),
  refusal_goal_stale:I('目标改版导致派发被拒','count','actions','查看等待复评的意图与计划。','最近 1 小时'),
  unknown_oldest:I('外部动作结果待核对','seconds','actions','在 Action 页筛选“待核对”，先查询执行结果，不要重发。'),
  unknown_oldest_escalated:I('外部动作待核对超过 2 小时','seconds','actions','在 Action 页筛选“待核对”并查询执行结果；必要时联系渠道。'),
  unknown_over_24h:I('待核对超过 24 小时的外部动作','count','actions','逐个查询执行结果并记录处理。'),
  reply_escalated:I('来信必回兜底失败','count','contact','查看“来信必回的升级记录”，人工回复客户。','最近 1 小时'),
  commitment_breached:I('承诺违约','count','commitments','查看异常承诺，联系客户或延期。','最近 1 小时'),
  commitment_under_restriction:I('受限期间作出的承诺','count','commitments','核对承诺与联系限制。','最近 1 小时'),
  queue_dead_letter:I('队列任务进入死信','count','operations','在运行状态查看对应队列。','最近 1 小时'),
  queue_backlog_age:I('队列积压','seconds','operations','在运行状态查看对应队列的最久待处理时间与处理进程。'),
  feed_dead_letter:I('后台 feed 进入死信','count','operations','在运行状态查看对应 feed。','最近 1 小时'),
  feed_backlog_age:I('后台 feed 积压','seconds','operations','最久一条待处理项等待的时间；在运行状态查看对应 feed 和处理服务是否在运行。'),
  extraction_lag:I('消息提取滞后','seconds','operations','查看待提取消息与提取服务。'),
  guard_timeout_rate:I('守卫超时率偏高','ratio','operations','查看守卫时延与超时。','最近 5 分钟'),
  guard_latency_p95:I('守卫时延 p95 偏高','ms','operations','查看守卫时延。','最近 5 分钟'),
  host_admission_timeout:I('Agent Host 准入等待超时','count','operations','查看 Agent Host 并发与排队。','最近 5 分钟'),
  pool_waiting:I('连接池有等待','count','operations','查看连接池。','最近 5 分钟'),
  connections_high:I('数据库连接数偏高','count','operations','查看数据库连接（部署上限 60）。'),
  budget_near_limit:I('预算接近上限','ratio','goals','查看预算消耗。'),
  backup_age:I('PG 备份过旧','seconds','operations','查看备份区块。'),
  relay_unenrolled_pending:I('来信无 relay 服务','count','operations','客户来信没有 relay 接收；检查 relay 注册。'),
  relay_backlog_age:I('relay 积压','seconds','operations','查看 relay 与消息待处理时间。'),
  wal_archive_lag:I('WAL 归档滞后','seconds','operations','查看备份区块的 WAL 归档。'),
  wal_archive_failing:I('WAL 归档失败','count','operations','查看备份区块的 WAL 归档。'),
  offsite_backup_age:I('异地备份过旧','seconds','operations','查看备份区块的各仓库最近备份。'),
  file_backup_age:I('文件备份过旧','seconds','operations','查看备份区块。'),
  restore_journal_lag:I('恢复日志导出滞后','seconds','operations','查看备份区块。'),
  restore_drill_overdue:I('恢复演练逾期','seconds','operations','安排恢复演练。'),
  backup_verify_failed:I('备份校验失败','count','operations','查看备份区块。'),
  effect_route_unconfigured:I('Action 没有配置渠道路由','count','actions','为对应 Action 配置渠道路由。','最近 5 分钟'),
  candidates_without_merge_configuration:I('候选定义缺少合并配置','count','knowledge','租户没有生效的合并配置，claim-matcher 空转；按操作单下发合并配置。'),
};

export function valueText(info:AlertInfo|undefined,raw:string|null):string{
  if(raw===null)return '—';
  const n=Number(raw);if(!info||!Number.isFinite(n))return raw;
  if(info.unit==='seconds')return `${ageText(Math.round(n))}（${raw} 秒）`;
  if(info.unit==='ms')return `${raw} 毫秒`;
  if(info.unit==='ratio')return `${(n*100).toFixed(2)}%`;
  return `${raw} 个`;
}
/** The threshold of this alert: from its latest firing event (same rule and selector). */
export function thresholdOf(alert:FiringAlert,events:AlertEvent[]):string|null{
  return events.filter(e=>e.kind==='firing'&&e.rule_id===alert.rule_id&&e.selector===alert.selector&&e.threshold!==null)
    .sort((a,b)=>b.recorded_at.localeCompare(a.recorded_at))[0]?.threshold??null;
}
export function sinceText(from:string,now:number=Date.now()):string{
  const t=new Date(from.replace(' ','T').replace(/(\.\d{3})\d+/,'$1').replace(/([+-]\d{2})$/,'$1:00')).getTime();
  return Number.isNaN(t)?'—':ageText(Math.max(0,Math.round((now-t)/1000)));
}
