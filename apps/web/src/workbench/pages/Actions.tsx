import {useState,type ReactNode} from 'react';
import {keepPreviousData,useQuery} from '@tanstack/react-query';
import {INTENT_STATE_TEXT,readActions,readRemediations,type Dispatch,type Remediation} from '../api';
import {RemediationResolveForm,RemediationRuleReleaseForm,RemediationSwitchForm,WithMember} from '../actions';
import {Page} from './Page';
import {StatusBadge,Time} from '../ui';
import {EmptyState,IdText} from '../kit';
import {DataTable} from '../DataTable';
import {Operations} from './PageOps';

/** Dispatch prediction comes from SQL (the same checks as dispatch); without it the page says so instead of guessing. */
export function dispatchText(dispatch:Dispatch,state?:string):string{
  // A finished intent is not dispatched again: say so instead of "不可派发（not_queued）" (walkthrough F13); a failure is its own state.
  if(state==='fulfilled'||state==='confirmed')return '无需再次派发';
  if(state==='failed')return '不适用（已失败，不会自动重发）';
  if(dispatch.status==='unavailable')return '派发预判暂不可用';
  const p=dispatch.prediction;
  if(p.dispatchable)return '可派发';
  return {control_paused:'已暂停',control_revision_stale:'控制已变更，派发时将被拒，等待复评',goal_version_stale:'目标已改版，等待复评',
    object_revision_stale:'对象已变更，等待复评',contact_restricted:'客户联系受限',attached_notification:'附带通知被拒',taken_over:'人工接管中',not_queued:'不在派发队列（已结束或无需派发）'}[p.reason??'']??('不可派发：'+(p.reason??'原因未知'));
}

export function Actions({actions}:{actions?:ReactNode}){
  const [state,setState]=useState('');
  const query=useQuery({queryKey:['workbench','actions',state],queryFn:()=>readActions(state||undefined),placeholderData:keepPreviousData});
  const filter=<div className="list-tools" role="group" aria-label="筛选"><label htmlFor="intent-state">状态<select id="intent-state" value={state} onChange={e=>setState(e.target.value)}>
    <option value="">全部</option>{Object.entries(INTENT_STATE_TEXT).map(([k,v])=><option key={k} value={k}>{v}</option>)}</select></label></div>;
  return <Page title="Action / 异常" query={query} toolbar={filter} lead="对外部系统发起的 Action 意图与它们的执行结果；结果未知时先查询，不重发。"
    announce={d=>`${state?INTENT_STATE_TEXT[state]??state:'全部'}：${d.intents.length} 条`}>{data=><>
    <p className="workbench-summary" role="status">{data.unknown.count?`待核对 ${data.unknown.count} 个${data.unknown.oldest_age_seconds!==null?`，最久 ${Math.round(data.unknown.oldest_age_seconds/60)} 分钟`:''}：先查询执行结果，不是重发。`:'当前没有待核对的 Action，无需处理。'}</p>
    {data.intents.length?<DataTable label="Action 意图" rows={data.intents} rowKey={i=>i.intent_id} columns={[
      {label:'Action',main:true,cell:i=><><code className="id-code">{i.action_name}</code> <span className="kv-hint">v{i.action_version}</span></>},{label:'意图',cell:i=><IdText kind="意图" value={i.intent_id}/>},
      {label:'状态',cell:i=><StatusBadge domain="intent_state" code={i.state}/>},{label:'尝试',num:true,cell:i=>i.attempts},
      {label:'最近观察',cell:i=>i.observation?<><StatusBadge domain="provider_state" code={i.observation.provider_state}/> <Time value={i.observation.observed_at}/></>:'—'},{label:'派发预判',cell:i=>dispatchText(i.dispatch,i.state)}]}/>
      :state?<EmptyState kind="filtered" subject={`“${INTENT_STATE_TEXT[state]??state}”的 Action`} next={<button type="button" className="link" onClick={()=>setState('')}>查看全部</button>}/>
      :<EmptyState kind="empty" subject="Action 意图"/>}
    {data.unknown.count?<Operations actions={actions}/>:null}
    <Remediations/>
  </>}</Page>;
}

/** NX-057 R1: remediation intents of final failures (suggestions only). Read for every member; resolving and the switch are the
 * owner's (offered only when the role carries them; the governed entry still decides). */
export function Remediations(){
  const query=useQuery({queryKey:['workbench','remediations'],queryFn:()=>readRemediations()});
  if(query.isPending)return <section aria-label="补救"><h2>补救</h2><p role="status">正在读取…</p></section>;
  if(query.isError)return <section aria-label="补救"><h2>补救</h2><p className="note" role="status">补救暂不可读。</p></section>;
  const data=query.data;const open=data.remediations.filter(r=>r.state==='proposed'||r.state==='escalated');
  return <section aria-label="补救"><h2>补救</h2>
    <p className="workbench-summary" role="status">{data.paused?'补救已暂停：新的最终失败不会生成补救。':open.length?`待处理 ${open.length} 条：最终失败的 Action 需要人跟进。`:'当前没有待处理的补救。'}
      {data.hidden?` 另有 ${data.hidden} 条因客户删除中未显示。`:''}{data.rate_capped.length?` 最近 24 小时有 ${data.rate_capped.reduce((n,x)=>n+x.over_count,0)} 次失败超过速率上限，已汇总升级。`:''}</p>
    {data.remediations.length?<DataTable label="补救" rows={data.remediations} rowKey={(r:Remediation)=>r.remediation_id} columns={[
      {label:'源 Action',main:true,cell:(r:Remediation)=><><code className="id-code">{r.source_action}</code> <span className="kv-hint">v{r.source_action_version}</span></>},
      {label:'失败原因',cell:(r:Remediation)=><StatusBadge domain="failure_reason" code={r.failure_kind}/>},{label:'失败时间',cell:(r:Remediation)=><Time value={r.failed_at}/>},
      {label:'方式',cell:(r:Remediation)=><StatusBadge domain="remediation_kind" code={r.kind}/>},{label:'状态',cell:(r:Remediation)=><StatusBadge domain="remediation_state" code={r.state}/>},
      {label:'客户',cell:(r:Remediation)=>r.consumer_id?<IdText kind="客户" value={r.consumer_id}/>:'—'},{label:'期限',cell:(r:Remediation)=><Time value={r.due_by}/>},
      {label:'规则',cell:(r:Remediation)=><><code className="id-code">{r.rule_id}</code> <span className="kv-hint">v{r.rule_version}</span></>}]}/>
      :<EmptyState kind="empty" subject="补救"/>}
    {data.held_rules.length?<p className="note" role="status">熔断中的规则：{data.held_rules.map(h=><span key={h.rule_id}><code className="id-code">{h.rule_id}</code> <StatusBadge domain="remediation_hold" code={h.reason_code}/> </span>)}</p>:null}
    <WithMember>{member=><>{open.map(r=><RemediationResolveForm key={r.remediation_id} member={member} remediationId={r.remediation_id} kind={r.kind}/>)}
      {data.held_rules.map(h=><RemediationRuleReleaseForm key={h.rule_id} member={member} ruleId={h.rule_id}/>)}
      <RemediationSwitchForm member={member} paused={data.paused}/></>}</WithMember>
  </section>;
}
