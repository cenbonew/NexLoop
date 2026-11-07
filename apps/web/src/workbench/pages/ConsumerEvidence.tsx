/**
 * K5 (docs/design/k5-consumer-evidence-views.md §2.3): the consumer profile card at the top of the consumer detail, on existing reads only.
 * First the upcoming commitments (open, due from now, soonest first, at most UPCOMING), then the properties as the Object read gives them
 * (GET /objects/Consumer/{id}: object revision, per-property ok / withheld with its reason / missing). A withheld property keeps its 受限
 * badge and never reads as empty; properties without a value are counted, not drawn as dashes (K4l). Without the Object read (no
 * permission or not available) the card says what the consumer read says, as before.
 */
import {useState,type ReactNode} from 'react';
import {useQuery} from '@tanstack/react-query';
import {COMMITMENT_STATUS_TEXT,INTENT_STATE_TEXT,kindOf,readCommitments,readEvidenceGraph,readObjectValues,type ConsumerDetail,type PropertySource,type PropertySources,type EvidenceAction,type EvidenceCandidate,
  type EvidenceClaim,type EvidenceCommitment,type EvidenceGraph,type EvidenceGroup,type EvidenceKind,type EvidenceOutreach} from '../api';
import {DataTable} from '../DataTable';
import {Time} from '../ui';
import {EmptyState,IdText,KeyValue,shorten} from '../kit';
import {commitmentSummary,dueText} from './Commitments';
import {NO_VALUES_TEXT,PropertyCell} from './WorldInstances';
import {glossText,useWorldGlosses} from '../Gloss';

export const UPCOMING=3;
export const CONSUMER_NO_VALUES=NO_VALUES_TEXT.replace('这些对象','这位客户');

function Upcoming({consumerId}:{consumerId:string}){
  const [now]=useState(()=>new Date().toISOString());
  const q=useQuery({queryKey:['workbench','commitments','upcoming',consumerId,now],
    queryFn:()=>readCommitments({status_group:'open',consumer_id:consumerId,due_after:now,sort:'due_at',order:'asc'},UPCOMING)});
  if(q.isPending)return <EmptyState kind="loading" subject="即将发生的承诺" compact/>;
  if(q.isError)return <EmptyState kind={kindOf(q.error)==='forbidden'?'forbidden':'unavailable'} subject="即将发生的承诺" onRetry={()=>void q.refetch()} compact/>;
  const items=q.data.commitments.slice(0,UPCOMING);
  if(!items.length)return <p className="note">没有即将到期的承诺</p>;
  return <ol className="profile-upcoming">{items.map(c=><li key={c.commitment_id}><span className="profile-due">{dueText(c)}</span> {commitmentSummary(c)}</li>)}</ol>;
}

function Properties({consumerId,fallback}:{consumerId:string;fallback:ReactNode}){
  const glosses=useWorldGlosses();
  const q=useQuery({queryKey:['workbench','object','Consumer',consumerId],queryFn:()=>readObjectValues('Consumer',consumerId),retry:false});
  if(q.isPending)return <EmptyState kind="loading" subject="属性" compact/>;
  if(q.isError)return <div className="note">{fallback}</div>;
  const v=q.data;const shown=v.properties.filter(p=>p.status!=='missing'&&!(p.status==='ok'&&p.value===undefined));
  const missing=v.properties.length-shown.length;
  return <>
    <p className="note profile-revision">修订 r{v.revision} · Schema v{v.schema_version}</p>
    {shown.length?<KeyValue label="客户的 Property" items={shown.map(p=>({key:glossText(p.name,glosses.property('Consumer',p.name).gloss),value:<PropertyCell p={p}/>}))}/>:null}
    {!shown.length?<p className="note">{CONSUMER_NO_VALUES}</p>:missing?<p className="note">另有 {missing} 个属性还没有值，未列出</p>:null}
  </>;
}

// ---- Claim to Consumer: structured properties with where they came from (property_sources block of the consumer read) ----
const DAY_TEXT:Record<string,string>={mon:'周一',tue:'周二',wed:'周三',thu:'周四',fri:'周五',sat:'周六',sun:'周日',any:'每天'};
const PART_TEXT:Record<string,string>={morning:'上午',daytime:'白天',afternoon:'下午',evening:'晚上',any:'全天'};
export const QUOTE_ELSEWHERE='原文见证据视图';
export const PROPERTY_SOURCE_TEXT:Record<string,string>={delivery_windows:'送货时间'};
export const SOURCE_EMPTY={unknown:'暂无（未从对话中获得）',corrected:'已更正：原来的值已撤回，不再显示',restricted:'没有权限查看属性来源',
  none:'还没有从对话中获得的结构化属性'} as const;
/** "周五 白天、周六 任意"; any/any is 随时. A code outside the vocabulary falls back to the server's display text. */
export function windowsText(p:PropertySource):string|null{
  if(!p.value)return null;
  const parts=p.value.map(w=>w.weekday==='any'&&w.part==='any'?'随时':DAY_TEXT[w.weekday]&&PART_TEXT[w.part]?`${DAY_TEXT[w.weekday]} ${PART_TEXT[w.part]}`:null);
  return parts.every(x=>x!==null)?parts.join('、'):p.display;
}
/** Dates in the tenant's time zone, not the browser's: a statement just after midnight there keeps its day. */
export const TENANT_TZ='Asia/Shanghai';
const MD=new Intl.DateTimeFormat('en-US',{timeZone:TENANT_TZ,month:'numeric',day:'numeric'});
export const monthDay=(at:string)=>{const p=Object.fromEntries(MD.formatToParts(new Date(at)).map(x=>[x.type,x.value]));return `${p.month} 月 ${p.day} 日`;};
/** The quote follows K5: read from the evidence graph, which returns it to reviewers only (otherwise 原文受限). A claim the graph does not list
 * (folded as a duplicate, or corrected) is not a permission matter: 原文见证据视图. */
function SourceQuote({consumerId,claimId}:{consumerId:string;claimId:string}){
  const q=useQuery({queryKey:['workbench','evidence-graph',consumerId],queryFn:()=>readEvidenceGraph(consumerId),retry:false});
  if(q.isPending)return null;
  const claim=q.data?.groups.find(g=>g.kind==='claim')?.items.find(i=>(i as EvidenceClaim).claim_id===claimId) as EvidenceClaim|undefined;
  if(q.data&&!claim)return <span className="note source-quote">{QUOTE_ELSEWHERE}</span>;
  return claim?.source.quote_status==='ok'&&claim.source.quote?<q className="source-quote">{claim.source.quote}</q>:<span className="note source-quote">原文受限</span>;
}
function SourceRow({consumerId,p}:{consumerId:string;p:PropertySource}){
  if(p.status==='unknown')return <span className="note">{SOURCE_EMPTY.unknown}</span>;
  if(p.status==='corrected')return <><strong className="property-source-state">已更正</strong> <span className="note">{SOURCE_EMPTY.corrected}</span></>;
  return <div className="property-source">
    <span className="property-source-value">{p.property_name==='delivery_windows'?windowsText(p):p.display??'—'}</span>
    {p.stated_at?<span className="property-source-when">来自 {monthDay(p.stated_at)}的对话</span>:<span className="property-source-when">来自对话</span>}
    {p.effective_at?<span className="note property-source-effective">{monthDay(p.effective_at)}记入</span>:null}
    {p.source?<SourceQuote consumerId={consumerId} claimId={p.source.claim_id}/>:null}
  </div>;
}
export function PropertySourcesList({consumerId,data}:{consumerId:string;data:PropertySources|null}){
  if(!data)return null;
  if(data.status==='restricted')return <p className="note">{SOURCE_EMPTY.restricted}</p>;
  if(!data.items.length)return <p className="note">{SOURCE_EMPTY.none}</p>;
  return <KeyValue label="来自对话的属性" items={data.items.map(p=>({key:PROPERTY_SOURCE_TEXT[p.property_name]??p.property_name,value:<SourceRow consumerId={consumerId} p={p}/>}))}/>;
}

/** The card; `fallback` is the consumer read's own wording for when the Object read is not available to this member, `groups` the
 * owner-restricted property groups (ADR-025), named only. */
export function ProfileCard({data,fallback,groups}:{data:ConsumerDetail;fallback:ReactNode;groups:ReactNode}){
  return <section className="detail-section profile-card" aria-labelledby="consumer-profile">
    <h2 id="consumer-profile">画像</h2>
    <h3>即将发生的承诺</h3><Upcoming consumerId={data.consumer_id}/>
    {data.property_sources?<><h3 id="consumer-property-sources">来自对话</h3><PropertySourcesList consumerId={data.consumer_id} data={data.property_sources}/></>:null}
    <h3 id="consumer-properties">属性</h3><Properties consumerId={data.consumer_id} fallback={fallback}/>
    {groups?<p className="note">{groups}</p>:null}
  </section>;
}

// ---- K5 §2.2: the evidence graph of one consumer (GET /consumers/{id}/evidence-graph, world real only) ----
/** Codes come from the server as codes; a code without a label is shown as it is (never an error). */
export const OPERATION_TEXT:Record<string,string>={'commitment.fulfil':'履行承诺','commitment.cancel':'取消承诺','commitment.extend':'延期承诺','message.send':'发送消息','outreach.send':'主动联系'};
export const ITEM_STATE_TEXT:Record<string,string>={...INTENT_STATE_TEXT,succeeded:'已成功',pending:'待处理',approved:'已批准',rejected:'已拒绝',cancelled:'已取消',queued:'排队中',open:'进行中'};
export const PURPOSE_TEXT:Record<string,string>={reminder:'提醒',follow_up:'跟进',renewal:'续约',care:'关怀'};
export const label=(map:Record<string,string>,code:string|null)=>code===null?'—':map[code]??code;
export const KIND_TEXT:Record<EvidenceKind,string>={claim:'事实（Claim）',commitment:'承诺',action:'Action',outreach:'主动触发',candidate:'候选'};
const CLAIM_STATE_TEXT={active:'有效',corrected:'已更正',superseded:'已取代'} as const;
const QUOTE_TEXT:Record<EvidenceClaim['source']['quote_status'],string>={ok:'',reviewer_only:'原文只对审核人和有知识只读权限的成员显示',
  restricted:'原文受限（缺少该消息的读取权限）',corrected:'已更正：原文不再显示，见更正后的 Claim',superseded:'已取代：原文不再显示，见取代它的 Claim',none:'没有原文'};
const EPISTEMIC_TEXT:Record<string,string>={user_statement:'客户陈述',verified_fact:'已核实事实',preference:'偏好',constraint:'约束',intent:'意图',need_problem:'需求 / 问题',
  commitment:'承诺',hypothesis:'推测',correction:'更正'};
const COMMITMENT_TEXT:Record<string,string>={...COMMITMENT_STATUS_TEXT};
const CANDIDATE_TEXT:Record<string,string>={proposal:'本体更新提案',definition:'候选定义'};
/** Why a group shows fewer than its total: said as what it is (L6: invisible is a permission, not lost data). */
export function groupNote(g:EvidenceGroup):string|null{
  const parts:string[]=[];
  if(g.restricted_reason==='owner_only'&&g.invisible)parts.push(`仅负责人可见 ${g.invisible} 个`);
  else if(g.invisible)parts.push(g.kind==='claim'?`不可见 ${g.invisible} 个：涉及你没有读取权限的属性（或其属性组受限），不是数据丢失`
    :g.kind==='commitment'||g.kind==='candidate'?`不可见 ${g.invisible} 个：来源 Claim 涉及你没有读取权限的属性，不是数据丢失`:`不可见 ${g.invisible} 个：你的角色不能读取`);
  if(g.erased)parts.push(`已删除 ${g.erased} 个`);  // a number for the owner only; null is never said
  if(g.collapsed)parts.push(`另有 ${g.collapsed} 个未载入`);
  return parts.length?parts.join(' · '):null;
}
export function nodeId(kind:EvidenceKind,item:EvidenceGroup['items'][number]):string{
  const key=kind==='claim'?(item as EvidenceClaim).claim_id:kind==='commitment'?(item as EvidenceCommitment).commitment_id
    :kind==='candidate'?(item as EvidenceCandidate).candidate_id:(item as EvidenceAction|EvidenceOutreach).intent_id;
  return `${kind}:${key}`;
}
/** Claims recorded more than once before write-time dedupe are folded for display only; the server keeps every record. */
export function duplicates(kind:EvidenceKind,item:EvidenceGroup['items'][number]):number{return kind==='claim'?(item as EvidenceClaim).duplicate_count:1;}
export const duplicateHint=(n:number)=>`同一说法被记录了 ${n} 次，已合并显示；原始记录保留为证据`;
function DuplicateMark({n}:{n:number}){return n>1?<span className="dup-mark" title={duplicateHint(n)} aria-label={duplicateHint(n)}>×{n}</span>:null;}
function nodeLabel(kind:EvidenceKind,item:EvidenceGroup['items'][number]):string{
  switch(kind){
    case 'claim':{const c=item as EvidenceClaim;return c.state!=='active'?CLAIM_STATE_TEXT[c.state]:c.predicate??EPISTEMIC_TEXT[c.epistemic_kind]??c.epistemic_kind;}
    case 'commitment':return label(COMMITMENT_TEXT,(item as EvidenceCommitment).status);
    case 'action':return label(OPERATION_TEXT,(item as EvidenceAction).operation);
    case 'outreach':return label(PURPOSE_TEXT,(item as EvidenceOutreach).purpose);
    case 'candidate':return CANDIDATE_TEXT[(item as EvidenceCandidate).candidate_kind];
  }
}

/** One page of a group appended to what is shown (cursor ?kind=&after=); edges come only between listed ids. */
function merge(base:EvidenceGraph,more:Partial<Record<EvidenceKind,EvidenceGraph>>):EvidenceGraph{
  const groups=base.groups.map(g=>{const page=more[g.kind]?.groups.find(x=>x.kind===g.kind);
    return page?{...g,items:[...g.items,...page.items] as never,next_cursor:page.next_cursor,collapsed:page.collapsed}:g;});
  const edges=[...base.edges];for(const p of Object.values(more))for(const e of p?.edges??[])if(!edges.some(x=>x.source===e.source&&x.target===e.target&&x.rel===e.rel))edges.push(e);
  return {...base,groups,edges};
}

const RADIUS=210,CENTER={x:330,y:250},DRAWN=8;
/** Five fixed sectors around the consumer, each group's first DRAWN nodes on its arc; the rest are in the table. Deterministic. */
export function radialLayout(graph:EvidenceGraph){
  const nodes:{id:string;kind:EvidenceKind;label:string;dup:number;x:number;y:number}[]=[];
  graph.groups.forEach((g,gi)=>{const centre=-Math.PI/2+gi*2*Math.PI/5;const shown=g.items.slice(0,DRAWN);
    shown.forEach((item,i)=>{const a=centre+(shown.length>1?(i/(shown.length-1)-0.5)*(Math.PI/3):0);const r=RADIUS-(i%2)*48;
      nodes.push({id:nodeId(g.kind,item),kind:g.kind,label:nodeLabel(g.kind,item),dup:duplicates(g.kind,item),x:Math.round(CENTER.x+r*Math.cos(a)),y:Math.round(CENTER.y+r*Math.sin(a))});});});
  const at=new Map(nodes.map(n=>[n.id,n]));
  const edges=graph.edges.filter(e=>at.has(e.source)&&at.has(e.target));
  const sectors=graph.groups.map((g,gi)=>{const a=-Math.PI/2+gi*2*Math.PI/5;return {kind:g.kind,x:Math.round(CENTER.x+(RADIUS+62)*Math.cos(a)),y:Math.round(CENTER.y+(RADIUS+62)*Math.sin(a)),shown:Math.min(g.items.length,DRAWN),total:g.total};});
  return {nodes,edges,sectors,at};
}
const KIND_HUE:Record<EvidenceKind,number>={claim:1,commitment:2,action:3,outreach:4,candidate:5};

function EvidenceSvg({graph,selected,onSelect}:{graph:EvidenceGraph;selected:string|null;onSelect:(id:string)=>void}){
  const {nodes,edges,sectors,at}=radialLayout(graph);
  return <svg className="evidence-graph" viewBox="0 0 660 520" role="group" aria-label={`客户关系与证据图：${nodes.length} 个节点，${edges.length} 条关系`} data-nodes={nodes.length} data-edges={edges.length}>
    {nodes.map(n=><line key={'c'+n.id} x1={CENTER.x} y1={CENTER.y} x2={n.x} y2={n.y} className="evidence-spoke"/>)}
    {edges.map(e=>{const a=at.get(e.source)!,b=at.get(e.target)!;return <line key={e.source+e.target+e.rel} x1={a.x} y1={a.y} x2={b.x} y2={b.y} className="evidence-edge" data-rel={e.rel}/>;})}
    <g className="evidence-center"><circle cx={CENTER.x} cy={CENTER.y} r={34}/><text x={CENTER.x} y={CENTER.y+5} textAnchor="middle">Consumer</text></g>
    {sectors.map(s=><text key={s.kind} x={s.x} y={s.y} textAnchor="middle" className="evidence-sector">{KIND_TEXT[s.kind]} {s.shown}/{s.total}</text>)}
    {nodes.map(n=><g key={n.id} role="button" tabIndex={0} data-node={n.id} aria-pressed={selected===n.id} aria-label={`${KIND_TEXT[n.kind]}：${n.label}${n.dup>1?'，'+duplicateHint(n.dup):''}`}
      className={'evidence-node'+(selected===n.id?' selected':'')} style={{['--node-hue' as string]:`var(--hue-${KIND_HUE[n.kind]})`}}
      onClick={()=>onSelect(n.id)} onKeyDown={e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();onSelect(n.id);}}}>
      <rect x={n.x-58} y={n.y-15} width={116} height={30} rx={6}/><text x={n.x} y={n.y+5} textAnchor="middle">{n.label.length>9?n.label.slice(0,8)+'…':n.label}</text>
      {n.dup>1?<g className="dup-badge" data-dup={n.dup}><title>{duplicateHint(n.dup)}</title><rect x={n.x+38} y={n.y-24} width={30} height={16} rx={8}/><text x={n.x+53} y={n.y-12} textAnchor="middle">×{n.dup}</text></g>:null}</g>)}
  </svg>;
}

/** The side panel: the evidence chain of the chosen node, from the fields the server returned (nothing inferred). */
function EvidencePanel({graph,id,onSelect,onClose}:{graph:EvidenceGraph;id:string;onSelect:(id:string)=>void;onClose:()=>void}){
  const [kind]=id.split(':') as [EvidenceKind];const g=graph.groups.find(x=>x.kind===kind);const item=g?.items.find(x=>nodeId(kind,x)===id);
  if(!g||!item)return null;
  const listed=(ref:string)=>graph.groups.some(x=>x.items.some(i=>nodeId(x.kind,i)===ref));
  const link=(ref:string)=>listed(ref)?<button type="button" className="link" onClick={()=>onSelect(ref)}>{shorten(ref.split(':')[1])}</button>:null;
  let rows:{key:string;value:ReactNode}[]=[];
  if(kind==='claim'){const c=item as EvidenceClaim;rows=[{key:'状态',value:CLAIM_STATE_TEXT[c.state]},...(c.duplicate_count>1?[{key:'重复记录',value:<><DuplicateMark n={c.duplicate_count}/> <span className="note">{duplicateHint(c.duplicate_count)}</span></>}]:[]),{key:'类型',value:label(EPISTEMIC_TEXT,c.epistemic_kind)},
    ...(c.predicate!==null?[{key:'内容',value:c.predicate}]:[]),{key:'来源',value:<>原文第 {c.source.sequence??'—'} 轮 · {c.source.speaker==='consumer'?'客户说的':'AI 说的'}</>},
    {key:'原文片段',value:c.source.quote_status==='ok'&&c.source.quote?<q>{c.source.quote}</q>:<span className="note">{QUOTE_TEXT[c.source.quote_status]}</span>},
    {key:'置信度',value:c.confidence===null?'—':c.confidence.toFixed(2)},{key:'解析',value:c.resolution_state},
    ...(c.successor_claim_ids.length?[{key:'更正 / 取代为',value:<>{c.successor_claim_ids.map(s=><span key={s}>{link('claim:'+s)} </span>)}</>}]:[])];}
  if(kind==='commitment'){const c=item as EvidenceCommitment;rows=[{key:'状态',value:label(COMMITMENT_TEXT,c.status)},{key:'截止',value:c.due_at?<Time value={c.due_at}/>:'无期限'},
    {key:'如何推导',value:c.origin==='claim'?(c.claim_id?<>由 Claim {link('claim:'+c.claim_id)} 推导</>:'由 Claim 推导（来源未显示）'):'由延期产生'},
    ...(c.replaces_commitment_id?[{key:'取代',value:<IdText kind="承诺" value={c.replaces_commitment_id}/>}]:[]),{key:'修订',value:`r${c.revision}`}];}
  if(kind==='action'){const a=item as EvidenceAction;rows=[{key:'操作',value:label(OPERATION_TEXT,a.operation)},{key:'状态',value:label(ITEM_STATE_TEXT,a.state)},{key:'时间',value:<Time value={a.created_at}/>}];}
  if(kind==='outreach'){const o=item as EvidenceOutreach;rows=[{key:'目的',value:label(PURPOSE_TEXT,o.purpose)},{key:'状态',value:label(ITEM_STATE_TEXT,o.state)},{key:'时间',value:<Time value={o.created_at}/>}];}
  if(kind==='candidate'){const c=item as EvidenceCandidate;rows=[{key:'类型',value:CANDIDATE_TEXT[c.candidate_kind]},{key:'状态',value:c.status},
    {key:'为什么还是候选',value:c.match_decision||c.match_reason_code?<>{c.match_decision??''}{c.match_reason_code?<> · <code>{c.match_reason_code}</code></>:null}</>:'没有匹配结论'},
    ...(c.dependent_claim_ids.length?[{key:'依据的 Claim',value:<>{c.dependent_claim_ids.map(s=><span key={s}>{link('claim:'+s)} </span>)}</>}]:[])];}
  const incoming=graph.edges.filter(e=>e.target===id&&listed(e.source));
  return <aside className="evidence-panel" aria-label={`${KIND_TEXT[kind]} 证据`}>
    <div className="card-head"><h3>{KIND_TEXT[kind]} <IdText value={id.split(':')[1]}/></h3>
      <button type="button" className="btn-secondary panel-close" onClick={onClose}>关闭</button></div>
    <KeyValue label="证据链" items={rows}/>
    {incoming.length?<p className="note">被引用：{incoming.map(e=><span key={e.source}>{link(e.source)} </span>)}</p>:null}
  </aside>;
}

export function EvidenceSection({consumerId}:{consumerId:string}){
  const base=useQuery({queryKey:['workbench','evidence-graph',consumerId],queryFn:()=>readEvidenceGraph(consumerId),retry:false});
  const [more,setMore]=useState<Partial<Record<EvidenceKind,EvidenceGraph>>>({});
  const [graphView,setGraphView]=useState(true);const [selected,setSelected]=useState<string|null>(null);
  const [loading,setLoading]=useState<EvidenceKind|null>(null);
  const head=<h2 id="consumer-evidence">关系与证据</h2>;
  if(base.isPending)return <section className="detail-section" aria-labelledby="consumer-evidence">{head}<EmptyState kind="loading" subject="关系与证据" compact/></section>;
  if(base.isError){const k=kindOf(base.error);return <section className="detail-section" aria-labelledby="consumer-evidence">{head}
    {k==='not_found'?<p className="note">关系与证据只在真实世界提供；当前客户不在真实世界、正在删除或不存在。</p>
      :<EmptyState kind={k==='forbidden'?'forbidden':'unavailable'} subject="关系与证据" onRetry={()=>void base.refetch()} compact/>}</section>;}
  const graph=merge(base.data,more);
  const next=async(g:EvidenceGroup)=>{if(!g.next_cursor)return;setLoading(g.kind);
    try{const page=await readEvidenceGraph(consumerId,{kind:g.kind,after:g.next_cursor});setMore(m=>({...m,[g.kind]:m[g.kind]?merge(m[g.kind]!,{[g.kind]:page}):page}));}finally{setLoading(null);}};
  return <section className="detail-section evidence-section" aria-labelledby="consumer-evidence">{head}
    <div className="segmented" role="group" aria-label="查看方式">
      {([[true,'图'],[false,'表格']] as const).map(([v,t])=><button key={t} type="button" aria-pressed={graphView===v} onClick={()=>setGraphView(v)}>{t}</button>)}</div>
    <ul className="evidence-groups">{graph.groups.map(g=>{const note=groupNote(g);return <li key={g.kind} data-kind={g.kind}>
      <strong>{KIND_TEXT[g.kind]}</strong> 共 {g.total} 个 · 显示 {g.items.length} 个{note?<span className="note"> · {note}</span>:null}
      {g.kind==='candidate'&&g.total===0?<span className="note"> · 还没有候选：提取的事实尚未产生本体更新提案</span>:null}
      {g.next_cursor?<button type="button" className="btn-secondary" disabled={loading===g.kind} onClick={()=>void next(g)}>{loading===g.kind?'正在载入…':'载入更多'}</button>:null}</li>;})}</ul>
    <div className={'evidence-body'+(selected?' with-aside':'')}>
      {graphView?<EvidenceSvg graph={graph} selected={selected} onSelect={setSelected}/>
        :<DataTable label="关系与证据" rows={graph.groups.flatMap(g=>g.items.map(item=>({id:nodeId(g.kind,item),kind:g.kind,label:nodeLabel(g.kind,item),dup:duplicates(g.kind,item)})))} rowKey={r=>r.id}
          rowClass={r=>r.id===selected?'row-selected':undefined} columns={[{label:'类别',cell:r=>KIND_TEXT[r.kind]},
            {label:'节点',main:true,cell:r=><><button type="button" className="link" aria-pressed={r.id===selected} onClick={()=>setSelected(r.id)}>{r.label}</button>{r.dup>1?' ':null}<DuplicateMark n={r.dup}/></>},
            {label:'标识',cell:r=><IdText value={r.id.split(':')[1]}/>}]}/>}
      {selected?<EvidencePanel graph={graph} id={selected} onSelect={setSelected} onClose={()=>setSelected(null)}/>:null}
    </div>
  </section>;
}
