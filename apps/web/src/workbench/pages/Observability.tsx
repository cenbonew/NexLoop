/**
 * NX-030 pages: alerts (world real only, D6; owner silences in the action slot), operations status (the SQL metrics snapshot)
 * and the owner's audit. Codes, counts, ages and IDs only; a missing block is labelled, never shown as zero (AT-045).
 */
import type {ReactNode} from 'react';
import {useQuery} from '@tanstack/react-query';
import {BACKUP_FIELDS,BACKUP_LABELS,BACKUP_UNAVAILABLE_TEXT,EVALUATOR_STALE_TEXT,ageText,PROCESS_UNAVAILABLE_TEXT,REFUSAL_TEXT,SEVERITY_TEXT,readAlerts,readHumanActions,readMetrics,type Backup,type Process} from '../api';
import {Page} from './Page';
import {when} from './Consumers';
import {DataTable} from '../DataTable';
import {IdRef,NoWrap,Operations as OpsSection} from './PageOps';
import {ALERT_INFO,sinceText,thresholdOf,valueText} from './alertInfo';
import {routePath} from '../route';

const age=ageText;

export function Alerts({actions}:{actions?:ReactNode}){
  const query=useQuery({queryKey:['workbench','alerts'],queryFn:readAlerts});
  return <Page title="告警" query={query}>{data=><>
    {data.evaluator.stale?<p className="warning" role="alert">{EVALUATOR_STALE_TEXT}</p>
      :<p className="note">只针对真实世界（real）的数据告警；规则版本 v{data.evaluator.rules_version ?? '—'}，最近评估 {when(data.evaluator.last_evaluated_at)}。</p>}
    <h2>正在触发</h2>
    {data.firing.length?<DataTable label="正在触发的告警" rows={data.firing} rowKey={a=>a.dedupe_key} rowClass={a=>a.severity==='critical'&&!a.silenced_until?'critical':undefined} columns={[
      {label:'告警',main:true,cell:a=>{const info=ALERT_INFO[a.rule_id];return <><strong>{info?.name??a.rule_id}</strong> <small className="alert-code"><code>{a.rule_id}</code></small>
        {info?<span className="alert-help"> · {info.help} <a href={routePath({page:info.page})}>去处理</a></span>:null}</>;}},
      {label:'级别',cell:a=><NoWrap>{SEVERITY_TEXT[a.severity]}</NoWrap>},{label:'影响对象',cell:a=><NoWrap>{a.selector??'全局'}</NoWrap>},
      {label:'当前值 / 阈值',cell:a=>{const info=ALERT_INFO[a.rule_id];const t=thresholdOf(a,data.events);
        return <>{valueText(info,a.value)}{t!==null?` / 阈值 ${valueText(info,t)}`:''}{info?.window?<small>（{info.window}）</small>:null}</>;}},
      {label:'持续',cell:a=><NoWrap>{sinceText(a.first_fired_at)}（自 {when(a.first_fired_at)}）</NoWrap>},{label:'次数',num:true,cell:a=>a.fire_count},
      {label:'静默',cell:a=>a.silenced_until?`已静默至 ${when(a.silenced_until)}（仍在评估和记录）`:'—'}]}/>
      :<p className="note">当前没有触发中的告警。</p>}
    <h2>最近事件</h2>
    {data.events.length?<ol>{data.events.map(e=><li key={e.event_id}>{when(e.recorded_at)} · {e.kind==='firing'?'触发':'恢复'} · {SEVERITY_TEXT[e.severity]} · {ALERT_INFO[e.rule_id]?.name??e.rule_id}（{e.rule_id}）{e.selector?` · ${e.selector}`:''}
      {e.value!==null?` · 值 ${valueText(ALERT_INFO[e.rule_id],e.value)}`:''}{e.threshold!==null?` / 阈值 ${valueText(ALERT_INFO[e.rule_id],e.threshold)}`:''}</li>)}</ol>:<p className="note">暂无事件。</p>}
    <OpsSection actions={actions} title="静默（可选）"/>
  </>}</Page>;
}

function ProcessBlock({title,section,fields}:{title:string;section:Process;fields:[string,string][]}){
  return <article className={'workbench-block workbench-block-'+(section.status==='ok'?'ok':'unavailable')} aria-label={title}><h3>{title}</h3>
    {section.status==='unavailable'?<p className="note" role="status">{PROCESS_UNAVAILABLE_TEXT}</p>:
      <table><thead><tr><th>进程</th>{fields.map(([,label])=><th key={label}>{label}</th>)}</tr></thead><tbody>
        {Object.entries(section.by_service).map(([service,v])=><tr key={service}><td>{service}</td>{fields.map(([key])=><td key={key}>{v.max[key]??v.sum[key]??'—'}</td>)}</tr>)}</tbody></table>}
  </article>;
}

/** NX-035 M12 backup block, read only: ages and counts as recorded; a metric without an observation reads “无” (AT-045). */
function BackupBlock({backup}:{backup:Backup}){
  if(backup.status==='unavailable')return <article className="workbench-block workbench-block-unavailable" aria-label="备份"><h3>备份</h3><p className="note" role="status">{BACKUP_UNAVAILABLE_TEXT}</p></article>;
  const value=(n:number|null)=>n===null?'无':age(n);
  const flag=(n:number|null,bad:string,good:string)=>n===null?'无':n>0?bad:good;
  const repos=Object.entries(backup.repo_backup_age_seconds);
  return <article className={'workbench-block workbench-block-'+(backup.verify_failed||backup.wal_archive_failing?'unavailable':'ok')} aria-label="备份"><h3>备份</h3>
    <table><tbody>{BACKUP_FIELDS.map(f=><tr key={f}><th>{BACKUP_LABELS[f]}</th><td>{value(backup[f])}</td></tr>)}
      <tr><th>最近一次备份校验</th><td>{flag(backup.verify_failed,'失败','通过')}</td></tr>
      <tr><th>WAL 归档</th><td>{flag(backup.wal_archive_failing,'出现失败','正常')}</td></tr>
      <tr><th>各仓库最近备份</th><td>{repos.length?repos.map(([r,n])=>`${r} ${age(n)}`).join('；'):'无'}</td></tr></tbody></table>
  </article>;
}

function Counts({values,labels}:{values:Record<string,number>;labels?:Record<string,string>}){
  const entries=Object.entries(values);
  return entries.length?<ul>{entries.map(([k,n])=><li key={k}>{labels?.[k]?`${labels[k]}（${k}）`:k}：{n}</li>)}</ul>:<p className="note">无。</p>;
}

export function Operations({actions}:{actions?:ReactNode}){
  const query=useQuery({queryKey:['workbench','metrics'],queryFn:readMetrics});
  return <Page title="运行状态" query={query} actions={actions}>{m=><>
    <p className="note">快照时间 {when(m.computed_at)}；只含代码、计数、年龄和比率，不含原文。</p>
    <div className="workbench-grid">
      <article className="workbench-block"><h3>派发拒绝（最近 1 小时）</h3><Counts values={m.refusals.last_hour} labels={REFUSAL_TEXT}/></article>
      <article className="workbench-block"><h3>外部动作</h3>{m.effects.unknown.count?<p><a href={routePath({page:'actions'})}>去 Action 页查询执行结果</a></p>:null}<p>待核对 {m.effects.unknown.count} 个，最久 {age(m.effects.unknown.oldest_age_seconds)}；超过 24 小时 {m.effects.unknown.over_24h} 个</p>
        <Counts values={m.effects.by_state}/></article>
      <article className="workbench-block"><h3>来信必回</h3><p>待回复 {m.replies.pending.count} 条，最久 {age(m.replies.pending.oldest_age_seconds)}；兜底升级累计 {m.replies.escalations_total} 条</p>
        <Counts values={m.replies.escalations_last_hour}/></article>
      <article className="workbench-block"><h3>承诺异常</h3>{Object.keys(m.commitments.exceptions).length?<p><a href={routePath({page:'commitments'})}>查看承诺</a></p>:null}<Counts values={m.commitments.exceptions}/></article>
      <article className="workbench-block"><h3>队列</h3>{Object.keys(m.queues).length?<ul>{Object.entries(m.queues).map(([q,v])=><li key={q}>{q}：待处理 {v.pending??0} 个，重试 {v.retry_wait??0} 个，
        <span className={v.dead_lettered?'warning':undefined}>死信 {v.dead_lettered??0} 个</span>，最久待处理 {age(v.oldest_pending_seconds??null)}</li>)}</ul>:<p className="note">无队列任务。</p>}</article>
      <article className="workbench-block"><h3>后台 feed 与提取</h3>{Object.keys(m.feeds).length?<ul>{Object.entries(m.feeds).map(([f,v])=><li key={f}>{f}：待处理 {v.pending??0} 个，
        <span className={v.dead_lettered?'warning':undefined}>死信 {v.dead_lettered??0} 个</span>，最久待处理 {age(v.oldest_pending_seconds??null)}{(v.oldest_pending_seconds??0)>600?<span className="warning">（超过告警阈值 10 分钟）</span>:null}</li>)}</ul>:<p className="note">无 feed 积压。</p>}
        <p>待提取消息 {m.extraction.pending_messages} 条，最久 {age(m.extraction.oldest_pending_seconds)}</p></article>
      <ProcessBlock title={`守卫时延（2 秒截止）${m.guard.status==='ok'&&m.guard.timeout_rate!==undefined?` · 超时率 ${(m.guard.timeout_rate*100).toFixed(2)}%`:''}`} section={m.guard}
        fields={[['calls','调用'],['timeouts','超时'],['p95_ms','p95 毫秒'],['max_ms','最大毫秒']]}/>
      <ProcessBlock title="Agent Host 并发" section={m.host} fields={[['active_runs','运行中'],['waiting','排队'],['max_active_runs','上限'],['admission_timeouts','等待超时']]}/>
      <ProcessBlock title="连接池" section={m.pool} fields={[['pool_size','连接'],['requests_waiting','等待'],['requests_errors','错误'],['max_size','上限']]}/>
      <article className="workbench-block"><h3>数据库连接</h3><p>{m.connections.total} 个（部署上限 60；不按角色细分）</p></article>
      <BackupBlock backup={m.backup}/>
    </div>
  </>}</Page>;
}

const ROLE_NAME:Record<string,string>={owner:'负责人',operator:'运营',reviewer:'审核人'};
/** F11: a member as role + stable short alias (the read model carries no display name yet); the full id is in the title. */
export function memberAlias(principal:string,role?:string):ReactNode{
  return <span title={principal}>{role?`${ROLE_NAME[role]??role} · `:''}{principal.length>14?principal.slice(0,6)+'…'+principal.slice(-4):principal}</span>;
}

export function Audit({actions}:{actions?:ReactNode}){
  const query=useQuery({queryKey:['workbench','human-actions'],queryFn:readHumanActions});
  return <Page title="审计" query={query} actions={actions} empty={d=>d.length===0}>{items=><>
    <p className="note">谁在什么时候做了哪些受治理的人类操作，以及成员读取消息原文和客户资料的记录（只有负责人可见；只含 ID、类别和时间）。</p>
    <DataTable label="审计" rows={items} rowKey={(_,i)=>String(i)} columns={[
      {label:'时间',main:true,cell:x=><NoWrap>{when(x.occurred_at)}</NoWrap>},{label:'类别',cell:x=><NoWrap>{x.category==='read'?'读取':'操作'}</NoWrap>},{label:'操作',cell:x=><NoWrap>{x.action}</NoWrap>},
      {label:'成员',cell:x=><NoWrap>{memberAlias(x.principal_id,x.role)}</NoWrap>},{label:'对象',wide:true,cell:x=>x.target_ref?<IdRef kind={x.target_kind} value={x.target_ref}/>:x.target_kind}]}/>
  </>}</Page>;
}
