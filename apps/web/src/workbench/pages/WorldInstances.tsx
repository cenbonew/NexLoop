/**
 * Enterprise world model W2 (the Object page) (docs/design/workbench-world-model-design.md §2/§5; owner 2026-10-19 D3; ADR-035): a type's instances
 * and one object's depth-1 neighbourhood, on L6's reads (0223/0224). D-G1 (owner 2026-10-20): property values are shown where the
 * member's per-property READ holds (read and audited server side); an owner-restricted or underived property, or one naming a
 * Consumer being erased, is named with the one "受限" badge and no value. The table scrolls sideways inside its own box and long
 * values are cut with an expander (UI spec draft §5). Every object and neighbour is listed only when the member's READ holds (audited server side); the rest are counted and
 * said as they are: "本页不可见 N 个" for unreadable objects of a page, per group "不可见 N 个" (checked, unreadable) and
 * "折叠 N 个" (beyond the 25 checked a group). Type, page cursor and the open object live in the URL (deep links; Back walks pages).
 */
import {useQuery} from '@tanstack/react-query';
import {readObjectNeighbors,readObjectValues,readObjects,type NeighborGroup,type ObjectNeighbors,type PropertyValue,type WorldObjectType} from '../api';
import {routePath,type Route} from '../route';
import {DataTable} from '../DataTable';
import {Empty,StatusBadge} from '../ui';
import {Failure} from './Page';
import {kindOf,STATE_TEXT} from '../api';
import {when} from './Consumers';
import {SchemaGraph} from '../graph/SchemaGraph';

const shortId=(id:string)=>id.length>14?id.slice(0,8)+'…'+id.slice(-4):id;
/** Where the workbench already has a page for an object of this type (its content, with its own READ checks). */
export function objectPage(type:string,id:string):Route|null{
  if(!/^[a-f0-9]{64}$/.test(id))return null;
  return type==='Consumer'?{page:'consumers',id}:type==='Commitment'?{page:'commitments',id}:null;
}
const WITHHELD_TEXT:Record<NonNullable<PropertyValue['reason']>,string>={restricted:'负责人已限制这一属性组',not_permitted:'当前角色不能读取这一属性',
  erasing:'值涉及正在删除的消费者'};
const CUT=48;
/** One property value: the text (cut with an expander when long), the one withheld badge, or an em dash when missing. */
export function PropertyCell({p,expanded=false}:{p:PropertyValue;expanded?:boolean}){
  if(p.status==='withheld')return <span title={p.reason?WITHHELD_TEXT[p.reason]:undefined}><StatusBadge text="受限" tone="limited" code="withheld"/></span>;
  if(p.status==='missing'||p.value===undefined)return <span className="note">—</span>;
  const value=p.valueType==='json'?<code>{p.value}</code>:<>{p.value}</>;
  const tail=p.truncated?<span className="note">（已截断）</span>:null;
  if(p.value.length<=CUT)return <span style={{whiteSpace:'nowrap'}}>{value}{tail}</span>;
  return <details className="property-value" open={expanded}><summary title={p.value}>{p.value.slice(0,CUT)}…</summary>
    <div style={{whiteSpace:'pre-wrap',wordBreak:'break-word',maxWidth:'40rem'}}>{value}{tail}</div></details>;
}
/** The property columns of a page: the first listed object's schema order, then any name only later objects carry. */
export function propertyColumns(rows:{properties:PropertyValue[]}[]):string[]{
  const names:string[]=[];for(const r of rows)for(const p of r.properties)if(!names.includes(p.name))names.push(p.name);return names;
}
const viaText=(via:string)=>via.startsWith('property:')?`Property ${via.slice(9)}`:`Link ${via}`;

export type InstanceState={type:string|null;after:string|null;object:string|null};

export function Instances({types,state,onState}:{types:WorldObjectType[];state:InstanceState;onState:(change:Partial<InstanceState>)=>void}){
  const type=state.type&&types.some(t=>t.name===state.type)?state.type:null;
  return <div className="world-instances">
    <div className="field" style={{maxWidth:420}}><label htmlFor="world-instance-type">Object Type</label>
      <select id="world-instance-type" value={type??''} onChange={e=>onState({type:e.target.value||null,after:null,object:null})}>
        <option value="">请选择 Object Type</option>
        {types.map(t=><option key={t.name} value={t.name}>{t.name}（{t.objects} 个）</option>)}</select></div>
    {type?<InstanceList type={type} state={state} onState={onState}/>:<Empty kind="none" title="选择一个 Object Type 查看 Object" next="列出标识、版本、更新时间和你能读取的属性值；你没有读取权限的 Object 只计数，不显示。"/>}
  </div>;
}

function InstanceList({type,state,onState}:{type:string;state:InstanceState;onState:(change:Partial<InstanceState>)=>void}){
  const page=useQuery({queryKey:['workbench','objects',type,state.after??''],queryFn:()=>readObjects(type,state.after??undefined)});
  if(page.isPending)return <p role="status">{STATE_TEXT.loading}</p>;
  if(page.isError)return kindOf(page.error)==='forbidden'?<Empty kind="forbidden"/>:<Failure kind={kindOf(page.error)} onRetry={()=>void page.refetch()}/>;
  const p=page.data;
  return <div className="world-instance-grid" style={{display:'grid',gridTemplateColumns:state.object?'minmax(0,1fr) minmax(280px,420px)':'minmax(0,1fr)',gap:16}}>
    <div>
      <p className="workbench-summary" role="status">{type} 共 {p.total} 个 Object · 本页显示 {p.items.length} 个{p.invisible?<> · <StatusBadge text={`本页不可见 ${p.invisible} 个`} tone="limited" code="invisible"/></>:null}</p>
      {p.items.length?<div className="table-scroll" style={{overflowX:'auto',maxWidth:'100%'}}><DataTable label={`${type} Object`} rows={p.items} rowKey={r=>r.object_id} rowClass={r=>r.object_id===state.object?'row-selected':undefined} columns={[
        {label:'Object',main:true,cell:r=><button type="button" className="link" aria-pressed={r.object_id===state.object} title={r.object_id}
          onClick={()=>onState({object:r.object_id===state.object?null:r.object_id})}><code>{shortId(r.object_id)}</code></button>},
        ...propertyColumns(p.items).map(name=>({label:name,cell:(r:typeof p.items[number])=>{const v=r.properties.find(x=>x.name===name);return v?<PropertyCell p={v}/>:<span className="note">—</span>;}})),
        {label:'Schema 版本',num:true,cell:r=>`v${r.schema_version}`},{label:'修订',num:true,cell:r=>r.revision},
        {label:'更新时间',cell:r=><span style={{whiteSpace:'nowrap'}}>{when(r.updated_at)}</span>}]}/></div>
        :<Empty kind={p.invisible?'forbidden':'none'} title={p.invisible?`本页 ${p.invisible} 个 Object 你没有读取权限`:'这一页没有 Object'}/>}
      <div className="pager">{state.after?<button type="button" onClick={()=>window.history.back()}>上一页</button>:null}
        {p.next_after?<button type="button" onClick={()=>onState({after:p.next_after,object:null})}>下一页</button>:null}</div>
    </div>
    {state.object?<ObjectDrawer type={type} id={state.object} row={p.items.find(r=>r.object_id===state.object)??null} onOpen={(t,id)=>onState({type:t,after:null,object:id})}
      onClose={()=>onState({object:null})}/>:null}
  </div>;
}

function ObjectDrawer({type,id,row,onOpen,onClose}:{type:string;id:string;row:{schema_version:number;revision:number;updated_at:string}|null;
  onOpen:(type:string,id:string)=>void;onClose:()=>void}){
  const neighbors=useQuery({queryKey:['workbench','object-neighbors',type,id],queryFn:()=>readObjectNeighbors(type,id)});
  const values=useQuery({queryKey:['workbench','object-values',type,id],queryFn:()=>readObjectValues(type,id)});
  const page=objectPage(type,id);
  return <aside className="workbench-block world-panel world-object" aria-label={`${type} ${shortId(id)} 详情`}>
    <h2 style={{marginTop:0}}>{type} <code title={id}>{shortId(id)}</code></h2>
    <dl><div><dt>完整标识</dt><dd><code style={{wordBreak:'break-all'}}>{id}</code></dd></div>
      {row?<><div><dt>Schema 版本</dt><dd>v{row.schema_version}</dd></div><div><dt>修订</dt><dd>{row.revision}</dd></div><div><dt>更新时间</dt><dd>{when(row.updated_at)}</dd></div></>:null}</dl>
    <h3>属性</h3>
    {values.isPending?<p role="status">{STATE_TEXT.loading}</p>
      :values.isError?(kindOf(values.error)==='forbidden'?<Empty kind="forbidden"/>:kindOf(values.error)==='not_found'?<Empty kind="none" title="Object 已不存在"/>
        :<Failure kind={kindOf(values.error)} onRetry={()=>void values.refetch()}/>)
      :values.data.properties.length?<dl className="object-properties">{values.data.properties.map(v=><div key={v.name}><dt>{v.name}</dt><dd><PropertyCell p={v} expanded/></dd></div>)}</dl>
      :<Empty kind="none" title="这个 Object 没有 Property"/>}
    {page?<p><a href={routePath(page)}>在{type==='Consumer'?'消费者详情':'承诺详情'}中查看内容</a></p>:null}
    <h3>邻域（深度 1）</h3>
    {neighbors.isPending?<p role="status">{STATE_TEXT.loading}</p>
      :neighbors.isError?(kindOf(neighbors.error)==='forbidden'?<Empty kind="forbidden"/>:kindOf(neighbors.error)==='not_found'?<Empty kind="none" title="Object 已不存在"/>
        :<Failure kind={kindOf(neighbors.error)} onRetry={()=>void neighbors.refetch()}/>)
      :<Neighborhood value={neighbors.data} onOpen={onOpen}/>}
    <button type="button" onClick={onClose}>关闭</button>
  </aside>;
}

/** Nodes of the neighbourhood graph: the centre, every listed neighbour, and one counted node per group for invisible/collapsed. */
export function neighborhoodGraph(v:ObjectNeighbors){
  const centre=`${v.type_name}/${v.object_id}`;
  const nodes=new Map<string,{id:string;label:string;sub?:string}>([[centre,{id:centre,label:`${v.type_name} ${shortId(v.object_id)}`,sub:'当前 Object'}]]);
  const edges:{id:string;from:string;to:string;label?:string}[]=[];
  v.groups.forEach((g,i)=>{
    const link=(other:string)=>edges.push({id:`${i}:${other}`,from:g.direction==='outgoing'?centre:other,to:g.direction==='outgoing'?other:centre,label:g.via.replace(/^property:/,'')});
    for(const item of g.items){const key=`${item.type_name}/${item.object_id}`;if(!nodes.has(key))nodes.set(key,{id:key,label:`${item.type_name} ${shortId(item.object_id)}`});link(key);}
    const hidden=g.invisible+g.collapsed;
    if(hidden){const key=`~${i}`;nodes.set(key,{id:key,label:`${g.type_name} 另 ${hidden} 个`,sub:[g.invisible?`不可见 ${g.invisible}`:'',g.collapsed?`折叠 ${g.collapsed}`:''].filter(Boolean).join(' · ')});link(key);}
  });
  return {nodes:[...nodes.values()],edges};
}

function Neighborhood({value,onOpen}:{value:ObjectNeighbors;onOpen:(type:string,id:string)=>void}){
  if(!value.groups.length)return <Empty kind="none" title="没有关联的 Object"/>;
  const graph=neighborhoodGraph(value);
  return <>
    <ul className="world-neighbors">{value.groups.map((g:NeighborGroup,i)=><li key={i}>
      <span>{g.direction==='outgoing'?'→':'←'} {g.type_name} · {viaText(g.via)}</span>
      {g.items.length?<ul>{g.items.map(item=><li key={item.object_id}><button type="button" className="link" title={item.object_id} onClick={()=>onOpen(item.type_name,item.object_id)}>
        <code>{shortId(item.object_id)}</code></button></li>)}</ul>:null}
      {g.invisible?<StatusBadge text={`不可见 ${g.invisible} 个`} tone="limited" code="invisible"/>:null}
      {g.collapsed?<StatusBadge text={`折叠 ${g.collapsed} 个`} tone="neutral" code="collapsed"/>:null}
      {!g.items.length&&!g.invisible&&!g.collapsed?<span className="note">无</span>:null}</li>)}</ul>
    <p className="note">每组最多检查 {value.group_limit} 个：不可见 = 已检查但你没有读取权限；折叠 = 超出 {value.group_limit} 个未检查，不显示。</p>
    <SchemaGraph nodes={graph.nodes} edges={graph.edges} focus={null} label={`邻域图：${graph.nodes.length} 个节点`}
      onFocus={key=>{if(key&&!key.startsWith('~')&&key!==`${value.type_name}/${value.object_id}`){const [t,...rest]=key.split('/');onOpen(t,rest.join('/'));}}}/>
  </>;
}
