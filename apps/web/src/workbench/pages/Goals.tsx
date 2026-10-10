import type {ReactNode} from 'react';
import {useQuery} from '@tanstack/react-query';
import {readGoals,readPlans} from '../api';
import {Page,Short} from './Page';

export function Goals({actions}:{actions?:ReactNode}){
  const query=useQuery({queryKey:['workbench','goals'],queryFn:readGoals});
  return <Page title="目标与对齐" query={query} actions={actions}>{data=><>
    <h2>目标</h2>
    {data.goals.length?<table><thead><tr><th>目标</th><th>类型</th><th>版本</th><th>优先级</th><th>KR</th></tr></thead><tbody>
      {data.goals.map(g=><tr key={g.goal_id}><td>{g.objective}</td><td>{g.goal_kind}</td><td>v{g.current_version}</td><td>{g.priority}</td>
        <td>{g.key_results.map(k=>`${k.kr_key}: ${k.direction==='at_least'?'≥':'≤'} ${k.target}（${k.metric_id}@${k.metric_version}）`).join('；')||'—'}</td></tr>)}
    </tbody></table>:<p className="note">暂无已发布目标。</p>}
    <h2>控制（revision {data.control.revision}）</h2>
    {data.control.scopes.length?<ul>{data.control.scopes.map(s=><li key={s.scope_kind+s.scope_ref}>{s.scope_kind}:{s.scope_ref} — {s.paused?'已暂停':'运行中'}（{s.reason}）</li>)}</ul>:<p className="note">没有暂停或恢复记录。</p>}
    <h2>预算</h2>
    {data.budgets.length?<ul>{data.budgets.map(b=><li key={b.budget_kind}>{b.budget_kind}：已用 {b.consumed} / {b.limit_amount} {b.unit}</li>)}</ul>:<p className="note">未设置预算。</p>}
    <h2>控制事件</h2>
    {data.control.events.length?<ol>{data.control.events.map(e=><li key={e.revision}>#{e.revision} {e.event_kind} {e.scope_kind}:{e.scope_ref} · {e.recorded_at}</li>)}</ol>:<p className="note">暂无控制事件。</p>}
  </>}</Page>;
}

export function Plans({actions,consumerId}:{actions?:ReactNode;consumerId?:string}){
  const query=useQuery({queryKey:['workbench','plans',consumerId??''],queryFn:()=>readPlans(consumerId)});
  return <Page title="计划与运行" query={query} actions={actions} empty={d=>d.length===0}>{data=><table>
    <thead><tr><th>计划</th><th>客户</th><th>目标版本</th><th>状态</th><th>步骤</th><th>最近结果</th></tr></thead>
    <tbody>{data.map(p=><tr key={p.plan_id}><td><Short value={p.plan_id}/> v{p.version}</td><td><Short value={p.consumer_id}/></td><td>{p.goal_version_ref}</td><td>{p.status}</td>
      <td>{p.steps.map(s=>s.step_key+(s.reassess_at?`（复评 ${s.reassess_at}）`:'')).join('、')||'—'}</td><td>{p.outcomes[0]?`${p.outcomes[0].kind} · ${p.outcomes[0].recorded_at}`:'—'}</td></tr>)}</tbody>
  </table>}</Page>;
}
