/**
 * Enterprise world model W1 (docs/design/workbench-world-model-design.md; owner 2026-10-19 D1-D7), arranged as EIOS arranges it
 * (owner review of release 23, 2026-10-20): the sidebar group 企业世界模型 opens a summary page (counts per concept, then the Event
 * Types) and one page per concept - Object Type, Object, Property, Link, Function, Action - each with its own deep link. Entities use
 * the EIOS English terms; explanatory sentences stay Chinese. How to look at Object Types (Table or Graph) is a view switch under the
 * Object Type title, never a sibling of the concepts. Read only (D5): changes stay candidate review in the knowledge workbench and
 * trusted configuration. A Property of an owner-restricted group shows its name and "受限" only.
 */
import {useMemo,useState,type ReactNode} from 'react';
import {useQuery} from '@tanstack/react-query';
import {readWorldModel,type WorldModel as Model,type WorldObjectType,type WorldProperty,type WorldRelation} from '../api';
import {WORLD_SECTIONS,type Query,type WorldSection} from '../route';
import {Page} from './Page';
import {DataTable} from '../DataTable';
import {Keyword,StatusBadge,Time} from '../ui';
import {SectionTerm,Term,WORLD_SECTION_NAMES,glossMatches,glossText,sectionText} from '../Gloss';
import {GlossEditor,type GlossTarget} from '../GlossEdit';
import {Card,CardGrid,EmptyState,KeyValue,MetricCard} from '../kit';
import {statusLabel} from '../status-labels';
import {CARDINALITY_TEXT,SchemaGraph} from '../graph/SchemaGraph';
import {Instances,type InstanceState} from './WorldInstances';
import {ObjectSets,type ObjectSetState} from './WorldObjectSets';

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
/** Object Type glosses by name, for the places that name a type by reference (Link ends, Event Types, Action and Function scopes). */
const typeGlosses=(m:Model)=>new Map(m.object_types.map(t=>[t.name,t] as const));

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
/** One correction target per identifier (a Property name shared by several types is glossed once). */
const unique=(targets:GlossTarget[])=>[...new Map(targets.map(t=>[t.identifier,t])).values()];
/** Property names of a type with their glosses (primary key, title Property). */
function propTerms(t:WorldObjectType,names:string[]):ReactNode{
  if(!names.length)return '—';
  return names.map((n,i)=>{const p=t.properties.find(x=>x.name===n);return <span key={n}>{i?'、':''}<Term id={n} gloss={p?.gloss} source={p?.gloss_source}/></span>;});
}
/** Where another page of the group is reached from here (a section and its query). */
export type WorldGo=(section:WorldSection|null,query?:Query,options?:{replace?:boolean})=>void;

export function WorldModel({section,query,go}:{section?:WorldSection;query?:Query;go:WorldGo}){
  const result=useQuery({queryKey:['workbench','world-model'],queryFn:readWorldModel});
  if(!section)return <Page title="企业世界模型" query={result}
    lead="已发布的 Object Type、Object、Property、Link、Function 与 Action（只读）；变更经知识工作台审核与可信配置发布。">{m=><Summary model={m} go={go}/>}</Page>;
  const q=worldQuery(section,query);
  const set=(change:Partial<WorldModelQuery>,replace=false)=>go(section,toQuery(section,{...q,...change}),{replace});
  return <Page title={<SectionTerm name={sectionLabel(section)}/>} query={result} lead={SECTION_LEAD[section]}>{m=><SectionBody key={section} section={section} model={m} q={q} set={set} go={go}/>}</Page>;
}

function properties(m:Model):PropertyRow[]{return m.object_types.flatMap(t=>t.properties.map(property=>({type:t.name,property})));}

function Summary({model:m,go}:{model:Model;go:WorldGo}){
  const S=(name:string)=>sectionText(name,m.glossary.sections);
  const props=useMemo(()=>properties(m),[m]);
  // Object Sets are read on their own page (ADR-036): the summary links there without a count.
  const counts:Record<WorldSection,number|null>={object_types:m.object_types.length,objects:m.object_types.reduce((n,t)=>n+t.objects,0),properties:props.length,
    links:m.relation_types.length,functions:m.functions.length,actions:m.actions.length,object_sets:null};
  const withheld=props.filter(p=>p.property.withheld).length;
  return <>
    <CardGrid label="模型计数" min="14rem">
      {WORLD_SECTIONS.map(s=><MetricCard key={s.key} label={S(s.label)+(s.key==='objects'?' · real':'')} value={counts[s.key]??'查看'} hint={SECTION_LEAD[s.key]}
        onOpen={()=>go(s.key)}/>)}
      <MetricCard label={S('Event Type')} value={m.event_types.length} hint="Object 上发生的业务事件，定义列在本页下方。"/>
    </CardGrid>
    <p className="note">计算时间 <Time value={m.computed_at}/>{withheld?` · ${withheld} 个 Property 属于负责人限制的属性组，只显示名称`:''}。</p>
    <h2 id="world-event-types"><SectionTerm name="Event Type"/></h2>
    <div className="world-list-head"><GlossEditor world={m.world} kind="event_type" label=" Event Type " targets={m.event_types.map(e=>({identifier:e.name,gloss:e.gloss}))}/><GlossEditor world={m.world} kind="section" label="栏目名" targets={WORLD_SECTION_NAMES.map(k=>({identifier:k,gloss:m.glossary.sections[k]??null}))}/></div>
    {m.event_types.length?<DataTable label="Event Type" rows={m.event_types} rowKey={e=>e.name} columns={[
      {label:S('Event Type'),main:true,cell:e=><Term id={e.name} gloss={e.gloss} source={e.gloss_source}/>},{label:S('Object Type'),cell:e=><TypeLink name={e.object_type} model={m} go={go}/>},{label:'版本',num:true,cell:e=>`v${e.version}`},
      {label:S('Payload Property'),num:true,cell:e=>e.payload_property_count},{label:'说明',wide:true,cell:e=>e.description||'—'}]}/>
      :<EmptyState kind="empty" subject="已发布的 Event Type"/>}
  </>;
}

/** An Object Type name that opens it on the Object Type page's graph. */
function TypeLink({name,model,go}:{name:string;model:Model;go:WorldGo}){
  const t=model.object_types.find(x=>x.name===name);
  return <button type="button" className="link" onClick={()=>go('object_types',{view:'graph',focus:name})}><Term id={name} gloss={t?.gloss} source={t?.gloss_source}/></button>;
}

/** Table / Graph: how the Object Types are shown (a view of this page, not another concept). */
function ViewSwitch({graph,onChange}:{graph:boolean;onChange:(graph:boolean)=>void}){
  return <div className="segmented" role="group" aria-label="查看方式">
    {([[false,'Table'],[true,'Graph']] as const).map(([value,label])=><button key={label} type="button" aria-pressed={graph===value}
      onClick={()=>{if(graph!==value)onChange(value);}}><SectionTerm name={label}/></button>)}
  </div>;
}

function SectionBody({section,model:m,q,set,go}:{section:WorldSection;model:Model;q:WorldModelQuery;set:(change:Partial<WorldModelQuery>,replace?:boolean)=>void;go:WorldGo}){
  const S=(name:string)=>sectionText(name,m.glossary.sections);
  const props=useMemo(()=>properties(m),[m]);const types=useMemo(()=>typeGlosses(m),[m]);
  const typeLink=(name:string)=><TypeLink name={name} model={m} go={go}/>;
  const typeTerms=(names:(string|null)[])=>{const shown=names.filter((n):n is string=>!!n);
    return shown.length?shown.map((n,i)=><span key={n}>{i?'、':''}<Term id={n} gloss={types.get(n)?.gloss} source={types.get(n)?.gloss_source}/></span>):'—';};
  // Owner 2026-10-29: each list is searchable by the English identifier or its Chinese gloss.
  const [keyword,setKeyword]=useState('');
  // Owner walkthrough R56 (W5): the glossary correction sits beside the search box, above the table, not below its last row.
  const search=(what:string,...editors:ReactNode[])=><div className="world-list-head"><Keyword value={keyword} onChange={setKeyword} label={`查找 ${what}（英文或中文）`}/>{editors}</div>;
  const hit=(id:string,gloss:string|null)=>glossMatches(keyword,id,gloss);
  const filtered={filtered:!!keyword.trim()};
  switch(section){
    case 'object_types':return <>
      <ViewSwitch graph={q.graph} onChange={graph=>set({graph,focus:graph?q.focus:null},true)}/>
      {/* Batch 52: the table is the default view; below 640 px the graph stacks into one tall column, so it says so and offers the
          table, while staying reachable. */}
      {q.graph?<p className="note narrow-only" data-hint="narrow-graph">窄屏建议用表格：图在手机宽度下会排成很长的一列。{' '}
        <button type="button" className="link" onClick={()=>set({graph:false,focus:null},true)}>切换到表格</button></p>:null}
      {q.graph?<TypeGraph model={m} focus={q.focus} onFocus={focus=>set({focus})}/>
        :m.object_types.length?<>{search('Object Type',<GlossEditor key="object_type" world={m.world} kind="object_type" label=" Object Type " targets={m.object_types.map(t=>({identifier:t.name,gloss:t.gloss}))}/>)}<DataTable label="Object Type" rows={m.object_types.filter(t=>hit(t.name,t.gloss))} rowKey={t=>t.name} empty={filtered} columns={[
          {label:S('Object Type'),main:true,cell:t=><button type="button" className="link" onClick={()=>set({graph:true,focus:t.name})}><Term id={t.name} gloss={t.gloss} source={t.gloss_source}/></button>},
          {label:'版本',num:true,cell:t=>`v${t.version}`},{label:S('Property'),num:true,cell:t=>t.properties.length},
          {label:'主键',cell:t=>propTerms(t,t.primary_key)},{label:'标题 '+S('Property'),cell:t=>t.title_property?propTerms(t,[t.title_property]):'—'},
          {label:S('Object'),num:true,cell:t=>t.objects?<button type="button" className="link" aria-label={`查看 ${glossText(t.name,t.gloss)} 的 ${t.objects} 个 Object`}
            onClick={()=>go('objects',{type:t.name})}>{t.objects}</button>:0},
          {label:'说明',wide:true,cell:t=>t.description||'—'}]}/>
          </>:<EmptyState kind="empty" subject="已发布的 Object Type"/>}
    </>;
    case 'objects':return <Instances types={m.object_types} state={q} onState={change=>set(change)}/>;
    case 'object_sets':return <ObjectSets state={q} onState={change=>set(change)}/>;
    case 'properties':return props.length?<>{search('Property',<GlossEditor key="property" world={m.world} kind="property" label=" Property " targets={unique(props.map(r=>({identifier:r.property.name,gloss:r.property.gloss})))}/>)}<DataTable label="Property" rows={props.filter(r=>hit(r.property.name,r.property.gloss)||hit(r.type,types.get(r.type)?.gloss??null))}
      rowKey={r=>r.type+'/'+r.property.name} empty={filtered} columns={[
      {label:S('Property'),main:true,cell:r=><Term id={r.property.name} gloss={r.property.gloss} source={r.property.gloss_source}/>},{label:S('Object Type'),cell:r=>typeLink(r.type)},
      {label:'数据类型',cell:r=>r.property.withheld?<StatusBadge text="受限" tone="limited" code="withheld"/>:r.property.value_type},
      {label:'约束',cell:r=>r.property.withheld?'—':[r.property.required?'必填':'可选',r.property.nullable?'可空':''].filter(Boolean).join(' · ')},
      {label:'属性组',cell:r=>r.property.withheld?'—':r.property.group??'—'},{label:'说明',wide:true,cell:r=>r.property.withheld?'负责人限制的属性组：只显示名称':r.property.description||'—'}]}/>
      </>
      :<EmptyState kind="empty" subject="Property"/>;
    case 'links':return m.relation_types.length?<>{search('Link',<GlossEditor key="relation" world={m.world} kind="relation" label=" Link " targets={m.relation_types.map(r=>({identifier:r.name,gloss:r.gloss}))}/>,<GlossEditor key="relation_inverse" world={m.world} kind="relation_inverse" label="反向名" targets={unique(m.relation_types.filter(r=>r.inverse_name).map(r=>({identifier:r.inverse_name,gloss:r.inverse_gloss})))}/>)}<DataTable label="Link" rows={m.relation_types.filter(r=>hit(r.name,r.gloss)||(!!r.inverse_name&&hit(r.inverse_name,r.inverse_gloss)))}
      rowKey={r=>r.name} empty={filtered} columns={[
      {label:S('Link'),main:true,cell:r=><Term id={r.name} gloss={r.gloss} source={r.gloss_source}/>},{label:'方向',cell:r=><>{typeLink(r.source_type)} → {typeLink(r.target_type)}</>},
      {label:'基数',cell:r=>CARDINALITY_TEXT[r.cardinality]},{label:'版本',num:true,cell:r=>`v${r.version}`},{label:'反向名',cell:r=>r.inverse_name?<Term id={r.inverse_name} gloss={r.inverse_gloss} source={r.inverse_gloss_source}/>:'—'},
      {label:'说明',wide:true,cell:r=>r.description||'—'}]}/>
      
      </>:<EmptyState kind="empty" subject="已发布的 Link"/>;
    case 'actions':return m.actions.length?<>{search('Action',<GlossEditor key="action" world={m.world} kind="action" label=" Action " targets={m.actions.map(a=>({identifier:a.name,gloss:a.gloss}))}/>)}<DataTable label="Action" rows={m.actions.filter(a=>hit(a.name,a.gloss))} rowKey={a=>a.resource_id} empty={filtered} columns={[
      {label:S('Action'),main:true,cell:a=><Term id={a.name} gloss={a.gloss} source={a.gloss_source}/>},{label:'版本',num:true,cell:a=>`v${a.version}`},
      {label:'风险',cell:a=>a.risk_level?<StatusBadge domain="action_risk" code={a.risk_level}/>:'—'},
      {label:'审批',cell:a=>a.approval_mode?<span title={a.approval_mode}>{statusLabel('action_approval',a.approval_mode)}</span>:'—'},
      {label:'副作用',cell:a=><StatusBadge text={a.has_side_effects?'有':'无'} tone={a.has_side_effects?'limited':'neutral'} code={a.change_scope}/>},
      {label:S('Object Type'),wide:true,cell:a=>typeTerms(a.object_types)},{label:'Scope',cell:a=>a.required_scopes.join('、')||'—'}]}/>
      </>
      :<EmptyState kind="empty" subject="生效的 Action"/>;
    case 'functions':return m.functions.length?<>{search('Function',<GlossEditor key="function" world={m.world} kind="function" label=" Function " targets={m.functions.map(f=>({identifier:f.name,gloss:f.gloss}))}/>)}<DataTable label="Function" rows={m.functions.filter(f=>hit(f.name,f.gloss))} rowKey={f=>f.resource_id} empty={filtered} columns={[
      {label:S('Function'),main:true,cell:f=><Term id={f.name} gloss={f.gloss} source={f.gloss_source}/>},{label:'版本',num:true,cell:f=>`v${f.version}`},
      {label:S('Object Type'),wide:true,cell:f=>typeTerms(f.applies_to)},{label:'Scope',cell:f=>f.required_scopes.join('、')||'—'}]}/>
      </>
      :<EmptyState kind="empty" subject="生效的 Function"/>;
  }
}

function TypeGraph({model:m,focus,onFocus}:{model:Model;focus:string|null;onFocus:(id:string|null)=>void}){
  const nodes=useMemo(()=>m.object_types.map(t=>({id:t.name,label:t.name,gloss:t.gloss,sub:`v${t.version} · ${t.properties.length} Property · ${t.objects} Object`})),[m]);
  const edges=useMemo(()=>m.relation_types.filter(r=>m.object_types.some(t=>t.name===r.source_type)&&m.object_types.some(t=>t.name===r.target_type))
    .map(r=>({id:r.name,from:r.source_type,to:r.target_type,label:`${glossText(r.name,r.gloss)} ${CARDINALITY_TEXT[r.cardinality]}`})),[m]);
  const focused=focus?m.object_types.find(t=>t.name===focus)??null:null;
  return <div className="world-graph-frame"><div className={'world-graph'+(focused?' world-graph-panel':'')}>
    {nodes.length?<SchemaGraph nodes={nodes} edges={edges} focus={focused?.name??null} onFocus={onFocus} emptyEdgesText="暂无已发布的 Link"
      label={`Object Type 关系图：${nodes.length} 个 Object Type，${edges.length} 条 Link`}/>:<EmptyState kind="empty" subject="已发布的 Object Type"/>}
    {focused?<TypePanel type={focused} model={m} onClose={()=>onFocus(null)}/>:null}
  </div></div>;
}

function TypePanel({type,model,onClose}:{type:WorldObjectType;model:Model;onClose:()=>void}){
  const out=model.relation_types.filter(r=>r.source_type===type.name),into=model.relation_types.filter(r=>r.target_type===type.name&&r.source_type!==type.name);
  const list=(items:ReactNode[])=><ul>{items}</ul>;
  const typeTerm=(name:string)=>{const t=model.object_types.find(x=>x.name===name);return <Term id={name} gloss={t?.gloss} source={t?.gloss_source}/>;};
  const linkRow=(key:string,arrow:string,said:string,r:WorldRelation,other:string)=><li key={key+r.name} className="panel-link">
    <span className="panel-link-dir" aria-label={said}>{arrow}</span><span className="panel-link-type">{typeTerm(other)}</span>
    <span className="panel-link-card">{CARDINALITY_TEXT[r.cardinality]}</span><span className="panel-link-name"><Term id={r.name} gloss={r.gloss} source={r.gloss_source}/></span></li>;
  return <aside className="world-panel" aria-label={`${glossText(type.name,type.gloss)} 详情`}>
    <Card title={<Term id={type.name} gloss={type.gloss} source={type.gloss_source} inline/>} badge={<span className="note">v{type.version}</span>} footer={<button type="button" onClick={onClose}>关闭</button>}>
      {type.description?<p>{type.description}</p>:null}
      <KeyValue label={`${type.name} 概况`} items={[{key:sectionText('Object',model.glossary.sections)+' · real',value:type.objects},{key:'标题 '+sectionText('Property',model.glossary.sections),value:type.title_property?propTerms(type,[type.title_property]):'—'},
        {key:'主键',value:propTerms(type,type.primary_key)}]}/>
      <h3><SectionTerm name="Property"/> · {type.properties.length}</h3>
      {type.properties.length?list(type.properties.map(p=><li key={p.name}><Term id={p.name} gloss={p.gloss} source={p.gloss_source}/> {p.withheld?<StatusBadge text="受限" tone="limited" code="withheld"/>
        :<span className="note">{p.value_type}{p.required?' · 必填':''}</span>}</li>)):<EmptyState kind="empty" subject="Property" compact/>}
      <h3><SectionTerm name="Link"/></h3>
      {out.length||into.length?<ul className="panel-links">{[...out.map(r=>linkRow('o','→','指向',r,r.target_type)),...into.map(r=>linkRow('i','←','来自',r,r.source_type))]}</ul>
        :<EmptyState kind="empty" subject="Link" compact/>}
      <GlossEditor key={type.name} world={model.world} kind="object_type" label={` ${type.name} `} targets={[{identifier:type.name,gloss:type.gloss}]}/>
    </Card>
  </aside>;
}
