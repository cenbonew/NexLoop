import type {ReactNode} from 'react';
import {useQuery} from '@tanstack/react-query';
import {readGoals,readPlans} from '../api';
import {Page,Short} from './Page';
import {when} from './Consumers';
import {DataTable} from '../DataTable';

export function Goals({actions}:{actions?:ReactNode}){
  const query=useQuery({queryKey:['workbench','goals'],queryFn:readGoals});
  return <Page title="目标与对齐" query={query} actions={actions}>{data=><>
    <h2>目标</h2>
    {data.goals.length?<DataTable label="目标" rows={data.goals} rowKey={g=>g.goal_id} columns={[
      {label:'目标',main:true,cell:g=>g.objective},{label:'类型',cell:g=>g.goal_kind},{label:'版本',cell:g=>`v${g.current_version}`},{label:'优先级',num:true,cell:g=>g.priority},
      {label:'KR',wide:true,cell:g=>g.key_results.map(k=>`${k.kr_key}: ${k.direction==='at_least'?'≥':'≤'} ${k.target}（${k.metric_id}@${k.metric_version}）`).join('；')||'—'}]}/>:<p className="note">暂无已发布目标。</p>}
    <h2>控制（revision {data.control.revision}）</h2>
    {data.control.scopes.length?<ul>{data.control.scopes.map(s=><li key={s.scope_kind+s.scope_ref}>{s.scope_kind}:{s.scope_ref} — {s.paused?'已暂停':'运行中'}（{s.reason}）</li>)}</ul>:<p className="note">没有暂停或恢复记录。</p>}
    <h2>预算</h2>
    {data.budgets.length?<ul>{data.budgets.map(b=><li key={b.budget_kind}>{b.budget_kind}：已用 {b.consumed} / {b.limit_amount} {b.unit}</li>)}</ul>:<p className="note">未设置预算。</p>}
    <h2>控制事件</h2>
    {data.control.events.length?<ol>{data.control.events.map(e=><li key={e.revision}>#{e.revision} {e.event_kind} {e.scope_kind}:{e.scope_ref} · {when(e.recorded_at)}</li>)}</ol>:<p className="note">暂无控制事件。</p>}
  </>}</Page>;
}

export function Plans({actions,consumerId}:{actions?:ReactNode;consumerId?:string}){
  const query=useQuery({queryKey:['workbench','plans',consumerId??''],queryFn:()=>readPlans(consumerId)});
  return <Page title="计划与运行" query={query} actions={actions} empty={d=>d.length===0}>{data=><DataTable label="计划" rows={data} rowKey={p=>p.plan_id} columns={[
    {label:'计划',main:true,cell:p=><><Short value={p.plan_id}/> v{p.version}</>},{label:'客户',cell:p=><Short value={p.consumer_id}/>},{label:'目标版本',cell:p=>p.goal_version_ref},
    {label:'状态',cell:p=>p.status},{label:'步骤',wide:true,cell:p=>p.steps.map(s=>s.step_key+(s.reassess_at?`（复评 ${when(s.reassess_at)}）`:'')).join('、')||'—'},
    {label:'最近结果',cell:p=>p.outcomes[0]?`${p.outcomes[0].kind} · ${when(p.outcomes[0].recorded_at)}`:'—'}]}/>}</Page>;
}
