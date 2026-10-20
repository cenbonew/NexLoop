/**
 * Object Set (ADR-036; capability gap D-G2), a page of the 企业世界模型 group: the tenant's Object Sets (latest version, conditions,
 * projection) and a preview of the selected one, evaluated by the server at read time. Read only: the owner publishes a version
 * through the governed Action; nothing here writes. The preview lists only Objects the member may read (the rest are counted as
 * 不可见) and shows a projected Property only with its own READ (otherwise 受限). A set whose filter or sort Property was restricted
 * after publication is withheld as a whole. Entities use the EIOS English terms (UI spec draft v1, §7/§8/§12).
 */
import {useQuery} from '@tanstack/react-query';
import {kindOf,readObjectSetMembers,readObjectSets,STATE_TEXT,type ObjectSetFilter,type ObjectSetSummary} from '../api';
import {DataTable} from '../DataTable';
import {Empty,StatusBadge} from '../ui';
import {Failure} from './Page';
import {when} from './Consumers';

export type ObjectSetState={set:string|null;offset:number};
const OPERATOR_TEXT:Record<ObjectSetFilter['operator'],string>={eq:'等于',ne:'不等于',gt:'大于',gte:'不小于',lt:'小于',lte:'不大于',in:'属于',not_in:'不属于'};
const shortId=(id:string)=>id.length>14?id.slice(0,8)+'…'+id.slice(-4):id;
const literal=(v:unknown)=>typeof v==='string'?`“${v}”`:String(v);
export const conditionText=(f:ObjectSetFilter)=>`${f.property} ${OPERATOR_TEXT[f.operator]} ${Array.isArray(f.value)?f.value.map(literal).join('、'):literal(f.value)}`;
const shown=(v:unknown)=>v===null||v===undefined?'—':typeof v==='object'?JSON.stringify(v):String(v);

export function ObjectSets({state,onState}:{state:ObjectSetState;onState:(change:Partial<ObjectSetState>)=>void}){
  const sets=useQuery({queryKey:['workbench','object-sets'],queryFn:readObjectSets});
  if(sets.isPending)return <p role="status">{STATE_TEXT.loading}</p>;
  if(sets.isError)return kindOf(sets.error)==='forbidden'?<Empty kind="forbidden"/>:<Failure kind={kindOf(sets.error)} onRetry={()=>void sets.refetch()}/>;
  const all=sets.data;
  if(!all.length)return <Empty kind="none" title="暂无 Object Set" next="负责人发布 Object Set 后会出现在这里。"/>;
  const selected=all.find(s=>s.stable_name===state.set)??null;
  return <>
    <DataTable label="Object Set" rows={all} rowKey={s=>s.stable_name} rowClass={s=>s.stable_name===state.set?'row-selected':undefined} columns={[
      {label:'Object Set',main:true,cell:s=><button type="button" className="link" aria-pressed={s.stable_name===state.set}
        onClick={()=>onState({set:s.stable_name===state.set?null:s.stable_name,offset:0})}><code>{s.stable_name}</code></button>},
      {label:'版本',cell:s=>`v${s.version}${s.versions>1?`（共 ${s.versions} 个版本）`:''}`},{label:'Object Type',cell:s=><code>{s.object_type}</code>},
      {label:'条件',wide:true,cell:s=>s.filters.length?s.filters.map(conditionText).join('；'):'全部 Object'},
      {label:'发布时间',cell:s=><span style={{whiteSpace:'nowrap'}}>{when(s.published_at)}</span>}]}/>
    {selected?<Preview set={selected} offset={state.offset} onOffset={offset=>onState({offset})}/>
      :<Empty kind="none" title="选择一个 Object Set 预览" next="预览由服务端按当前数据求值；你没有读取权限的 Object 只计数，不显示。"/>}
  </>;
}

function Preview({set,offset,onOffset}:{set:ObjectSetSummary;offset:number;onOffset:(offset:number)=>void}){
  const page=useQuery({queryKey:['workbench','object-set-members',set.stable_name,set.version,offset],queryFn:()=>readObjectSetMembers(set.stable_name,offset)});
  return <section className="workbench-block" aria-label={`${set.stable_name} 预览`}>
    <h2 style={{marginTop:0}}><code>{set.stable_name}</code> 预览</h2>
    <dl className="facts">
      <div><dt>Object Type</dt><dd><code>{set.object_type}</code>（v{set.object_type_version}）</dd></div>
      <div><dt>条件</dt><dd>{set.filters.length?set.filters.map(conditionText).join('；'):'全部 Object'}</dd></div>
      <div><dt>排序</dt><dd>{set.sort.length?set.sort.map(s=>`${s.property}${s.direction==='desc'?' 降序':' 升序'}`).join('，'):'按标识'}</dd></div>
      <div><dt>显示的 Property</dt><dd>{set.projection.join('、')}</dd></div>
      <div><dt>发布原因</dt><dd>{set.reason}</dd></div>
    </dl>
    {page.isPending?<p role="status">{STATE_TEXT.loading}</p>
      :page.isError?(kindOf(page.error)==='forbidden'?<Empty kind="forbidden"/>:kindOf(page.error)==='not_found'?<Empty kind="none" title="Object Set 已不存在"/>
        :<Failure kind={kindOf(page.error)} onRetry={()=>void page.refetch()}/>)
      :page.data.status==='withheld'?<Empty kind="forbidden" title="这个 Object Set 暂不可预览" next="它的条件或排序用到的 Property 已被负责人限制，求值结果会泄露受限内容，因此不求值。"/>
      :<>
        <p className="workbench-summary" role="status">符合条件 {page.data.total} 个 · 本页显示 {page.data.items.length} 个
          {page.data.invisible?<> · <StatusBadge text={`本页不可见 ${page.data.invisible} 个`} tone="limited" code="invisible"/></>:null}</p>
        {page.data.items.length?<DataTable label={`${set.stable_name} Object`} rows={page.data.items} rowKey={r=>r.object_id} columns={[
          {label:'Object',main:true,cell:r=><code title={r.object_id}>{shortId(r.object_id)}</code>},
          ...page.data.projection.map(p=>({label:p,cell:(r:typeof page.data.items[number])=>r.withheld.includes(p)
            ?<StatusBadge text="受限" tone="limited" code="withheld"/>:shown(r.values[p])}))]}/>
          :<Empty kind={page.data.invisible?'forbidden':'none'} title={page.data.invisible?`本页 ${page.data.invisible} 个 Object 你没有读取权限`:'暂无符合条件的 Object'}/>}
        <div className="pager">{offset>0?<button type="button" onClick={()=>onOffset(Math.max(0,offset-25))}>上一页</button>:null}
          {page.data.next_offset!==null?<button type="button" onClick={()=>onOffset(page.data.next_offset!)}>下一页</button>:null}</div>
      </>}
  </section>;
}
