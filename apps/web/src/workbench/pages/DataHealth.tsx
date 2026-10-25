/**
 * Data health (D-G5, owner decision as recommended; docs/implementation/eios-capability-gap.md): the health of NexLoop's own data
 * flows, read from the owner's data-health read (0245): claim extraction, the recall index, review reflow, ingestion, Artifacts and
 * erasure propagation. Counts, ratios, ages and reason codes only, never a message, Claim value, recall body or payload. Layout per
 * the workbench UI spec draft (docs/design/workbench-ui-spec.md §3/§7/§8): one card per flow, a status badge in the title row,
 * reasons as Chinese labels (the code in the title), "不可用" distinct from empty.
 */
import type {ReactNode} from 'react';
import {useQuery} from '@tanstack/react-query';
import {ageText,readDataHealth,type DataHealth as Health,type DeadLetterItem,type HealthStatus} from '../api';
import {Page} from './Page';
import {StatusBadge} from '../ui';
import {Card as KitCard} from '../kit';
import {when} from './Consumers';
import {ExtractionDeadLetterForm,WithMember} from '../actions';

export const REASON_TEXT:Record<string,string>={
  dead_letters:'有未处理的提取死信',failed_jobs:'有提取任务失败',backlog_stale:'待提取消息等待超过 1 小时',failure_ratio_high:'24 小时提取失败率超过 20%',
  no_active_profile:'召回索引没有启用的 embedding profile',missing_embeddings:'有条目缺少 embedding，需要回填',profile_drift:'存在旧 profile 的向量（profile 漂移）',
  reflow_stale:'审核决定等待重流超过 1 小时',
  signature_rejections:'有商业事件因签名被拒',rejected_ratio_high:'24 小时商业事件拒收率超过 10%',outbox_backlog:'消息发件箱积压超过 50 条',
  orphans:'有超过 1 天、没有任何引用的 Artifact',retention_passed:'有超过保留期仍可用的 Artifact',stuck_deleting:'有 Artifact 删除中超过 1 小时',
  erasure_stalled:'有客户删除超过 1 天未完成',waiting_unknown_effects:'有客户删除在等待结果未知的外部动作',purge_retrying:'有清理项重试 3 次以上',
};
export const OUTCOME_TEXT:Record<string,string>={verified:'已验证',duplicate:'重复',conflict:'冲突',rejected_signature:'签名被拒',rejected_schema:'格式被拒',
  replay_window:'超出重放窗口',disabled:'连接器已停用',too_large:'过大'};
const JOB_TEXT:Record<string,string>={pending:'待处理',running:'运行中',retry_wait:'等待重试',succeeded:'完成',failed:'失败',dead_lettered:'死信'};
const percent=(v:number|null)=>v===null?'—':`${(v*100).toFixed(1)}%`;

function Badge({s}:{s:HealthStatus}){
  return s.status==='ok'?<StatusBadge text="正常" tone="ok" code="ok"/>:<StatusBadge text={`需关注（${s.reasons.length}）`} tone="limited" code={s.reasons.join(',')}/>;
}
/** One card per flow: title row with its badge, key figures, the reasons in words (code in the title). */
function Card({title,s,children}:{title:string;s:HealthStatus;children:ReactNode}){
  return <div className="health-card" data-status={s.status}><KitCard title={title} label={title} badge={<Badge s={s}/>}>
    <dl className="kv">{children}</dl>
    {s.reasons.length?<ul className="health-reasons">{s.reasons.map(r=><li key={r} title={r}>{REASON_TEXT[r]??'未知状态'}</li>)}</ul>:null}
  </KitCard></div>;
}
const Row=({k,v}:{k:string;v:ReactNode})=><><dt>{k}</dt><dd>{v}</dd></>;
function Counts({values,labels}:{values:Record<string,number>;labels:Record<string,string>}){
  const keys=Object.keys(values).sort();
  return keys.length?<>{keys.map(k=><span key={k} title={k} className="count-item">{labels[k]??'未知状态'} {values[k]}</span>)}</>:<>无记录</>;
}

export function DataHealth(){
  const query=useQuery({queryKey:['workbench','data-health'],queryFn:readDataHealth});
  return <Page title="数据健康" query={query}
    lead="NexLoop 自己数据流的健康（只读）：提取、召回索引、重流、接入、Artifact、删除传播。只有计数、比例、时间和原因，不含消息原文。超过阈值时同时触发告警。">{h=><Body h={h}/>}</Page>;
}

function Body({h}:{h:Health}){
  const {extraction:x,recall:r,reflow:f,ingestion:i,artifacts:a,erasure:e}=h;
  return <>
    <p className="note" role="status">快照时间 {when(h.computed_at)}。{h.attention?<>共 <strong>{h.attention}</strong> 项需关注。</>:'各项正常。'}</p>
    <div className="workbench-grid">
      <Card title="Claim 提取" s={x}>
        <Row k="待入队消息" v={x.feed_pending}/><Row k="最久等待" v={x.feed_pending?ageText(x.oldest_pending_seconds):'—'}/>
        <Row k="未处理死信" v={x.unresolved_dead_letters}/><Row k="死信总数" v={x.dead_lettered_total}/><Row k="24 小时失败率" v={x.finished_24h?`${percent(x.failure_ratio_24h)}（${x.failed_24h}/${x.finished_24h}）`:'—'}/>
        <Row k="队列" v={<Counts values={x.jobs} labels={JOB_TEXT}/>}/>
      </Card>
      {x.dead_letter_items.length?<DeadLetters items={x.dead_letter_items} total={x.unresolved_dead_letters}/>:null}
      <Card title="召回索引与回填" s={r}>
        <Row k="启用的 profile" v={r.active_profile?<code>{r.active_profile}</code>:'未设置'}/><Row k="条目" v={r.entries}/>
        <Row k="缺少 embedding" v={r.missing_embeddings}/><Row k="覆盖率" v={percent(r.coverage_ratio)}/>
        <Row k="旧 profile 向量" v={r.other_profile_vectors}/><Row k="最近回填" v={r.last_backfill_at?when(r.last_backfill_at):'—'}/>
      </Card>
      <Card title="审核重流" s={f}>
        <Row k="待重流" v={f.pending}/><Row k="等待授权" v={f.waiting}/><Row k="最久等待" v={f.pending+f.waiting?ageText(f.oldest_seconds):'—'}/>
      </Card>
      <Card title="接入" s={i}>
        <Row k="发件箱待投递" v={i.outbox_pending}/>
        <Row k="24 小时商业事件" v={<Counts values={i.commercial_receipts_24h} labels={OUTCOME_TEXT}/>}/>
        <Row k="拒收率" v={percent(i.commercial_rejected_ratio_24h)}/><Row k="迟到事件" v={i.commercial_late_24h}/>
        <Row k="最大接收延迟" v={ageText(i.commercial_max_latency_seconds_24h)}/>
      </Card>
      <Card title="Artifact" s={a}>
        <Row k="孤儿（无引用超过 1 天）" v={a.orphans}/><Row k="超过保留期" v={a.retention_passed}/><Row k="删除卡住" v={a.stuck_deleting}/>
      </Card>
      <Card title="删除传播" s={e}>
        <Row k="未完成的删除" v={e.open}/><Row k="最久未完成" v={e.open?ageText(e.oldest_open_seconds):'—'}/>
        <Row k="等待未知结果" v={e.waiting_unknown_effects}/><Row k="重试 3 次以上的清理项" v={e.purge_items_retrying}/><Row k="带保留的完成" v={e.completed_with_holds}/>
      </Card>
    </div>
  </>;
}

/** 0355: the unresolved dead letters (ids and codes only, at most 20) with the owner's two choices; a dead letter fully covered by a
 * later extraction of its conversation is not listed. */
function DeadLetters({items,total}:{items:DeadLetterItem[];total:number}){
  return <div className="health-card dead-letter-list"><KitCard title="未处理的提取死信" label="未处理的提取死信">
    {total>items.length?<p className="note">共 {total} 条，显示最近 {items.length} 条。</p>:null}
    <ul className="dead-letters">{items.map(d=><li key={d.task_id}>
      <dl className="kv"><Row k="任务" v={<code className="id-code">{d.task_id}</code>}/><Row k="原因" v={d.code?<code>{d.code}</code>:'—'}/>
        <Row k="尝试" v={`${d.attempts}/${d.max_attempts}`}/><Row k="消息数" v={d.message_count}/><Row k="已有 Claim" v={d.existing_claims}/><Row k="进入死信" v={when(d.dead_lettered_at)}/></dl>
      <WithMember>{member=><ExtractionDeadLetterForm member={member} taskId={d.task_id} existingClaims={d.existing_claims}/>}</WithMember>
    </li>)}</ul>
  </KitCard></div>;
}
