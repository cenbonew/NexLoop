/**
 * NX-030 pages: alerts (world real only, D6; owner silences in the action slot), operations status (the SQL metrics snapshot)
 * and the owner's audit. Codes, counts, ages and IDs only; a missing block is labelled, never shown as zero (AT-045).
 */
import type {ReactNode} from 'react';
import {useQuery} from '@tanstack/react-query';
import {BACKUP_FIELDS,BACKUP_LABELS,BACKUP_UNAVAILABLE_TEXT,EVALUATOR_STALE_TEXT,ageText,PROCESS_UNAVAILABLE_TEXT,REFUSAL_TEXT,SEVERITY_TEXT,readAlerts,readHumanActions,readMe,readMetrics,offers,type Backup,type Process} from '../api';
import {Page} from './Page';
import {StatusBadge,Time} from '../ui';
import {Card,CardGrid,EmptyState,IdText,KeyValue,Notice} from '../kit';
import {statusLabel,type Domain} from '../status-labels';
import {when} from './Consumers';
import {DataTable} from '../DataTable';
import {Operations as OpsSection} from './PageOps';
import {ALERT_INFO,sinceText,thresholdOf,valueText} from './alertInfo';
import {routePath} from '../route';
import {readableName} from './Capabilities';

const age=ageText;

export function Alerts({actions}:{actions?:ReactNode}){
  const query=useQuery({queryKey:['workbench','alerts'],queryFn:readAlerts});
  // C6: what the page is, and the silence section only when there is something to silence and the member may (no empty title).
  const member=useQuery({queryKey:['workbench','me'],queryFn:readMe,refetchOnWindowFocus:false});
  return <Page title="告警" query={query} lead="基于真实数据的运行告警：正在触发的告警和最近的告警事件；负责人可以暂时静默某条告警，静默期间仍在评估和记录。">{data=><>
    {data.evaluator.stale?<Notice tone="warning" live>{EVALUATOR_STALE_TEXT}</Notice>
      :<p className="note">只针对真实数据（不含测试与模拟数据）；规则版本 v{data.evaluator.rules_version ?? '—'}，最近评估 <Time value={data.evaluator.last_evaluated_at}/>。</p>}
    <Card title="正在触发" badge={data.firing.length?<StatusBadge text={`${data.firing.length} 条`} tone={data.firing.some(a=>a.severity==='critical'&&!a.silenced_until)?'fail':'limited'}/>:null}>
      <DataTable label="正在触发的告警" empty={{subject:'触发中的告警'}} rows={data.firing} rowKey={a=>a.dedupe_key} rowClass={a=>a.severity==='critical'&&!a.silenced_until?'critical':undefined} columns={[
        {label:'告警',main:true,cell:a=>{const info=ALERT_INFO[a.rule_id];return <><strong>{info?.name??a.rule_id}</strong><code className="tech-name alert-code">{a.rule_id}</code>
          {info?<span className="alert-help">{info.help} <a href={routePath({page:info.page})}>去处理</a></span>:null}</>;}},
        {label:'级别',cell:a=><StatusBadge domain="alert_severity" code={a.severity}/>},{label:'影响对象',cell:a=><span className="nowrap">{a.selector??'全局'}</span>},
        {label:'当前值 / 阈值',cell:a=>{const info=ALERT_INFO[a.rule_id];const t=thresholdOf(a,data.events);
          return <>{valueText(info,a.value)}{t!==null?` / 阈值 ${valueText(info,t)}`:''}{info?.window?<small>（{info.window}）</small>:null}</>;}},
        {label:'持续',cell:a=><span className="nowrap">{sinceText(a.first_fired_at)}（自 <Time value={a.first_fired_at}/>）</span>},{label:'次数',num:true,cell:a=>a.fire_count},
        {label:'静默',cell:a=>a.silenced_until?<>已静默至 <Time value={a.silenced_until}/>（仍在评估和记录）</>:'—'}]}/>
    </Card>
    <Card title="最近事件">
      <DataTable label="最近的告警事件" empty={{subject:'告警事件'}} rows={data.events} rowKey={e=>String(e.event_id)} columns={[
        {label:'时间',cell:e=><Time value={e.recorded_at}/>},{label:'事件',cell:e=><StatusBadge domain="alert_event" code={e.kind}/>},
        {label:'告警',main:true,cell:e=><><span className="name-main">{ALERT_INFO[e.rule_id]?.name??e.rule_id}</span><code className="tech-name">{e.rule_id}</code></>},
        {label:'级别',cell:e=><StatusBadge domain="alert_severity" code={e.severity}/>},{label:'影响对象',cell:e=>e.selector??'全局'},
        {label:'值 / 阈值',cell:e=><>{e.value!==null?valueText(ALERT_INFO[e.rule_id],e.value):'—'}{e.threshold!==null?` / 阈值 ${valueText(ALERT_INFO[e.rule_id],e.threshold)}`:''}</>}]}/>
    </Card>
    {data.firing.length&&offers(member.data,'silence_alert')?<OpsSection actions={actions} title="静默告警"/>:null}
  </>}</Page>;
}

function ProcessBlock({title,section,fields}:{title:string;section:Process;fields:[string,string][]}){
  if(section.status==='unavailable')return <Card title={title}><EmptyState kind="unavailable" subject="样本" reason="最近 5 分钟没有样本" compact/></Card>;
  const rows=Object.entries(section.by_service);
  return <Card title={title}><DataTable label={title} empty={{subject:'进程样本'}} rows={rows} rowKey={([service])=>service} columns={[
    {label:'进程',main:true,cell:([service])=><code className="id-code">{service}</code>},
    ...fields.map(([key,label])=>({label,num:true,cell:([,v]:[string,{max:Record<string,number>;sum:Record<string,number>}])=>v.max[key]??v.sum[key]??'—'}))]}/></Card>;
}

/** NX-035 M12 backup block, read only: ages and counts as recorded; a metric without an observation reads “无” (AT-045). */
export function BackupBlock({backup}:{backup:Backup}){
  if(backup.status==='unavailable')return <Card title="备份"><EmptyState kind="unavailable" subject="备份记录" reason="还没有任何已记录的数据库备份" next="这不代表没有备份。" compact/></Card>;
  const value=(n:number|null)=>n===null?'无':age(n);
  const flag=(n:number|null,bad:string,good:string)=>n===null?'无':<StatusBadge text={n>0?bad:good} tone={n>0?'fail':'ok'}/>;
  const repos=Object.entries(backup.repo_backup_age_seconds);
  const failing=!!(backup.verify_failed||backup.wal_archive_failing);
  return <Card title="备份" badge={failing?<StatusBadge text="需要处理" tone="fail"/>:null}><KeyValue label="备份" items={[
    ...BACKUP_FIELDS.map(f=>({key:BACKUP_LABELS[f],value:value(backup[f])})),
    {key:'最近一次备份校验',value:flag(backup.verify_failed,'失败','通过')},{key:'日志归档',value:flag(backup.wal_archive_failing,'出现失败','正常')},
    {key:'各仓库最近备份',value:repos.length?repos.map(([r,n])=>`${statusLabel('backup_repo',r)} ${age(n)}`).join('；'):'无'}]}/></Card>;
}

/** Counts by code. With `domain` (UI spec §7) each code shows its Chinese label (未知状态 for an unlisted one; the code only in the
 * title) and an empty map is the compact empty state; `labels` is the earlier form, kept until batches 2/3 move to domains. */
export function Counts({values,labels,domain,subject='记录'}:{values:Record<string,number>;labels?:Record<string,string>;domain?:Domain;subject?:string}){
  const entries=Object.entries(values);
  if(domain)return entries.length?<ul className="count-list">{entries.map(([k,n])=><li key={k}><span title={k}>{statusLabel(domain,k)}</span>：<span className="num">{n}</span></li>)}</ul>
    :<EmptyState kind="empty" subject={subject} compact/>;
  return entries.length?<ul>{entries.map(([k,n])=><li key={k}>{labels?.[k]?`${labels[k]}（${k}）`:k}：{n}</li>)}</ul>:<p className="note">无。</p>;
}

export function Operations({actions}:{actions?:ReactNode}){
  const query=useQuery({queryKey:['workbench','metrics'],queryFn:readMetrics});
  return <Page title="运行状态" query={query} actions={actions} lead="系统此刻是否健康：派发拒绝、外部动作、来信必回、承诺异常、队列与后台任务、进程样本、数据库连接与备份。只含计数、时长和比率，不含原文。">{m=><>
    <p className="note">快照时间 <Time value={m.computed_at}/>。</p>
    <CardGrid label="运行状态">
      <Card title="派发拒绝（最近 1 小时）"><Counts values={m.refusals.last_hour} domain="refusal_code" subject="派发拒绝"/></Card>
      <Card title="外部动作" footer={m.effects.unknown.count?<a href={routePath({page:'actions'})}>去 Action 页查询执行结果</a>:null}>
        <p>待核对 {m.effects.unknown.count} 个，最久 {age(m.effects.unknown.oldest_age_seconds)}；超过 24 小时 {m.effects.unknown.over_24h} 个</p>
        <Counts values={m.effects.by_state} domain="intent_state" subject="外部动作"/></Card>
      <Card title="来信必回"><p>待回复 {m.replies.pending.count} 条，最久 {age(m.replies.pending.oldest_age_seconds)}；兜底升级累计 {m.replies.escalations_total} 条</p>
        <Counts values={m.replies.escalations_last_hour} domain="escalation_reason" subject="最近 1 小时的升级"/></Card>
      <Card title="承诺异常" footer={Object.keys(m.commitments.exceptions).length?<a href={routePath({page:'commitments'})}>查看承诺</a>:null}>
        <Counts values={m.commitments.exceptions} domain="commitment_exception" subject="承诺异常"/></Card>
      <Card title="队列">{Object.keys(m.queues).length?<ul>{Object.entries(m.queues).map(([q,v])=><li key={q}>{statusLabel('queue',q)}：待处理 {v.pending??0} 个，重试 {v.retry_wait??0} 个，
        <span className={v.dead_lettered?'warning':undefined}>死信 {v.dead_lettered??0} 个</span>，最久待处理 {age(v.oldest_pending_seconds??null)}</li>)}</ul>:<EmptyState kind="empty" subject="队列任务" compact/>}</Card>
      <Card title="后台任务与提取">{Object.keys(m.feeds).length?<ul>{Object.entries(m.feeds).map(([f,v])=><li key={f}>{statusLabel('feed',f)}：待处理 {v.due??v.pending??0} 个{v.scheduled?`（另有 ${v.scheduled} 个定时未到期）`:''}，
        <span className={v.dead_lettered?'warning':undefined}>死信 {v.dead_lettered??0} 个</span>，最久待处理 {age(v.oldest_pending_seconds??null)}{(v.oldest_pending_seconds??0)>600?<span className="warning">（超过告警阈值 10 分钟）</span>:null}</li>)}</ul>:<EmptyState kind="empty" subject="后台任务积压" compact/>}
        <p>待提取消息 {m.extraction.pending_messages} 条，最久 {age(m.extraction.oldest_pending_seconds)}</p></Card>
      <ProcessBlock title={`守卫时延（2 秒截止）${m.guard.status==='ok'&&m.guard.timeout_rate!==undefined?` · 超时率 ${(m.guard.timeout_rate*100).toFixed(2)}%`:''}`} section={m.guard}
        fields={[['calls','调用'],['timeouts','超时'],['p95_ms','95 分位（毫秒）'],['max_ms','最大（毫秒）']]}/>
      <ProcessBlock title="智能体主机并发" section={m.host} fields={[['active_runs','运行中'],['waiting','排队'],['max_active_runs','上限'],['admission_timeouts','等待超时']]}/>
      <ProcessBlock title="连接池" section={m.pool} fields={[['pool_size','连接'],['requests_waiting','等待'],['requests_errors','错误'],['max_size','上限']]}/>
      <Card title="数据库连接"><p>{m.connections.total} 个（部署上限 60；不按角色细分）</p></Card>
      <BackupBlock backup={m.backup}/>
    </CardGrid>
  </>}</Page>;
}

const ROLE_NAME:Record<string,string>={owner:'负责人',operator:'运营',reviewer:'审核人'};
/** F11: a member as role + stable short alias (the read model carries no display name yet); a long id is an IdText (copy button). */
export function memberAlias(principal:string,role?:string):ReactNode{
  return <span title={principal}>{role?`${ROLE_NAME[role]??role} · `:''}{principal.length>14?<IdText value={principal} head={6} tail={4}/>:principal}</span>;
}

export function Audit({actions}:{actions?:ReactNode}){
  const query=useQuery({queryKey:['workbench','human-actions'],queryFn:readHumanActions});
  return <Page title="审计" query={query} actions={actions} lead="谁在什么时候做了哪些受治理的人类操作，以及成员读取消息原文和客户资料的记录（只有负责人可见；只含 ID、类别和时间）。">{items=>
    <DataTable label="审计" empty={{subject:'审计记录'}} rows={items} rowKey={(_,i)=>String(i)} columns={[
      {label:'时间',main:true,cell:x=><Time value={x.occurred_at}/>},{label:'类别',cell:x=><StatusBadge domain="audit_category" code={x.category}/>},
      {label:'操作',cell:x=><><span className="name-main">{readableName(x.action)}</span><code className="tech-name">{x.action}</code></>},
      {label:'成员',cell:x=><span className="nowrap">{memberAlias(x.principal_id,x.role)}</span>},{label:'对象',wide:true,cell:x=>x.target_ref?<IdText kind={statusLabel('audit_target',x.target_kind)} value={x.target_ref} expandable/>:statusLabel('audit_target',x.target_kind)}]}/>}</Page>;
}
