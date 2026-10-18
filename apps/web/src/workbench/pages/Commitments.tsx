import {useEffect,type ReactNode} from 'react';
import {keepPreviousData,useQuery} from '@tanstack/react-query';
import {COMMITMENT_STATUS_TEXT,OPEN_STATUSES,RESTRICTED_TEXT,consumerAlias,consumerLabel,readCommitment,readCommitments,
  type CommitmentQuery,type CommitmentView,type EvidenceItem} from '../api';
import type {Context} from '../WorkbenchApp';
import type {Query} from '../route';
import {Page} from './Page';
import {CopyId,Empty,FilterChip,Keyword,Pager,StatusBadge,Tabs,commitmentTone} from '../ui';
import {ConsumerHeader,ConsumerName,when} from './Consumers';
import {DataTable} from '../DataTable';
import {Operations} from './PageOps';

/** The promised words: shown only with the source Message's READ while it exists. */
export function quoteText(view:CommitmentView):string{
  return view.quote_status==='ok'&&view.quote?view.quote:view.quote_status==='restricted'?RESTRICTED_TEXT:view.quote_status==='unavailable'?'来源消息已删除，原文不可用':'无来源消息';
}
/** F03: what was promised, in one line: the promised words (only what the reader may read, cut at 60 characters), else why not. */
export function commitmentSummary(view:CommitmentView):string{
  if(view.quote_status==='ok'&&view.quote){const q=view.quote.trim();return q.length>60?q.slice(0,60)+'…':q;}
  return view.quote_status==='restricted'?'内容受限':view.quote_status==='unavailable'?'来源消息已删除':'无原文';
}
/** Due date and precision (`due_precision`); a latest bound is a derived value, labelled as such; late is said. */
export function dueText(view:CommitmentView):string{
  const due=typeof view.properties.due_at==='string'?view.properties.due_at:null;
  if(!due)return '无期限';
  const text=view.properties.due_precision==='latest_bound'?`最晚界 ${when(due)}（推导值）`:when(due);
  return view.properties.late===true?text+' · 已逾期':text;
}
/** Who made the promise (D6: a member by a workbench alias, never the principal value). */
export function promiser(view:CommitmentView):string{
  const by=view.properties.made_by&&typeof view.properties.made_by==='object'?view.properties.made_by as Record<string,unknown>:{};
  const p=typeof by.sender_principal==='string'?by.sender_principal:'';
  return by.sender_kind==='agent'?'Agent':by.sender_kind==='human_takeover'||by.sender_kind==='staff'?`成员 ${p.slice(0,4)||'—'}`:by.sender_kind?'企业':'—';
}
/** D1: who acts next, derived from the object's status and exceptions only (no assignee field; nothing is written). */
export function nextStep(view:CommitmentView):string{
  if(!OPEN_STATUSES.includes(view.status))return '—';
  if(view.status==='breached'||view.exceptions.length)return '负责人处理';
  if(view.status==='conditional')return '等客户条件成立';
  return '系统跟进（承诺方）';
}

function Evidence({title,items,note}:{title:string;items:EvidenceItem[];note?:string}){
  return <div className="evidence-column"><h3>{title}</h3>{note?<p className="note">{note}</p>:null}
    {items.length?<ul>{items.map(x=><li key={x.kind+x.ref}>{x.kind} · {when(x.occurred_at)}</li>)}</ul>:<p className="note">暂无证据。</p>}</div>;
}

/** "到期到 <day>" is inclusive of that whole Beijing day: the server's bound is exclusive (due_at < due_before), so send the next day's
 * 00:00:00+08:00 — a commitment due at 23:59:59.5 that day is included. */
export function dueBefore(day:string):string{
  const [y,m,d]=day.split('-').map(Number);const next=new Date(Date.UTC(y,m-1,d+1));
  return next.toISOString().slice(0,10)+'T00:00:00+08:00';
}
const PAGE_SIZE=50;
type Group='open'|'ended'|'all';
/** F03/F05/F09/F10: the list in business words; tabs with counts; list, counts and exceptions from one server scope; filters, sort and
 * page in the URL; a customer filter shown as a removable chip (from the customer page or a commitment count). */
export function Commitments({actions,onOpen,onConsumer,query={},onQuery=()=>{}}:{actions?:ReactNode;onOpen:(commitmentId:string)=>void;onConsumer?:(consumerId:string)=>void;
  query?:Query;onQuery?:(query:Query)=>void}){
  const group=(query.status as Group)??'open';
  const q:CommitmentQuery={status_group:group,consumer_id:query.consumer,late:query.late===undefined?undefined:query.late==='true',
    due_after:query.due_from?query.due_from+'T00:00:00+08:00':undefined,due_before:query.due_to?dueBefore(query.due_to):undefined,
    sort:query.sort as CommitmentQuery['sort'],order:query.order as CommitmentQuery['order'],cursor:query.page};
  const start=Number(query.page??0);
  const data=useQuery({queryKey:['workbench','commitments',q],queryFn:()=>readCommitments(q,PAGE_SIZE),placeholderData:keepPreviousData});
  const set=(changes:Query)=>{const next={...query,...changes};for(const k of Object.keys(next))if(next[k]==='')delete next[k];
    if(!('page' in changes))delete next.page;onQuery(next);};
  const counts=data.data?.counts;
  const tab=(key:Group,label:string)=>({key,label:<>{label}{counts&&key!=='all'?<span className="tab-count">（{counts[key]}）</span>:null}</>});
  const customer=query.consumer?data.data?.commitments.find(c=>c.consumer_id===query.consumer):undefined;
  const keyword=query.q??'';
  return <Page title="承诺 / 交付" query={data} actions={actions} toolbar={<>
    <Tabs label="承诺范围" value={group} onChange={k=>set({status:k==='open'?'':k})} tabs={[tab('open','未结束'),tab('ended','已结束'),tab('all','全部')]}/>
    <div className="list-tools" role="search" aria-label="筛选承诺">
      {query.consumer?<FilterChip label={<>客户：{customer?consumerLabel(query.consumer,customer.consumer_display??{status:'missing'}):consumerAlias(query.consumer)}</>} onClear={()=>set({consumer:''})}/>:null}
      <label className="inline"><input type="checkbox" checked={query.late==='true'} onChange={e=>set({late:e.target.checked?'true':''})}/>只看已逾期</label>
      <label>到期从<input type="date" value={query.due_from??''} onChange={e=>set({due_from:e.target.value})}/></label>
      <label>到<input type="date" value={query.due_to??''} onChange={e=>set({due_to:e.target.value})}/></label>
      <label>排序<select value={(query.sort??'registered_at')+':'+(query.order??'asc')} onChange={e=>{const [sort,order]=e.target.value.split(':');set({sort,order});}}>
        <option value="registered_at:asc">登记时间（早→晚）</option><option value="registered_at:desc">登记时间（晚→早）</option>
        <option value="due_at:asc">到期时间（近→远）</option><option value="status:asc">状态</option></select></label>
      <Keyword label="关键字" value={keyword} onChange={v=>set({q:v,page:query.page??''})}/></div></>}>{page=>{
    const k=keyword.trim().toLowerCase();
    const rows=k?page.commitments.filter(c=>[commitmentSummary(c),c.consumer_display?consumerLabel(c.consumer_id,c.consumer_display):consumerAlias(c.consumer_id),
      c.commitment_id,COMMITMENT_STATUS_TEXT[c.status]??c.status].join(' ').toLowerCase().includes(k)):page.commitments;
    const filtered=!!(query.consumer||query.late||query.due_from||query.due_to||k);
    return <>
      {rows.length?<DataTable label="承诺" rows={rows} rowKey={c=>c.commitment_id} columns={[
        {label:'承诺',main:true,wide:true,cell:c=><span className="commitment-summary"><button type="button" className="link" onClick={()=>onOpen(c.commitment_id)}>{commitmentSummary(c)}</button>
          {typeof c.properties.condition==='string'&&c.properties.condition?<span className="commitment-condition"> 条件：{c.properties.condition}</span>:null}</span>},
        {label:'客户',cell:c=>onConsumer?<button type="button" className="link" onClick={()=>onConsumer(c.consumer_id)}><ConsumerName consumerId={c.consumer_id} display={c.consumer_display}/></button>
          :<ConsumerName consumerId={c.consumer_id} display={c.consumer_display}/>},
        {label:'期限',cell:dueText},
        {label:'状态',cell:c=><StatusBadge text={COMMITMENT_STATUS_TEXT[c.status]??'未知状态'} tone={commitmentTone(c.status)} code={c.status}/>},
        {label:'承诺方',cell:promiser},{label:'下一步',cell:nextStep},
        {label:'ID',cell:c=><CopyId value={c.commitment_id}/>}]}/>
        :k&&page.commitments.length?<p className="note" role="status">当前页没有匹配的承诺；关键字只在当前页内查找。</p>
        :<Empty kind="filtered" title={filtered?'没有符合筛选条件的承诺':group==='ended'?'没有已结束的承诺':group==='open'?'没有未结束的承诺':'没有承诺'}
          next={filtered?'清除筛选条件，或换一个范围。':group==='ended'?'切换到“未结束”查看进行中的承诺。':'切换到“已结束”查看历史承诺。'}/>}
      {k&&rows.length?<p className="note">关键字只在当前页内查找（当前页 {page.commitments.length} 条中匹配 {rows.length} 条）。</p>:null}
      <Pager total={page.total} start={start} size={PAGE_SIZE} shown={page.commitments.length} onPage={n=>set({page:n?String(n):''})}/>
      {/* F05: the exceptions of exactly this scope; the whole tenant's count separately, said as such. */}
      <h2>本范围的异常{page.counts?`（${page.counts.exceptions_in_scope}）`:''}</h2>
      {page.exceptions.length?<ul className="exceptions-scope">{page.exceptions.map(e=><li key={e.subject_ref+e.reason}>{e.reason} · {e.subject_ref.startsWith('commitment:')?<>承诺 <code title={e.subject_ref}>{e.subject_ref.slice(11,23)}…</code></>
        :'登记前'} · {when(e.raised_at)}</li>)}</ul>:<p className="note">当前范围内没有异常。</p>}
      {page.counts&&page.counts.exceptions_all!==page.counts.exceptions_in_scope?<details className="exceptions-all"><summary>全部承诺异常（{page.counts.exceptions_all}）</summary>
        <p className="note">包括其他范围（如已结束的承诺）的异常；切换到“全部”且不加筛选即可逐条查看。</p></details>:null}
    </>;}}</Page>;
}

export function CommitmentDetail({commitmentId,actions,onContext,onConsumer}:{commitmentId:string;actions?:ReactNode;onContext?:(context:Context)=>void;onConsumer?:(consumerId:string)=>void}){
  const query=useQuery({queryKey:['workbench','commitment',commitmentId],queryFn:()=>readCommitment(commitmentId)});
  const d=query.data;
  useEffect(()=>{if(d)onContext?.({consumerId:d.consumer_id,label:d.consumer_display?consumerLabel(d.consumer_id,d.consumer_display):consumerAlias(d.consumer_id)});},[d,onContext]);
  return <Page title="承诺详情" query={query} lead={d?<ConsumerHeader consumerId={d.consumer_id} display={d.consumer_display}/>:null}>{c=><>
    {onConsumer?<p><button type="button" className="link-count" onClick={()=>onConsumer(c.consumer_id)}>打开这位客户</button></p>:null}
    <p className="workbench-summary">承诺方：{promiser(c)} · 下一步：{nextStep(c)} · <CopyId value={c.commitment_id}/></p>
    <h2>承诺正文</h2>
    <blockquote>{quoteText(c)}</blockquote>
    <h2>当前状态</h2>
    <p className="workbench-summary" role="status">{COMMITMENT_STATUS_TEXT[c.status]??c.status}（来自承诺对象，界面不推断） · 期限：{dueText(c)}
      {c.exceptions.length?` · 异常：${c.exceptions.map(e=>e.reason).join('、')}`:''}</p>
    <h2>证据</h2>
    <div className="evidence-grid">
      <Evidence title="请求创建" items={c.evidence.requested}/>
      <Evidence title="已交付" items={c.evidence.delivered} note="消息已送达不代表承诺已兑现。"/>
      <Evidence title="客户确认" items={c.evidence.customer_confirmed}/>
      <div className="evidence-column"><h3>问题解决</h3><p className="note">不可用（v0.1 不推断问题是否解决）。</p></div>
    </div>
    <h2>事件</h2><ol>{c.events.map((e,i)=><li key={i}>{e.kind} · {when(e.recorded_at)}{e.principal_id?` · ${e.principal_id}`:''}</li>)}</ol>
    {c.exceptions.length?<><h2>异常</h2><ul>{c.exceptions.map(e=><li key={e.reason}>{e.reason} · {when(e.raised_at)}</li>)}</ul></>:null}
    <Operations actions={actions} title="对这条承诺的操作"/>
  </>}</Page>;
}
