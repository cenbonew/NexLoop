/**
 * Enterprise world model W1 (docs/design/workbench-world-model-design.md; owner 2026-10-19 D1-D7), arranged as EIOS arranges it
 * (owner review of release 23, 2026-10-20): the sidebar group 企业世界模型 opens a summary page (counts per concept, then the Event
 * Types) and one page per concept - Object Type, Object, Property, Link, Function, Action - each with its own deep link. Entities use
 * the EIOS English terms; explanatory sentences stay Chinese. How to look at Object Types (Table or Graph) is a view switch under the
 * Object Type title, never a sibling of the concepts. Read only (D5): changes stay candidate review in the knowledge workbench and
 * trusted configuration. A Property of an owner-restricted group shows its name and "受限" only.
 */
import {useMemo,type ReactNode} from 'react';
import {useQuery} from '@tanstack/react-query';
import {readWorldModel,type WorldModel as Model,type WorldObjectType,type WorldProperty} from '../api';
import {WORLD_SECTIONS,type Query,type WorldSection} from '../route';
import {Page} from './Page';
import {DataTable} from '../DataTable';
import {Empty,StatusBadge} from '../ui';
import {when} from './Consumers';
import {CARDINALITY_TEXT,SchemaGraph} from '../graph/SchemaGraph';
import {Instances,type InstanceState} from './WorldInstances';
import {ObjectSets,type ObjectSetState} from './WorldObjectSets';

const RISK_TEXT:Record<string,string>={low:'低',medium:'中',high:'高',critical:'极高'};
const APPROVAL_TEXT:Record<string,string>={none:'无需审批',human:'人工审批',human_review:'人工审批',dual:'双人审批'};
/** What each concept is (EIOS concept list; NexLoop wording). */
export const SECTION_LEAD:Record<WorldSection,string>={
  object_types:'稳定命名、不可变版本与 Property 契约，是企业世界的类型定义。',
  objects:'具体的企业对象：标识、版本、更新时间和你能读取的属性值；受限属性只显示名称，你没有读取权限的 Object 只计数。',
  properties:'归属于 Object Type 的字段定义，含数据类型与约束。',
  links:'Object Type 之间的关系及基数约束。',
  functions:'版本化的只读计算契约。',
  actions:'版本化的写入契约，携带治理与副作用声明；对外的业务变更只经由受治理的 Action。',
  object_sets:'保存的只读查询：一个 Object Type 加条件；按当前数据求值，只显示你有读取权限的 Object。'};
export const sectionLabel=(s:WorldSection)=>WORLD_SECTIONS.find(x=>x.key===s)!.label;

export type WorldModelQuery={graph:boolean;focus:string|null}&InstanceState&ObjectSetState;
export function worldQuery(section:WorldSection|undefined,query:Query|undefined):WorldModelQuery{
  const objects=section==='objects',types=section==='object_types';
  return {graph:types&&query?.view==='graph',focus:types?query?.focus??null:null,
    type:objects?query?.type??null:null,after:objects&&query?.type?query?.after??null:null,object:objects&&query?.type?query?.object??null:null,
    set:section==='object_sets'?query?.set??null:null,offset:section==='object_sets'&&query?.set?Number(query?.offset??0):0};
}
function toQuery(section:WorldSection,q:WorldModelQuery):Query{
  if(section==='object_types')return {...(q.graph?{view:'graph'}:{}),...(q.focus?{focus:q.focus}:{})};
  if(section==='objects'&&q.type)return {type:q.type,...(q.after?{after:q.after}:{}),...(q.object?{object:q.object}:{})};
  if(section==='object_sets'&&q.set)return {set:q.set,...(q.offset?{offset:String(q.offset)}:{})};
  return {};
}

type PropertyRow={type:string;property:WorldProperty};
const Mono=({children}:{children:string})=><code style={{whiteSpace:'nowrap'}}>{children}</code>;
/** Where another page of the group is reached from here (a section and its query). */
export type WorldGo=(section:WorldSection|null,query?:Query,options?:{replace?:boolean})=>void;

export function WorldModel({section,query,go}:{section?:WorldSection;query?:Query;go:WorldGo}){
  const result=useQuery({queryKey:['workbench','world-model'],queryFn:readWorldModel});
  if(!section)return <Page title="企业世界模型" query={result}
    lead="已发布的 Object Type、Object、Property、Link、Function 与 Action（只读）；变更经知识工作台审核与可信配置发布。">{m=><Summary model={m} go={go}/>}</Page>;
  const q=worldQuery(section,query);
  const set=(change:Partial<WorldModelQuery>,replace=false)=>go(section,toQuery(section,{...q,...change}),{replace});
  return <Page title={sectionLabel(section)} query={result} lead={SECTION_LEAD[section]}>{m=><SectionBody section={section} model={m} q={q} set={set} go={go}/>}</Page>;
}

function properties(m:Model):PropertyRow[]{return m.object_types.flatMap(t=>t.properties.map(property=>({type:t.name,property})));}

function Summary({model:m,go}:{model:Model;go:WorldGo}){
  const props=useMemo(()=>properties(m),[m]);
  // Object Sets are read on their own page (ADR-036): the summary links there without a count.
  const counts:Record<WorldSection,number|null>={object_types:m.object_types.length,objects:m.object_types.reduce((n,t)=>n+t.objects,0),properties:props.length,
    links:m.relation_types.length,functions:m.functions.length,actions:m.actions.length,object_sets:null};
  const withheld=props.filter(p=>p.property.withheld).length;
  return <>
    <ul className="world-summary" aria-label="模型计数" style={{display:'grid',gridTemplateColumns:'repeat(auto-fit,minmax(180px,1fr))',gap:8,margin:'8px 0 16px',padding:0,listStyle:'none'}}>
      {WORLD_SECTIONS.map(s=><li key={s.key}><button type="button" className="workbench-block world-count" aria-label={counts[s.key]===null?`${s.label}，查看`:`${s.label} ${counts[s.key]} 个，查看`}
        onClick={()=>go(s.key)} style={{textAlign:'left',width:'100%',height:'100%'}}>
        <span className="note">{s.label}{s.key==='objects'?'（real）':''}</span><strong style={{display:'block',fontSize:22}}>{counts[s.key]??'查看'}</strong>
        <span className="note" style={{display:'block'}}>{SECTION_LEAD[s.key]}</span></button></li>)}
      <li><div className="workbench-block world-count" aria-label={`Event Type ${m.event_types.length} 个`} style={{height:'100%'}}>
        <span className="note">Event Type</span><strong style={{display:'block',fontSize:22}}>{m.event_types.length}</strong>
        <span className="note" style={{display:'block'}}>Object 上发生的业务事件，定义列在本页下方。</span></div></li>
    </ul>
    <p className="note">计算时间 {when(m.computed_at)}{withheld?` · ${withheld} 个 Property 属于负责人限制的属性组，只显示名称`:''}。</p>
    <h2 id="world-event-types">Event Type</h2>
    {m.event_types.length?<DataTable label="Event Type" rows={m.event_types} rowKey={e=>e.name} columns={[
      {label:'Event Type',main:true,cell:e=><Mono>{e.name}</Mono>},{label:'Object Type',cell:e=><TypeLink name={e.object_type} go={go}/>},{label:'版本',num:true,cell:e=>`v${e.version}`},
      {label:'Payload Property',num:true,cell:e=>e.payload_property_count},{label:'说明',wide:true,cell:e=>e.description||'—'}]}/>
      :<Empty kind="none" title="还没有已发布的 Event Type"/>}
  </>;
}

/** An Object Type name that opens it on the Object Type page's graph. */
function TypeLink({name,go}:{name:string;go:WorldGo}){
  return <button type="button" className="link" onClick={()=>go('object_types',{view:'graph',focus:name})}>{name}</button>;
}

/** Table / Graph: how the Object Types are shown (a view of this page, not another concept). */
function ViewSwitch({graph,onChange}:{graph:boolean;onChange:(graph:boolean)=>void}){
  return <div className="segmented" role="group" aria-label="查看方式">
    {([[false,'Table'],[true,'Graph']] as const).map(([value,label])=><button key={label} type="button" aria-pressed={graph===value}
      onClick={()=>{if(graph!==value)onChange(value);}}>{label}</button>)}
  </div>;
}

function SectionBody({section,model:m,q,set,go}:{section:WorldSection;model:Model;q:WorldModelQuery;set:(change:Partial<WorldModelQuery>,replace?:boolean)=>void;go:WorldGo}){
  const props=useMemo(()=>properties(m),[m]);
  const typeLink=(name:string)=><TypeLink name={name} go={go}/>;
  switch(section){
    case 'object_types':return <>
      <ViewSwitch graph={q.graph} onChange={graph=>set({graph,focus:graph?q.focus:null},true)}/>
      {q.graph?<TypeGraph model={m} focus={q.focus} onFocus={focus=>set({focus})}/>
        :m.object_types.length?<DataTable label="Object Type" rows={m.object_types} rowKey={t=>t.name} columns={[
          {label:'Object Type',main:true,cell:t=><button type="button" className="link" onClick={()=>set({graph:true,focus:t.name})}>{t.name}</button>},
          {label:'版本',num:true,cell:t=>`v${t.version}`},{label:'Property',num:true,cell:t=>t.properties.length},
          {label:'主键',cell:t=>t.primary_key.join('、')||'—'},{label:'标题 Property',cell:t=>t.title_property||'—'},
          {label:'Object',num:true,cell:t=>t.objects?<button type="button" className="link" aria-label={`查看 ${t.name} 的 ${t.objects} 个 Object`}
            onClick={()=>go('objects',{type:t.name})}>{t.objects}</button>:0},
          {label:'说明',wide:true,cell:t=>t.description||'—'}]}/>:<Empty kind="none" title="还没有已发布的 Object Type"/>}
    </>;
    case 'objects':return <Instances types={m.object_types} state={q} onState={change=>set(change)}/>;
    case 'object_sets':return <ObjectSets state={q} onState={change=>set(change)}/>;
    case 'properties':return props.length?<DataTable label="Property" rows={props} rowKey={r=>r.type+'/'+r.property.name} columns={[
      {label:'Property',main:true,cell:r=><Mono>{r.property.name}</Mono>},{label:'Object Type',cell:r=>typeLink(r.type)},
      {label:'数据类型',cell:r=>r.property.withheld?<StatusBadge text="受限" tone="limited" code="withheld"/>:r.property.value_type},
      {label:'约束',cell:r=>r.property.withheld?'—':[r.property.required?'必填':'可选',r.property.nullable?'可空':''].filter(Boolean).join(' · ')},
      {label:'属性组',cell:r=>r.property.withheld?'—':r.property.group??'—'},{label:'说明',wide:true,cell:r=>r.property.withheld?'负责人限制的属性组：只显示名称':r.property.description||'—'}]}/>
      :<Empty kind="none" title="还没有 Property"/>;
    case 'links':return m.relation_types.length?<DataTable label="Link" rows={m.relation_types} rowKey={r=>r.name} columns={[
      {label:'Link',main:true,cell:r=><Mono>{r.name}</Mono>},{label:'方向',cell:r=><>{typeLink(r.source_type)} → {typeLink(r.target_type)}</>},
      {label:'基数',cell:r=>CARDINALITY_TEXT[r.cardinality]},{label:'版本',num:true,cell:r=>`v${r.version}`},{label:'反向名',cell:r=>r.inverse_name||'—'},
      {label:'说明',wide:true,cell:r=>r.description||'—'}]}/>:<Empty kind="none" title="暂无已发布的 Link"/>;
    case 'actions':return m.actions.length?<DataTable label="Action" rows={m.actions} rowKey={a=>a.resource_id} columns={[
      {label:'Action',main:true,cell:a=><Mono>{a.name}</Mono>},{label:'版本',num:true,cell:a=>`v${a.version}`},
      {label:'风险',cell:a=>a.risk_level?<StatusBadge text={RISK_TEXT[a.risk_level]??a.risk_level} tone={a.risk_level==='low'?'ok':'limited'} code={a.risk_level}/>:'—'},
      {label:'审批',cell:a=>a.approval_mode?APPROVAL_TEXT[a.approval_mode]??a.approval_mode:'—'},
      {label:'副作用',cell:a=><StatusBadge text={a.has_side_effects?'有':'无'} tone={a.has_side_effects?'limited':'neutral'} code={a.change_scope}/>},
      {label:'Object Type',wide:true,cell:a=>a.object_types.join('、')||'—'},{label:'Scope',cell:a=>a.required_scopes.join('、')||'—'}]}/>
      :<Empty kind="none" title="还没有生效的 Action"/>;
    case 'functions':return m.functions.length?<DataTable label="Function" rows={m.functions} rowKey={f=>f.resource_id} columns={[
      {label:'Function',main:true,cell:f=><Mono>{f.name}</Mono>},{label:'版本',num:true,cell:f=>`v${f.version}`},
      {label:'Object Type',wide:true,cell:f=>f.applies_to.filter(Boolean).join('、')||'—'},{label:'Scope',cell:f=>f.required_scopes.join('、')||'—'}]}/>
      :<Empty kind="none" title="还没有生效的 Function"/>;
  }
}

function TypeGraph({model:m,focus,onFocus}:{model:Model;focus:string|null;onFocus:(id:string|null)=>void}){
  const nodes=useMemo(()=>m.object_types.map(t=>({id:t.name,label:t.name,sub:`v${t.version} · ${t.properties.length} Property · ${t.objects} Object`})),[m]);
  const edges=useMemo(()=>m.relation_types.filter(r=>m.object_types.some(t=>t.name===r.source_type)&&m.object_types.some(t=>t.name===r.target_type))
    .map(r=>({id:r.name,from:r.source_type,to:r.target_type,label:`${r.name} ${CARDINALITY_TEXT[r.cardinality]}`})),[m]);
  const focused=focus?m.object_types.find(t=>t.name===focus)??null:null;
  return <div className="world-graph" style={{display:'grid',gridTemplateColumns:focused?'minmax(0,1fr) minmax(260px,320px)':'minmax(0,1fr)',gap:16}}>
    {nodes.length?<SchemaGraph nodes={nodes} edges={edges} focus={focused?.name??null} onFocus={onFocus} emptyEdgesText="暂无已发布的 Link"
      label={`Object Type 关系图：${nodes.length} 个 Object Type，${edges.length} 条 Link`}/>:<Empty kind="none" title="还没有已发布的 Object Type"/>}
    {focused?<TypePanel type={focused} model={m} onClose={()=>onFocus(null)}/>:null}
  </div>;
}

function TypePanel({type,model,onClose}:{type:WorldObjectType;model:Model;onClose:()=>void}){
  const out=model.relation_types.filter(r=>r.source_type===type.name),into=model.relation_types.filter(r=>r.target_type===type.name&&r.source_type!==type.name);
  const list=(items:ReactNode[])=><ul>{items}</ul>;
  return <aside className="workbench-block world-panel" aria-label={`${type.name} 详情`}>
    <h2 style={{marginTop:0}}>{type.name} <small className="note">v{type.version}</small></h2>
    {type.description?<p>{type.description}</p>:null}
    <p className="note">{type.objects} 个 Object（real）{type.title_property?` · 标题 Property ${type.title_property}`:''}</p>
    <h3>Property（{type.properties.length}）</h3>
    {type.properties.length?list(type.properties.map(p=><li key={p.name}><Mono>{p.name}</Mono> {p.withheld?<StatusBadge text="受限" tone="limited" code="withheld"/>
      :<span className="note">{p.value_type}{p.required?' · 必填':''}</span>}</li>)):<p className="note">没有 Property。</p>}
    <h3>Link</h3>
    {out.length||into.length?list([...out.map(r=><li key={'o'+r.name}>→ {r.target_type}：{r.name}（{CARDINALITY_TEXT[r.cardinality]}）</li>),
      ...into.map(r=><li key={'i'+r.name}>← {r.source_type}：{r.name}（{CARDINALITY_TEXT[r.cardinality]}）</li>)]):<p className="note">没有 Link。</p>}
    <button type="button" onClick={onClose}>关闭</button>
  </aside>;
}
