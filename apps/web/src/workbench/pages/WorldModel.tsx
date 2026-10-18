/**
 * Enterprise world model W1 (docs/design/workbench-world-model-design.md; owner 2026-10-19 D1-D7): the tenant's published model,
 * read only. A summary of counts, then six tabs (Object Type, Property, Link, event types, Action, Function) as tables; Object Type
 * and Link also as the schema relation graph (hand-written SVG). Tab, view and the focused type live in the URL, so a view can be
 * linked and Back walks it. Nothing here edits the model (D5): changes stay candidate review in the knowledge workbench and trusted
 * configuration. A property of an owner-restricted group shows its name and "受限" only.
 */
import {useMemo} from 'react';
import {useQuery} from '@tanstack/react-query';
import {readWorldModel,type WorldModel as Model,type WorldObjectType,type WorldProperty} from '../api';
import type {Query} from '../route';
import {Page} from './Page';
import {DataTable} from '../DataTable';
import {Empty,StatusBadge,Tabs} from '../ui';
import {when} from './Consumers';
import {CARDINALITY_TEXT,SchemaGraph} from '../graph/SchemaGraph';

type Tab='object_types'|'properties'|'relations'|'events'|'actions'|'functions';
const TABS:{key:Tab;label:string}[]=[{key:'object_types',label:'Object Type'},{key:'properties',label:'Property'},{key:'relations',label:'Link'},
  {key:'events',label:'事件类型'},{key:'actions',label:'Action'},{key:'functions',label:'Function'}];
const GRAPH_TABS:Tab[]=['object_types','relations'];
const RISK_TEXT:Record<string,string>={low:'低',medium:'中',high:'高',critical:'极高'};
const APPROVAL_TEXT:Record<string,string>={none:'无需审批',human:'人工审批',human_review:'人工审批',dual:'双人审批'};

export type WorldModelQuery={tab:Tab;graph:boolean;focus:string|null};
export function worldQuery(query:Query|undefined):WorldModelQuery{
  const tab=(TABS.some(t=>t.key===query?.tab)?query!.tab:'object_types') as Tab;
  return {tab,graph:query?.view==='graph'&&GRAPH_TABS.includes(tab),focus:query?.focus??null};
}
function toQuery(q:WorldModelQuery):Query{
  return {...(q.tab!=='object_types'?{tab:q.tab}:{}),...(q.graph?{view:'graph'}:{}),...(q.focus?{focus:q.focus}:{})};
}

type PropertyRow={type:string;property:WorldProperty};
const Mono=({children}:{children:string})=><code style={{whiteSpace:'nowrap'}}>{children}</code>;

export function WorldModel({query,onQuery}:{query?:Query;onQuery:(query:Query)=>void}){
  const result=useQuery({queryKey:['workbench','world-model'],queryFn:readWorldModel});
  const q=worldQuery(query);
  const set=(change:Partial<WorldModelQuery>)=>onQuery(toQuery({...q,...change}));
  return <Page title="企业世界模型" query={result}
    lead="已发布的对象、关系、事件、Action 与 Function（只读）；变更经知识工作台审核与可信配置发布。">{m=><Body model={m} q={q} set={set}/>}</Page>;
}

function Body({model:m,q,set}:{model:Model;q:WorldModelQuery;set:(change:Partial<WorldModelQuery>)=>void}){
  const properties=useMemo<PropertyRow[]>(()=>m.object_types.flatMap(t=>t.properties.map(property=>({type:t.name,property}))),[m]);
  const withheld=properties.filter(p=>p.property.withheld).length;
  const counts:[Tab,string,number][]=[['object_types','Object Type',m.object_types.length],['properties','Property',properties.length],['relations','Link',m.relation_types.length],
    ['events','事件类型',m.event_types.length],['actions','Action',m.actions.length],['functions','Function',m.functions.length]];
  const instances=m.object_types.reduce((n,t)=>n+t.objects,0);
  const graphNodes=useMemo(()=>m.object_types.map(t=>({id:t.name,label:t.name,sub:`v${t.version} · ${t.properties.length} 个属性 · ${t.objects} 个实例`})),[m]);
  const graphEdges=useMemo(()=>m.relation_types.filter(r=>m.object_types.some(t=>t.name===r.source_type)&&m.object_types.some(t=>t.name===r.target_type))
    .map(r=>({id:r.name,from:r.source_type,to:r.target_type,label:`${r.name} ${CARDINALITY_TEXT[r.cardinality]}`})),[m]);
  const focused=q.focus?m.object_types.find(t=>t.name===q.focus)??null:null;
  return <>
    <ul className="world-summary" aria-label="模型计数" style={{display:'grid',gridTemplateColumns:'repeat(auto-fit,minmax(130px,1fr))',gap:8,margin:'8px 0 16px',padding:0,listStyle:'none'}}>
      {counts.map(([key,label,n])=><li key={key}><button type="button" className="workbench-block world-count" aria-label={`${label} ${n} 个，查看`}
        onClick={()=>set({tab:key,graph:false,focus:null})} style={{textAlign:'left',width:'100%'}}><span className="note">{label}</span><strong style={{display:'block',fontSize:22}}>{n}</strong></button></li>)}
      <li className="workbench-block world-count"><span className="note">实例（real）</span><strong style={{display:'block',fontSize:22}}>{instances}</strong></li>
    </ul>
    <p className="note">计算时间 {when(m.computed_at)}{withheld?` · ${withheld} 个属性属于负责人限制的属性组，只显示名称`:''}。</p>
    <Tabs label="模型概念" tabs={TABS} value={q.tab} onChange={tab=>set({tab,graph:q.graph&&GRAPH_TABS.includes(tab),focus:null})}
      announce={`${TABS.find(t=>t.key===q.tab)!.label}：${counts.find(c=>c[0]===q.tab)![2]} 项`}/>
    {GRAPH_TABS.includes(q.tab)?<Tabs label="视图" tabs={[{key:'table',label:'表格'},{key:'graph',label:'关系图'}]} value={q.graph?'graph':'table'}
      onChange={v=>set({graph:v==='graph'})}/>:null}
    <div role="tabpanel" aria-label={TABS.find(t=>t.key===q.tab)!.label}>
      {q.graph?<div className="world-graph" style={{display:'grid',gridTemplateColumns:focused?'minmax(0,1fr) minmax(260px,320px)':'minmax(0,1fr)',gap:16}}>
          {graphNodes.length?<SchemaGraph nodes={graphNodes} edges={graphEdges} focus={focused?.name??null} onFocus={id=>set({focus:id})}
            label={`对象类型关系图：${graphNodes.length} 个类型，${graphEdges.length} 条关系`}/>:<Empty kind="none" title="还没有已发布的对象类型"/>}
          {focused?<TypePanel type={focused} model={m} onClose={()=>set({focus:null})}/>:null}
        </div>
        :<Table tab={q.tab} model={m} properties={properties} onType={name=>set({tab:'object_types',graph:true,focus:name})}/>}
    </div>
  </>;
}

function TypePanel({type,model,onClose}:{type:WorldObjectType;model:Model;onClose:()=>void}){
  const out=model.relation_types.filter(r=>r.source_type===type.name),into=model.relation_types.filter(r=>r.target_type===type.name&&r.source_type!==type.name);
  return <aside className="workbench-block world-panel" aria-label={`${type.name} 详情`}>
    <h2 style={{marginTop:0}}>{type.name} <small className="note">v{type.version}</small></h2>
    {type.description?<p>{type.description}</p>:null}
    <p className="note">{type.objects} 个实例（real）{type.title_property?` · 标题属性 ${type.title_property}`:''}</p>
    <h3>属性（{type.properties.length}）</h3>
    {type.properties.length?<ul>{type.properties.map(p=><li key={p.name}><Mono>{p.name}</Mono> {p.withheld?<StatusBadge text="受限" tone="limited" code="withheld"/>:<span className="note">{p.value_type}{p.required?' · 必填':''}</span>}</li>)}</ul>
      :<p className="note">没有属性。</p>}
    <h3>关系</h3>
    {out.length||into.length?<ul>{out.map(r=><li key={'o'+r.name}>→ {r.target_type}：{r.name}（{CARDINALITY_TEXT[r.cardinality]}）</li>)}
      {into.map(r=><li key={'i'+r.name}>← {r.source_type}：{r.name}（{CARDINALITY_TEXT[r.cardinality]}）</li>)}</ul>:<p className="note">没有关系。</p>}
    <button type="button" onClick={onClose}>关闭</button>
  </aside>;
}

function Table({tab,model:m,properties,onType}:{tab:Tab;model:Model;properties:PropertyRow[];onType:(name:string)=>void}){
  const typeLink=(name:string)=><button type="button" className="link" onClick={()=>onType(name)}>{name}</button>;
  switch(tab){
    case 'object_types':return m.object_types.length?<DataTable label="Object Type" rows={m.object_types} rowKey={t=>t.name} columns={[
      {label:'名称',main:true,cell:t=>typeLink(t.name)},{label:'版本',num:true,cell:t=>`v${t.version}`},{label:'Property',num:true,cell:t=>t.properties.length},
      {label:'主键',cell:t=>t.primary_key.join('、')||'—'},{label:'标题属性',cell:t=>t.title_property||'—'},{label:'实例',num:true,cell:t=>t.objects},
      {label:'说明',wide:true,cell:t=>t.description||'—'}]}/>:<Empty kind="none" title="还没有已发布的对象类型"/>;
    case 'properties':return properties.length?<DataTable label="Property" rows={properties} rowKey={r=>r.type+'/'+r.property.name} columns={[
      {label:'Property',main:true,cell:r=><Mono>{r.property.name}</Mono>},{label:'所属类型',cell:r=>typeLink(r.type)},
      {label:'数据类型',cell:r=>r.property.withheld?<StatusBadge text="受限" tone="limited" code="withheld"/>:r.property.value_type},
      {label:'约束',cell:r=>r.property.withheld?'—':[r.property.required?'必填':'可选',r.property.nullable?'可空':''].filter(Boolean).join(' · ')},
      {label:'属性组',cell:r=>r.property.withheld?'—':r.property.group??'—'},{label:'说明',wide:true,cell:r=>r.property.withheld?'负责人限制的属性组：只显示名称':r.property.description||'—'}]}/>
      :<Empty kind="none" title="还没有属性"/>;
    case 'relations':return m.relation_types.length?<DataTable label="Link" rows={m.relation_types} rowKey={r=>r.name} columns={[
      {label:'关系',main:true,cell:r=><Mono>{r.name}</Mono>},{label:'方向',cell:r=><>{typeLink(r.source_type)} → {typeLink(r.target_type)}</>},
      {label:'基数',cell:r=>CARDINALITY_TEXT[r.cardinality]},{label:'版本',num:true,cell:r=>`v${r.version}`},{label:'反向名',cell:r=>r.inverse_name||'—'},
      {label:'说明',wide:true,cell:r=>r.description||'—'}]}/>:<Empty kind="none" title="还没有已发布的关系类型"/>;
    case 'events':return m.event_types.length?<DataTable label="事件类型" rows={m.event_types} rowKey={e=>e.name} columns={[
      {label:'事件',main:true,cell:e=><Mono>{e.name}</Mono>},{label:'对象类型',cell:e=>typeLink(e.object_type)},{label:'版本',num:true,cell:e=>`v${e.version}`},
      {label:'载荷属性',num:true,cell:e=>e.payload_property_count},{label:'说明',wide:true,cell:e=>e.description||'—'}]}/>:<Empty kind="none" title="还没有已发布的事件类型"/>;
    case 'actions':return m.actions.length?<DataTable label="Action" rows={m.actions} rowKey={a=>a.resource_id} columns={[
      {label:'Action',main:true,cell:a=><Mono>{a.name}</Mono>},{label:'版本',num:true,cell:a=>`v${a.version}`},
      {label:'风险',cell:a=>a.risk_level?<StatusBadge text={RISK_TEXT[a.risk_level]??a.risk_level} tone={a.risk_level==='low'?'ok':'limited'} code={a.risk_level}/>:'—'},
      {label:'审批',cell:a=>a.approval_mode?APPROVAL_TEXT[a.approval_mode]??a.approval_mode:'—'},
      {label:'副作用',cell:a=><StatusBadge text={a.has_side_effects?'有':'无'} tone={a.has_side_effects?'limited':'neutral'} code={a.change_scope}/>},
      {label:'对象类型',wide:true,cell:a=>a.object_types.join('、')||'—'},{label:'Scope',cell:a=>a.required_scopes.join('、')||'—'}]}/>
      :<Empty kind="none" title="还没有生效的 Action"/>;
    case 'functions':return m.functions.length?<DataTable label="Function" rows={m.functions} rowKey={f=>f.resource_id} columns={[
      {label:'Function',main:true,cell:f=><Mono>{f.name}</Mono>},{label:'版本',num:true,cell:f=>`v${f.version}`},
      {label:'作用于',wide:true,cell:f=>f.applies_to.filter(Boolean).join('、')||'—'},{label:'Scope',cell:f=>f.required_scopes.join('、')||'—'}]}/>
      :<Empty kind="none" title="还没有生效的 Function"/>;
  }
}
