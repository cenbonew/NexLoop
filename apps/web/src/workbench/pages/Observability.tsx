/**
 * NX-030 pages: alerts (world real only, D6; owner silences in the action slot), operations status (the SQL metrics snapshot)
 * and the owner's audit. Codes, counts, ages and IDs only; a missing block is labelled, never shown as zero (AT-045).
 */
import type {ReactNode} from 'react';
import {useQuery} from '@tanstack/react-query';
import {EVALUATOR_STALE_TEXT,PROCESS_UNAVAILABLE_TEXT,REFUSAL_TEXT,SEVERITY_TEXT,readAlerts,readHumanActions,readMetrics,type Process} from '../api';
import {Page} from './Page';

const age=(seconds:number|null)=>seconds===null?'—':seconds<120?`${seconds} 秒`:seconds<7200?`${Math.round(seconds/60)} 分钟`:`${Math.round(seconds/3600)} 小时`;

export function Alerts({actions}:{actions?:ReactNode}){
  const query=useQuery({queryKey:['workbench','alerts'],queryFn:readAlerts});
  return <Page title="告警" query={query} actions={actions}>{data=><>
    {data.evaluator.stale?<p className="warning" role="alert">{EVALUATOR_STALE_TEXT}</p>
      :<p className="note">只针对真实世界（real）的数据告警；规则版本 v{data.evaluator.rules_version ?? '—'}，最近评估 {data.evaluator.last_evaluated_at ?? '—'}。</p>}
    <h2>正在触发</h2>
    {data.firing.length?<table><thead><tr><th>级别</th><th>规则</th><th>对象</th><th>当前值</th><th>首次触发</th><th>次数</th><th>静默</th></tr></thead><tbody>
      {data.firing.map(a=><tr key={a.dedupe_key} className={a.severity==='critical'&&!a.silenced_until?'critical':undefined}>
        <td>{SEVERITY_TEXT[a.severity]}</td><td>{a.rule_id}</td><td>{a.selector??'—'}</td><td>{a.value??'—'}</td><td>{a.first_fired_at}</td><td>{a.fire_count}</td>
        <td>{a.silenced_until?`已静默至 ${a.silenced_until}（仍在评估和记录）`:'—'}</td></tr>)}</tbody></table>
      :<p className="note">当前没有触发中的告警。</p>}
    <h2>最近事件</h2>
    {data.events.length?<ol>{data.events.map(e=><li key={e.event_id}>{e.recorded_at} · {e.kind==='firing'?'触发':'恢复'} · {SEVERITY_TEXT[e.severity]} · {e.rule_id}{e.selector?`（${e.selector}）`:''}
      {e.value!==null?` · 值 ${e.value}`:''}{e.threshold!==null?` / 阈值 ${e.threshold}`:''}</li>)}</ol>:<p className="note">暂无事件。</p>}
  </>}</Page>;
}

function ProcessBlock({title,section,fields}:{title:string;section:Process;fields:[string,string][]}){
  return <article className={'workbench-block workbench-block-'+(section.status==='ok'?'ok':'unavailable')} aria-label={title}><h3>{title}</h3>
    {section.status==='unavailable'?<p className="note" role="status">{PROCESS_UNAVAILABLE_TEXT}</p>:
      <table><thead><tr><th>进程</th>{fields.map(([,label])=><th key={label}>{label}</th>)}</tr></thead><tbody>
        {Object.entries(section.by_service).map(([service,v])=><tr key={service}><td>{service}</td>{fields.map(([key])=><td key={key}>{v.max[key]??v.sum[key]??'—'}</td>)}</tr>)}</tbody></table>}
  </article>;
}

function Counts({values,labels}:{values:Record<string,number>;labels?:Record<string,string>}){
  const entries=Object.entries(values);
  return entries.length?<ul>{entries.map(([k,n])=><li key={k}>{labels?.[k]?`${labels[k]}（${k}）`:k}：{n}</li>)}</ul>:<p className="note">无。</p>;
}

export function Operations({actions}:{actions?:ReactNode}){
  const query=useQuery({queryKey:['workbench','metrics'],queryFn:readMetrics});
  return <Page title="运行状态" query={query} actions={actions}>{m=><>
    <p className="note">快照时间 {m.computed_at}；只含代码、计数、年龄和比率，不含原文。</p>
    <div className="workbench-grid">
      <article className="workbench-block"><h3>派发拒绝（最近 1 小时）</h3><Counts values={m.refusals.last_hour} labels={REFUSAL_TEXT}/></article>
      <article className="workbench-block"><h3>外部动作</h3><p>待核对 {m.effects.unknown.count} 个，最久 {age(m.effects.unknown.oldest_age_seconds)}；超过 24 小时 {m.effects.unknown.over_24h} 个</p>
        <Counts values={m.effects.by_state}/></article>
      <article className="workbench-block"><h3>来信必回</h3><p>待回复 {m.replies.pending.count} 条，最久 {age(m.replies.pending.oldest_age_seconds)}；兜底升级累计 {m.replies.escalations_total} 条</p>
        <Counts values={m.replies.escalations_last_hour}/></article>
      <article className="workbench-block"><h3>承诺异常</h3><Counts values={m.commitments.exceptions}/></article>
      <article className="workbench-block"><h3>队列</h3>{Object.keys(m.queues).length?<ul>{Object.entries(m.queues).map(([q,v])=><li key={q}>{q}：待处理 {v.pending??0}，重试 {v.retry_wait??0}，
        死信 {v.dead_lettered??0}，最久 {age(v.oldest_pending_seconds??null)}</li>)}</ul>:<p className="note">无队列任务。</p>}</article>
      <article className="workbench-block"><h3>后台 feed 与提取</h3>{Object.keys(m.feeds).length?<ul>{Object.entries(m.feeds).map(([f,v])=><li key={f}>{f}：待处理 {v.pending??0}，
        死信 {v.dead_lettered??0}，最久 {age(v.oldest_pending_seconds??null)}</li>)}</ul>:<p className="note">无 feed 积压。</p>}
        <p>待提取消息 {m.extraction.pending_messages} 条，最久 {age(m.extraction.oldest_pending_seconds)}</p></article>
      <ProcessBlock title={`守卫时延（2 秒截止）${m.guard.status==='ok'&&m.guard.timeout_rate!==undefined?` · 超时率 ${(m.guard.timeout_rate*100).toFixed(2)}%`:''}`} section={m.guard}
        fields={[['calls','调用'],['timeouts','超时'],['p95_ms','p95 毫秒'],['max_ms','最大毫秒']]}/>
      <ProcessBlock title="Agent Host 并发" section={m.host} fields={[['active_runs','运行中'],['waiting','排队'],['max_active_runs','上限'],['admission_timeouts','等待超时']]}/>
      <ProcessBlock title="连接池" section={m.pool} fields={[['pool_size','连接'],['requests_waiting','等待'],['requests_errors','错误'],['max_size','上限']]}/>
      <article className="workbench-block"><h3>数据库连接</h3><p>{m.connections.total} 个（部署上限 60；不按角色细分）</p></article>
      <article className="workbench-block workbench-block-unavailable"><h3>备份</h3><p className="note" role="status">{m.backup.status==='unavailable'?'未接入（NX-035 提供备份清单后显示）：不代表没有备份。':m.backup.status}</p></article>
    </div>
  </>}</Page>;
}

export function Audit({actions}:{actions?:ReactNode}){
  const query=useQuery({queryKey:['workbench','human-actions'],queryFn:readHumanActions});
  return <Page title="审计" query={query} actions={actions} empty={d=>d.length===0}>{items=><>
    <p className="note">谁在什么时候做了哪些受治理的人类操作，以及成员读取消息原文和客户资料的记录（只有负责人可见；只含 ID、类别和时间）。</p>
    <table><thead><tr><th>时间</th><th>类别</th><th>操作</th><th>成员</th><th>对象</th></tr></thead><tbody>
      {items.map((x,i)=><tr key={i}><td>{x.occurred_at}</td><td>{x.category==='read'?'读取':'操作'}</td><td>{x.action}</td><td>{x.principal_id}{x.role?`（${x.role}）`:''}</td>
        <td>{x.target_kind}{x.target_ref?` · ${x.target_ref}`:''}</td></tr>)}</tbody></table>
  </>}</Page>;
}
