import type {ReactNode} from 'react';
import {useQuery} from '@tanstack/react-query';
import {readGoals,readPlans} from '../api';
import {Page} from './Page';
import {Money,StatusBadge,Time} from '../ui';
import {EmptyState,IdText,ScopeText} from '../kit';
import {routePath} from '../route';
import {statusLabel} from '../status-labels';
import {when} from './Consumers';
import {DataTable} from '../DataTable';
import {Operations} from './PageOps';

export function Goals({actions}:{actions?:ReactNode}){
  const query=useQuery({queryKey:['workbench','goals'],queryFn:readGoals});
  return <Page title="目标与对齐" query={query} lead="已发布的目标与关键结果、主动联系的暂停与恢复、预算，以及每一次控制变更。">{data=><>
    <p className="workbench-summary" role="status">{data.goals.length?`已发布目标 ${data.goals.length} 个`:'当前没有已发布目标'} · 主动联系{data.control.scopes.some(s=>s.paused)
      ?`已暂停：${data.control.scopes.filter(s=>s.paused).map(s=>statusLabel('scope_kind',s.scope_kind)).join('、')}`:'未暂停'} · 控制修订号 {data.control.revision}</p>
    <h2>目标</h2>
    <DataTable label="目标" empty={{subject:'已发布目标'}} rows={data.goals} rowKey={g=>g.goal_id} columns={[
      {label:'目标',main:true,cell:g=>g.objective},{label:'类型',cell:g=><StatusBadge domain="goal_kind" code={g.goal_kind}/>},{label:'版本',cell:g=>`v${g.current_version}`},{label:'优先级',num:true,cell:g=>g.priority},
      {label:'KR',wide:true,cell:g=>g.key_results.map(k=>`${k.kr_key}：${k.direction==='at_least'?'≥':'≤'} ${k.target}（指标 ${k.metric_id} v${k.metric_version}）`).join('；')||'—'}]}/>
    <h2>主动联系的暂停与恢复</h2>
    <DataTable label="暂停范围" empty={{subject:'暂停或恢复记录'}} rows={data.control.scopes} rowKey={s=>s.scope_kind+':'+s.scope_ref} columns={[
      {label:'范围',main:true,cell:s=><ScopeText kind={s.scope_kind} value={s.scope_ref}/>},
      {label:'状态',cell:s=><StatusBadge text={s.paused?'已暂停':'运行中'} tone={s.paused?'limited':'ok'}/>},{label:'原因',wide:true,cell:s=>s.reason||'—'}]}/>
    <h2>预算</h2>
    <DataTable label="预算" empty={{subject:'预算'}} rows={data.budgets} rowKey={b=>b.budget_kind} columns={[
      {label:'预算',main:true,cell:b=>statusLabel('budget_kind',b.budget_kind)},{label:'已用',num:true,cell:b=><Money value={b.consumed}/>},
      {label:'上限',num:true,cell:b=><Money value={b.limit_amount}/>},{label:'单位',cell:b=>b.unit}]}/>
    <h2>控制事件</h2>
    <DataTable label="控制事件" empty={{subject:'控制事件'}} rows={data.control.events} rowKey={e=>String(e.revision)} columns={[
      {label:'修订号',num:true,cell:e=>e.revision},{label:'事件',main:true,cell:e=><StatusBadge domain="control_event" code={e.event_kind}/>},
      {label:'范围',wide:true,cell:e=><ScopeText kind={e.scope_kind} value={e.scope_ref}/>},{label:'时间',cell:e=><Time value={e.recorded_at}/>}]}/>
    <Operations actions={actions}/>
  </>}</Page>;
}

export function Plans({actions,consumerId}:{actions?:ReactNode;consumerId?:string}){
  const query=useQuery({queryKey:['workbench','plans',consumerId??''],queryFn:()=>readPlans(consumerId)});
  return <Page title="计划与运行" query={query} lead="规划为有待办的客户生成的计划、它们的步骤与复评时间；需要时可手动复评。">{data=>!data.length?<EmptyState kind="empty" subject="计划"
    next="计划在目标发布后由规划为有待办的客户生成；生成后可在这里查看步骤和复评时间，并按需手动复评。"/>:<><DataTable label="计划" rows={data} rowKey={p=>p.plan_id} columns={[
    {label:'计划',main:true,cell:p=><><IdText kind="计划" value={p.plan_id}/> <span className="kv-hint">v{p.version}</span></>},{label:'客户',cell:p=><a href={routePath({page:'consumers',id:p.consumer_id})}><IdText value={p.consumer_id} copy={false}/></a>},
    {label:'目标版本',cell:p=><code className="id-code">{p.goal_version_ref}</code>},
    {label:'状态',cell:p=><StatusBadge domain="plan_status" code={p.status}/>},{label:'步骤',wide:true,cell:p=>p.steps.length?p.steps.map((s,i)=><span key={s.step_key}>{i?'、':''}<code className="id-code">{s.step_key}</code>{s.reassess_at?<>（复评 <Time value={s.reassess_at}/>）</>:null}</span>):'—'},
    {label:'最近结果',cell:p=>p.outcomes[0]?<><StatusBadge domain="plan_outcome" code={p.outcomes[0].kind}/> <Time value={p.outcomes[0].recorded_at}/></>:'—'}]}/>
    <Operations actions={actions}/></>}</Page>;
}
