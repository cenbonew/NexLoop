import {useState,type ReactNode} from 'react';
import {useQuery} from '@tanstack/react-query';
import {INTENT_STATE_TEXT,readActions,type Dispatch} from '../api';
import {Page,Short} from './Page';

/** Dispatch prediction comes from SQL (the same checks as dispatch); without it the page says so instead of guessing. */
export function dispatchText(dispatch:Dispatch):string{
  if(dispatch.status==='unavailable')return '派发预判暂不可用';
  const p=dispatch.prediction;
  if(p.dispatchable)return '可派发';
  return {control_paused:'已暂停',control_revision_stale:'控制已变更，派发时将被拒，等待复评',goal_version_stale:'目标已改版，等待复评',
    object_revision_stale:'对象已变更，等待复评',contact_restricted:'客户联系受限',attached_notification:'附带通知被拒',taken_over:'人工接管中'}[p.reason??'']??('不可派发：'+(p.reason??'原因未知'));
}

export function Actions({actions}:{actions?:ReactNode}){
  const [state,setState]=useState('');
  const query=useQuery({queryKey:['workbench','actions',state],queryFn:()=>readActions(state||undefined)});
  return <Page title="Action / 异常" query={query} actions={actions}>{data=><>
    <label htmlFor="intent-state">状态</label>
    <select id="intent-state" value={state} onChange={e=>setState(e.target.value)}><option value="">全部</option>
      {Object.entries(INTENT_STATE_TEXT).map(([k,v])=><option key={k} value={k}>{v}</option>)}</select>
    <p>待核对 {data.unknown.count} 个{data.unknown.oldest_age_seconds!==null?`，最久 ${Math.round(data.unknown.oldest_age_seconds/60)} 分钟`:''}。默认动作是“查询执行结果”，不是重发。</p>
    {data.intents.length?<table><thead><tr><th>意图</th><th>Action</th><th>状态</th><th>尝试</th><th>最近观察</th><th>派发预判</th></tr></thead><tbody>
      {data.intents.map(i=><tr key={i.intent_id}><td><Short value={i.intent_id}/></td><td>{i.action_name}@{i.action_version}</td>
        <td className={i.state==='unknown'?'pending':undefined}>{INTENT_STATE_TEXT[i.state]??i.state}</td><td>{i.attempts}</td>
        <td>{i.observation?`${i.observation.provider_state} · ${i.observation.observed_at}`:'—'}</td><td>{dispatchText(i.dispatch)}</td></tr>)}
    </tbody></table>:<p className="note">暂无记录。</p>}
  </>}</Page>;
}
