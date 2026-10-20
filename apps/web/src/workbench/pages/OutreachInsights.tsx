/**
 * Outreach slice 8 (docs/implementation/outreach-impl-plan.md; design §5, P7): the owner's outreach metrics and the evidence chain of
 * each proactive send, under 运营人设 › 外联指标与证据. Read only; counts, ratios, ids, digests and codes, never a message body.
 * Every status code is shown by its Chinese label (labels.ts and the maps below, from docs/design/workbench-glossary.md §2.4), the
 * raw code only in the title. 演示（合成客户）lists the sends of synthetic / test personas only: the owner's demonstration view on
 * synthetic data; such sends never reach a provider (persona gate) and real dispatch stays as configured.
 */
import {useState,type ReactNode} from 'react';
import {useQuery} from '@tanstack/react-query';
import {kindOf,readOutreachEvidence,readOutreachEvidenceList,readOutreachMetrics,STATE_TEXT,type OutreachEvidence,type OutreachMetrics} from '../api';
import {DataTable} from '../DataTable';
import {StatusBadge,Tabs,Time,type Tone} from '../ui';
import {Card,EmptyState} from '../kit';
import {labelOf,OUTREACH_GATE,OUTREACH_GATE_RESULT,OUTREACH_PURPOSE,type LabelMap} from '../labels';

export const SEND_STATE:LabelMap={pending_approval:'待审批',accepted:'已受理',dispatching:'派发中',unknown:'结果未知',fulfilled:'已送达（回环）',failed:'失败',refused:'已拒绝'};
export const REFUSAL:LabelMap={contact_restricted:'客户联系受限',control_paused:'已暂停',control_revision_stale:'控制已变更',object_revision_stale:'对象已变更',
  requester_unauthorized:'发起人已无权限',consumer_erasing:'客户正在删除',purpose_not_allowed:'目的不在清单',purpose_disabled:'目的已停用',purpose_changed:'目的已变更',
  approval_required:'需要审批',approval_rejected:'审批被拒',approval_expired:'审批超时',marketing_disabled:'营销未开启',marketing_consent_missing:'没有营销同意',
  marketing_opted_out:'客户已退订营销',persona_excluded:'非真实客户',quiet_hours:'勿扰时段',frequency_capped:'超过联系频次',preference_quiet_hours:'客户勿扰时段',
  preference_frequency:'超过客户频次',preference_channel:'客户拒绝该渠道',preference_topic:'客户拒绝该主题',real_dispatch_closed:'真实派发未开启',
  budget_exhausted:'预算用尽',budget_unconfigured:'未配置预算'};
const PURPOSE:LabelMap={...OUTREACH_PURPOSE,marketing_general:'营销',renewal_reminder:'续费提醒'};
const ORIGIN:LabelMap={commitment:'承诺',intent:'服务回执',plan:'计划复评',owner:'负责人发起'};
const INITIATOR:LabelMap={human:'负责人',agent:'复评 Run'};
const DECISION:LabelMap={pending:'待审批',approved:'已批准',rejected:'已拒绝',expired:'已超时'};
const PERSONA:LabelMap={real:'真实客户',synthetic:'合成客户',simulated_agent:'模拟客户'};
const OPTOUT:LabelMap={unsubscribed:'退订营销',resumed:'恢复营销'};
const CONSENT:LabelMap={granted:'同意',withdrawn:'撤回同意'};
const SOURCE:LabelMap={unsubscribe_rule:'退订词表',refusal_rule:'拒绝规则',owner:'负责人',consumer_message:'客户来信',offline:'线下证据'};
const EVENT:LabelMap={'nexloop.outreach.send.accepted':'受理','nexloop.outreach.send.refused':'拒绝','nexloop.outreach.send.fulfilled':'送达',
  'nexloop.outreach.send.failed':'失败','nexloop.outreach.send.unknown':'结果未知'};
const STAGE:LabelMap={refused:'在派发时被拒',admitted:'派发时全部通过',not_yet:'尚未派发'};
const toneOf=(state:string):Tone=>state==='fulfilled'?'ok':state==='refused'||state==='failed'?'fail':state==='pending_approval'||state==='accepted'||state==='dispatching'||state==='unknown'?'pending':'neutral';

/** A code as its Chinese label, the raw code in the title (unknown codes: 未知状态). */
export function L({map,value}:{map:LabelMap;value:string|null|undefined}){const l=labelOf(map,value);return <span title={l.code}>{l.text}</span>;}
const pct=(v:number|null)=>v===null?'—':`${(v*100).toFixed(1)}%`;
const Row=({k,v}:{k:string;v:ReactNode})=><><dt>{k}</dt><dd>{v}</dd></>;
function CodeCounts({values,map}:{values:Record<string,number>;map:LabelMap}){
  const keys=Object.keys(values).sort();
  return keys.length?<ul className="plain">{keys.map(k=><li key={k}><L map={map} value={k}/>：{values[k]}</li>)}</ul>:<EmptyState kind="empty" subject="近 7 天的记录" compact/>;
}

export function OutreachInsights(){
  const [view,setView]=useState<'metrics'|'evidence'|'demo'>('metrics');
  return <>
    <Tabs label="外联指标与证据" value={view} onChange={setView} tabs={[{key:'metrics',label:'指标'},{key:'evidence',label:'证据链'},{key:'demo',label:'演示（合成客户）'}]}/>
    {view==='metrics'?<Metrics/>:<Evidence syntheticOnly={view==='demo'}/>}
  </>;
}

function Metrics(){
  const query=useQuery({queryKey:['workbench','outreach-metrics'],queryFn:readOutreachMetrics});
  if(query.isPending)return <EmptyState kind="loading" subject="外联数据"/>;
  if(query.isError)return <EmptyState kind={kindOf(query.error)==='forbidden'?'forbidden':'unavailable'} subject="外联数据" onRetry={()=>void query.refetch()}/>;
  const m:OutreachMetrics=query.data;
  return <>
    <p className="note">近 {m.window_days} 天，快照时间 <Time value={m.computed_at}/>。只有计数、比例和原因，不含消息正文。</p>
    <div className="workbench-grid">
      <Card title="外发漏斗"><dl className="kv">
        <Row k="当前候选" v={m.candidates}/><Row k="受理" v={m.accepted_7d}/><Row k="待审批" v={m.pending_approval}/>
        <Row k="已派发" v={m.sent_7d}/><Row k="已送达（回环）" v={m.fulfilled_7d}/></dl></Card>
      <Card title="审批"><CodeCounts values={m.approvals_7d} map={DECISION}/></Card>
      <Card title="按闸门的拒绝"><CodeCounts values={m.refused_7d} map={REFUSAL}/>
        <p className="note">其中超过联系频次 {m.frequency_capped_7d} 次。</p></Card>
      <Card title="营销与退订"><dl className="kv">
        <Row k="营销" v={m.marketing.enabled?<StatusBadge text="已开启" tone="ok" code="enabled"/>:<StatusBadge text="未开启" tone="neutral" code="disabled"/>}/>
        <Row k="同意模式" v={<L map={{opt_out:'默认可发',opt_in:'需明确同意'}} value={m.marketing.consent_mode}/>}/>
        <Row k="营销发送" v={m.marketing.sent_7d}/><Row k="退订" v={m.marketing.unsubscribes_7d}/>
        <Row k="退订率" v={m.marketing.unsubscribe_rate_7d===null?<span title="样本不足">不足 {m.marketing.min_volume} 条营销发送，不计算</span>:pct(m.marketing.unsubscribe_rate_7d)}/>
        <Row k="营销额度（每客户 7 天）" v={m.marketing.allowance_per_7d}/><Row k="已用满额度的客户" v={m.marketing.consumers_at_allowance}/></dl></Card>
      <Card title="营销同意"><dl className="kv">
        <Row k="新增同意" v={m.consent.granted_7d}/><Row k="撤回" v={m.consent.withdrawn_7d}/>
        <Row k="当前同意的客户" v={`${m.consent.consumers_consented} / ${m.consent.consumers_with_record}`}/><Row k="同意率" v={pct(m.consent.consent_rate)}/></dl></Card>
      <Card title="按目的">{Object.keys(m.by_purpose_7d).length?<DataTable label="按目的" rows={Object.entries(m.by_purpose_7d)} rowKey={([p])=>p} columns={[
        {label:'目的',main:true,cell:([p])=><L map={PURPOSE} value={p}/>},{label:'受理',num:true,cell:([,x])=>x.accepted},{label:'派发',num:true,cell:([,x])=>x.sent},
        {label:'拒绝',num:true,cell:([,x])=>x.refused}]}/>:<EmptyState kind="empty" subject="近 7 天的外发" compact/>}</Card>
    </div>
  </>;
}

function Evidence({syntheticOnly}:{syntheticOnly:boolean}){
  const [open,setOpen]=useState<string|null>(null);
  const query=useQuery({queryKey:['workbench','outreach-evidence',syntheticOnly],queryFn:()=>readOutreachEvidenceList(syntheticOnly)});
  if(query.isPending)return <EmptyState kind="loading" subject="外联数据"/>;
  if(query.isError)return <EmptyState kind={kindOf(query.error)==='forbidden'?'forbidden':'unavailable'} subject="外联数据" onRetry={()=>void query.refetch()}/>;
  const items=query.data.items;
  return <>
    {syntheticOnly?<p className="note" data-demo>演示视图：只列合成或测试客户的外发，便于在合成数据上演示完整链路。合成客户在派发时一律被拒（非真实客户），不会触达任何渠道；真实派发开关不受影响。</p>:null}
    {items.length?<DataTable label="外发证据" rows={items} rowKey={i=>i.intent_id} columns={[
      {label:'受理时间',cell:i=><Time value={i.requested_at}/>},{label:'目的',main:true,cell:i=><L map={PURPOSE} value={i.purpose_id}/>},
      {label:'触发',cell:i=><L map={ORIGIN} value={i.origin}/>},{label:'发起',cell:i=><L map={INITIATOR} value={i.initiator}/>},
      {label:'客户',cell:i=><L map={PERSONA} value={i.persona}/>},
      {label:'状态',cell:i=>{const l=labelOf(SEND_STATE,i.state);return <StatusBadge text={l.text} tone={toneOf(i.state)} code={i.state}/>;}},
      {label:'拒绝原因',cell:i=>i.refusal_code?<L map={REFUSAL} value={i.refusal_code}/>:'—'},
      {label:'',cell:i=><button type="button" onClick={()=>setOpen(open===i.intent_id?null:i.intent_id)} aria-expanded={open===i.intent_id}>{open===i.intent_id?'收起':'证据链'}</button>}]}/>
      :<EmptyState kind="empty" subject={syntheticOnly?'合成客户的外发':'外发'}/>}
    {open?<Chain intentId={open}/>:null}
  </>;
}

export function Chain({intentId}:{intentId:string}){
  const query=useQuery({queryKey:['workbench','outreach-evidence',intentId],queryFn:()=>readOutreachEvidence(intentId)});
  if(query.isPending)return <EmptyState kind="loading" subject="外联数据"/>;
  if(query.isError)return <EmptyState kind={kindOf(query.error)==='forbidden'?'forbidden':'unavailable'} subject="外联数据" onRetry={()=>void query.refetch()}/>;
  return <ChainBody e={query.data}/>;
}

export function ChainBody({e}:{e:OutreachEvidence}){
  if(!e.found)return <EmptyState kind="empty" subject="匹配的外发" next="找不到这条外发：意图 ID 不存在，或不属于本租户。"/>;
  const {trigger:t,request:q,approval:a,gates:g,dispatch:d,responses:r}=e;
  return <Card title="证据链">
    <ol className="evidence-chain">
      <li><h3>触发</h3><dl className="kv"><Row k="来源" v={<L map={ORIGIN} value={t.origin}/>}/><Row k="引用" v={<code>{t.origin_ref}</code>}/>
        <Row k="发起" v={<L map={INITIATOR} value={t.initiator}/>}/>{t.run_id?<Row k="Run" v={<code>{t.run_id}</code>}/>:null}</dl></li>
      <li><h3>受理的请求</h3><dl className="kv"><Row k="目的" v={<><L map={PURPOSE} value={q.purpose_id}/>（清单 v{q.purpose_version}）</>}/>
        <Row k="客户" v={<L map={PERSONA} value={q.persona}/>}/><Row k="正文" v={<span title={q.body_digest}>{q.body_chars} 字（只存摘要可查）</span>}/>
        <Row k="受理时间" v={<Time value={q.requested_at}/>}/></dl></li>
      <li><h3>审批</h3>{a?<dl className="kv"><Row k="结论" v={<L map={DECISION} value={a.decision??'pending'}/>}/><Row k="申请" v={<Time value={a.requested_at}/>}/>
        <Row k="决定" v={a.decided_at?<Time value={a.decided_at}/>:'—'}/></dl>:<p>这个目的不需要审批。</p>}</li>
      <li><h3>派发闸门</h3><p><L map={STAGE} value={g.stage}/>{g.code?<>：<L map={REFUSAL} value={g.code}/></>:null}</p>
        {Array.isArray(g.gates)&&g.gates.length?<ul className="plain">{g.gates.map(x=><li key={x.gate}><L map={OUTREACH_GATE} value={x.gate}/>：
          <StatusBadge text={labelOf(OUTREACH_GATE_RESULT,x.result).text} tone={x.result==='pass'?'ok':x.result==='refuse'?'fail':'neutral'} code={x.result}/></li>)}</ul>:null}</li>
      <li><h3>派发结果</h3><StatusBadge text={labelOf(SEND_STATE,d.state).text} tone={toneOf(d.state)} code={d.state}/>{' '}只有回环 provider，未触达真实渠道。</li>
      <li><h3>过程记录</h3>{e.events.length?<ul className="plain">{e.events.map(x=><li key={x.action+x.at}><Time value={x.at}/> <L map={EVENT} value={x.action}/></li>)}</ul>:<p>暂无记录。</p>}</li>
      <li><h3>客户的回应</h3>
        {r.optouts.length||r.consents.length?<ul className="plain">
          {r.optouts.map(o=><li key={'o'+o.at+o.kind}><Time value={o.at}/> <L map={OPTOUT} value={o.kind}/>（<L map={SOURCE} value={o.source}/>）{o.answers_this_send?'，回应的就是这条':''}</li>)}
          {r.consents.map(k=><li key={'c'+k.at+k.kind}><Time value={k.at}/> <L map={CONSENT} value={k.kind}/>（<L map={SOURCE} value={k.source}/>）</li>)}</ul>:<p>没有退订或同意记录。</p>}
        {r.contact_restricted?<StatusBadge text="客户联系受限" tone="fail" code="contact_restricted"/>:null}</li>
    </ol>
  </Card>;
}
