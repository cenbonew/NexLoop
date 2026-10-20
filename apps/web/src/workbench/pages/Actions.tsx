import {useState,type ReactNode} from 'react';
import {keepPreviousData,useQuery} from '@tanstack/react-query';
import {INTENT_STATE_TEXT,readActions,type Dispatch} from '../api';
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
  </>}</Page>;
}
