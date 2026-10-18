import {useState,type ReactNode} from 'react';
import {useQuery} from '@tanstack/react-query';
import {INTENT_STATE_TEXT,readActions,type Dispatch} from '../api';
import {Page,Short} from './Page';
import {when} from './Consumers';
import {DataTable} from '../DataTable';
import {Operations} from './PageOps';

/** Dispatch prediction comes from SQL (the same checks as dispatch); without it the page says so instead of guessing. */
export function dispatchText(dispatch:Dispatch):string{
  if(dispatch.status==='unavailable')return '派发预判暂不可用';
  const p=dispatch.prediction;
  if(p.dispatchable)return '可派发';
  return {control_paused:'已暂停',control_revision_stale:'控制已变更，派发时将被拒，等待复评',goal_version_stale:'目标已改版，等待复评',
    object_revision_stale:'对象已变更，等待复评',contact_restricted:'客户联系受限',attached_notification:'附带通知被拒',taken_over:'人工接管中',not_queued:'不在派发队列（已结束或无需派发）'}[p.reason??'']??('不可派发：'+(p.reason??'原因未知'));
}

export function Actions({actions}:{actions?:ReactNode}){
  const [state,setState]=useState('');
  const query=useQuery({queryKey:['workbench','actions',state],queryFn:()=>readActions(state||undefined)});
  return <Page title="Action / 异常" query={query}>{data=><>
    <p className="workbench-summary" role="status">{data.unknown.count?`待核对 ${data.unknown.count} 个${data.unknown.oldest_age_seconds!==null?`，最久 ${Math.round(data.unknown.oldest_age_seconds/60)} 分钟`:''}：先查询执行结果，不是重发。`:'当前没有待核对的 Action，无需处理。'}</p>
    <label htmlFor="intent-state">状态</label>
    <select id="intent-state" value={state} onChange={e=>setState(e.target.value)}><option value="">全部</option>
      {Object.entries(INTENT_STATE_TEXT).map(([k,v])=><option key={k} value={k}>{v}</option>)}</select>
    {data.intents.length?<DataTable label="Action 意图" rows={data.intents} rowKey={i=>i.intent_id} columns={[
      {label:'Action',main:true,cell:i=>`${i.action_name}@${i.action_version}`},{label:'意图',cell:i=><Short value={i.intent_id}/>},
      {label:'状态',cell:i=><span className={i.state==='unknown'?'pending':undefined}>{INTENT_STATE_TEXT[i.state]??i.state}</span>},{label:'尝试',num:true,cell:i=>i.attempts},
      {label:'最近观察',cell:i=>i.observation?`${i.observation.provider_state} · ${when(i.observation.observed_at)}`:'—'},{label:'派发预判',cell:i=>dispatchText(i.dispatch)}]}/>:<p className="note" role="status">{state?`当前没有“${INTENT_STATE_TEXT[state]??state}”的 Action。`:'暂无记录。'}
      {state?<button type="button" className="link" onClick={()=>setState('')}>查看全部</button>:null}</p>}
    {data.unknown.count?<Operations actions={actions}/>:null}
  </>}</Page>;
}
