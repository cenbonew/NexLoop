import {useEffect,useState,type ReactNode} from 'react';
import {keepPreviousData,useInfiniteQuery,useQuery} from '@tanstack/react-query';
import {COMMITMENT_STATUS_TEXT,DISPLAY_NOTE,STATE_TEXT,consumerAlias,consumerLabel,kindOf,readConsumer,readConsumerActions,readConsumerCommitments,readConsumerHistory,readConsumers,readConversation,RESTRICTED_TEXT,
  type ConsumerQuery,type DisplayName,type HistoryItem} from '../api';
import type {Context} from '../WorkbenchApp';
import type {Query} from '../route';
import {Page,Short} from './Page';
import {CopyId,Keyword,Pager,relative} from '../ui';
import {commitmentSummary} from './Commitments';
import {DataTable} from '../DataTable';
import {Operations} from './PageOps';

/** The customer's readable name, or the stable non-identifying alias with the one reason no name is shown (F04: 受限 / 未登记 / 加载失败;
 * a restriction never says whether a name exists). `null` (an older server sent no name state): the alias alone. */
export function ConsumerName({consumerId,display}:{consumerId:string;display:DisplayName|null}){
  return display?.status==='ok'?<span className="consumer-name" data-name-state="ok">{display.name}</span>
    :<span className={'consumer-name consumer-alias'+(display?.status==='failed'?' consumer-failed':'')} data-name-state={display?.status??'unknown'}>{consumerAlias(consumerId)}
      {display?<small>（{DISPLAY_NOTE[display.status]}）</small>:null}</span>;
}
/** A PostgreSQL or ISO timestamp as Beijing date and time plus the relative value (walkthrough F15: one time zone everywhere, labelled
 * once in the workbench header); anything unparsable is shown as given (never hidden). */
export function when(value:string|null,timeZone='Asia/Shanghai'):string{
  if(!value)return '—';
  const iso=value.replace(' ','T').replace(/(\.\d{3})\d+/,'$1').replace(/([+-]\d{2})$/,'$1:00');
  const d=new Date(iso);if(Number.isNaN(d.getTime()))return value;
  const rel=relative(value);
  return d.toLocaleString('zh-CN',{hour12:false,timeZone})+(rel?`（${rel}）`:'');
}
/** Beijing time, as the owner reads it whatever the member's machine is set to. */
export const beijing=(value:string|null)=>when(value,'Asia/Shanghai');
const senderText=(m:{direction:string;sender_kind:string})=>m.direction==='inbound'?'顾客':m.sender_kind==='agent'?'Agent':m.sender_kind==='human_takeover'?'人工客服':'企业';

/** "客户近期往来" (owner feedback 2026-10-18): what this customer said and was told across all of their conversations, newest first.
 * Collapsed it shows the latest exchange; expanded the latest 10, and "查看更早" pages back 10 at a time (cursor). Bodies come through
 * the per-message READ (restricted ones are labelled), so a member sees no more than the conversation page would show. */
/** F10: an earlier message as a 40-character summary first; the whole text on request, without moving the reader. */
function HistoryBody({body}:{body:string}){
  const [full,setFull]=useState(false);const long=body.length>40;
  return <p className="history-snippet" data-full={full||!long||undefined}>{full||!long?body:body.slice(0,40)+'…'}
    {long?<button type="button" className="history-expand" aria-expanded={full} onClick={()=>setFull(!full)}>{full?'收起':'全文'}</button>:null}</p>;
}

export function CustomerHistory({consumerId,currentConversationId}:{consumerId:string;currentConversationId?:string}){
  const [open,setOpen]=useState(false);
  const history=useInfiniteQuery({queryKey:['workbench','consumer-history',consumerId],initialPageParam:'',
    queryFn:({pageParam})=>readConsumerHistory(consumerId,pageParam||undefined),getNextPageParam:last=>last.next_cursor??undefined});
  const commitments=useQuery({queryKey:['workbench','consumer-commitments',consumerId],queryFn:()=>readConsumerCommitments(consumerId),enabled:open});
  const actions=useQuery({queryKey:['workbench','consumer-actions',consumerId],queryFn:()=>readConsumerActions(consumerId),enabled:open});
  if(history.isPending)return <section className="customer-history" aria-label="客户近期往来"><p role="status">{STATE_TEXT.loading}</p></section>;
  if(history.isError)return <section className="customer-history" aria-label="客户近期往来"><p className="note" role="status">客户近期往来暂不可用：{STATE_TEXT[kindOf(history.error)]}</p></section>;
  const items:HistoryItem[]=history.data.pages.flatMap(p=>p.items);const total=history.data.pages[0]?.conversations??0;
  const shown=open?items:items.slice(0,1);
  return <section className="customer-history" aria-label="客户近期往来">
    <header className="customer-history-head"><h2>客户近期往来</h2><span className="note">{total} 个会话 · 按时间倒序 · 北京时间</span>
      {items.length>1||history.hasNextPage?<button type="button" className="history-toggle" aria-expanded={open} onClick={()=>setOpen(!open)}>{open?'收起':'展开最近 10 条'}</button>:null}</header>
    {items.length?<ol className="history-list">{shown.map(m=><li key={m.id} className={'history-item history-'+m.direction}>
      <p className="meta"><span className={'history-conv'+(m.conversation_id===currentConversationId?' history-current':'')}>{m.conversation_id===currentConversationId?'本会话':<>会话 <Short value={m.conversation_id}/></>}</span>
        · #{m.sequence} · {senderText(m)}{m.content.status==='ok'&&m.content.actor&&m.direction==='outbound'?`（${m.content.actor}）`:''} · {beijing(m.accepted_at)}</p>
      {m.content.status==='ok'?<HistoryBody body={m.content.body??''}/>:<p className="note">{RESTRICTED_TEXT}</p>}</li>)}</ol>
      :<p className="note">这位客户还没有消息。</p>}
    {open&&history.hasNextPage?<button type="button" className="history-more" disabled={history.isFetchingNextPage} onClick={()=>void history.fetchNextPage()}>
      {history.isFetchingNextPage?'正在加载…':'查看更早'}</button>:null}
    {open&&!history.hasNextPage&&items.length>1?<p className="note">已显示全部 {items.length} 条。</p>:null}
    {open?<div className="history-summary">
      <div><h3>承诺</h3>{commitments.isPending?<p role="status">{STATE_TEXT.loading}</p>:commitments.isError?<p className="note">{STATE_TEXT[kindOf(commitments.error)]}</p>
        :commitments.data.commitments.length?<ul>{commitments.data.commitments.slice(0,5).map(c=><li key={c.commitment_id}><span className="badge">{COMMITMENT_STATUS_TEXT[c.status]??c.status}</span>
          {commitmentSummary(c)}</li>)}</ul>:<p className="note">暂无承诺。</p>}</div>
      <div><h3>最近的订单与 Action</h3>{actions.isPending?<p role="status">{STATE_TEXT.loading}</p>:actions.isError?<p className="note">{STATE_TEXT[kindOf(actions.error)]}</p>
        :actions.data.intents.length?<ul>{actions.data.intents.slice(0,5).map(a=><li key={a.intent_id}><span className="badge">{a.state}</span>{a.action_name.replace(/^nexloop\./,'')} · {beijing(a.created_at)}</li>)}</ul>
        :<p className="note">暂无。</p>}</div>
    </div>:null}
  </section>;
}

const PAGE_SIZE=50;
const bool=(v:string|undefined)=>v===undefined?undefined:v==='true';
/** F09: filters and sort on the server (the list's own counts), keyword within the page, total and offset pages; all in the URL. */
export function Consumers({actions,onOpen,onCommitments,query={},onQuery=()=>{}}:{actions?:ReactNode;onOpen:(consumerId:string)=>void;onCommitments?:(consumerId:string)=>void;
  query?:Query;onQuery?:(query:Query)=>void}){
  const q:ConsumerQuery={restricted:bool(query.restricted),has_open_commitments:bool(query.open),sort:(query.sort as ConsumerQuery['sort'])??'id',
    order:query.order as ConsumerQuery['order'],cursor:query.page};
  const start=Number(query.page??0);
  const data=useQuery({queryKey:['workbench','consumers',q],queryFn:()=>readConsumers(q,PAGE_SIZE),placeholderData:keepPreviousData});
  const set=(changes:Query)=>{const next={...query,...changes};for(const k of Object.keys(next))if(next[k]==='')delete next[k];
    if(!('page' in changes))delete next.page;onQuery(next);};
  const keyword=query.q??'';
  const choose=(name:string,value:string)=>set({[name]:value});
  return <Page title="消费者" query={data} actions={actions} toolbar={<div className="list-tools" role="search" aria-label="筛选消费者">
      <label>联系限制<select value={query.restricted??''} onChange={e=>choose('restricted',e.target.value)}><option value="">全部</option><option value="true">受限</option><option value="false">未受限</option></select></label>
      <label>未结束承诺<select value={query.open??''} onChange={e=>choose('open',e.target.value)}><option value="">全部</option><option value="true">有</option><option value="false">没有</option></select></label>
      <label>排序<select value={(query.sort??'id')+':'+(query.order??'asc')} onChange={e=>{const [sort,order]=e.target.value.split(':');set({sort,order});}}>
        <option value="id:asc">按 ID</option><option value="open_commitments:desc">未结束承诺最多</option><option value="conversations:desc">会话最多</option><option value="active_plans:desc">活跃计划最多</option></select></label>
      <Keyword label="关键字" value={keyword} onChange={v=>set({q:v,page:query.page??''})}/></div>}
    empty={d=>d.items.length===0&&!start&&query.restricted===undefined&&query.open===undefined}>{page=>{
    const k=keyword.trim().toLowerCase();
    const rows=k?page.items.filter(c=>(consumerLabel(c.consumer_id,c.display)+' '+c.consumer_id).toLowerCase().includes(k)):page.items;
    return <>
    {rows.length?<DataTable label="消费者" rows={rows} rowKey={c=>c.consumer_id} columns={[
      {label:'客户',main:true,cell:c=><button type="button" className="link" onClick={()=>onOpen(c.consumer_id)}><ConsumerName consumerId={c.consumer_id} display={c.display}/></button>},
      {label:'ID',cell:c=><CopyId value={c.consumer_id}/>},
      {label:'联系限制',cell:c=>c.restricted?'受限':'—'},
      {label:'未结束承诺',num:true,cell:c=>c.open_commitments&&onCommitments?<button type="button" className="link-count" aria-label={`查看 ${consumerLabel(c.consumer_id,c.display)} 的 ${c.open_commitments} 条未结束承诺`}
        onClick={()=>onCommitments(c.consumer_id)}>{c.open_commitments}</button>:c.open_commitments},
      {label:'活跃计划',num:true,cell:c=>c.active_plans},{label:'会话',num:true,cell:c=>c.conversations}]}/>
      :<p className="note" role="status">{k?'当前页没有匹配的客户；关键字只在当前页内查找。':'没有符合筛选条件的客户。'}</p>}
    {k&&rows.length?<p className="note">关键字只在当前页内查找（共 {page.items.length} 条中匹配 {rows.length} 条）。</p>:null}
    <Pager total={page.total} start={start} size={PAGE_SIZE} shown={page.items.length} onPage={n=>set({page:n?String(n):''})}/>
  </>;}}</Page>;
}

/** F10: who this is and what is open for them, each count leading to the matching list. */
export function ConsumerHeader({consumerId,display,restricted,commitments,conversations,onCommitments}:{consumerId:string;display:DisplayName|null;restricted?:boolean;
  commitments?:number;conversations?:number;onCommitments?:()=>void}){
  return <div className="consumer-header" aria-label="客户摘要"><ConsumerName consumerId={consumerId} display={display}/><CopyId value={consumerId}/>
    <span className="counts">{restricted!==undefined?<span>联系限制：{restricted?'受限':'无'}</span>:null}
      {commitments!==undefined?(commitments&&onCommitments?<button type="button" className="link-count" onClick={onCommitments}>承诺 {commitments} 条</button>:<span>承诺 {commitments} 条</span>):null}
      {conversations!==undefined?<span>会话 {conversations} 个</span>:null}</span></div>;
}

export function ConsumerDetail({consumerId,actions,onConversation,onCommitments,onContext}:{consumerId:string;actions?:ReactNode;onConversation:(conversationId:string)=>void;
  onCommitments?:(consumerId:string)=>void;onContext?:(context:Context)=>void}){
  const query=useQuery({queryKey:['workbench','consumer',consumerId],queryFn:()=>readConsumer(consumerId)});
  const d=query.data;
  useEffect(()=>{if(d)onContext?.({consumerId:d.consumer_id,label:consumerLabel(d.consumer_id,d.display)});},[d,onContext]);
  return <Page title="消费者详情" query={query}
    lead={d?<><ConsumerHeader consumerId={d.consumer_id} display={d.display} restricted={!!d.restriction?.active} commitments={d.commitments.length}
      conversations={d.conversations.length} onCommitments={onCommitments?()=>onCommitments(d.consumer_id):undefined}/>{d.paused?'主动联系已暂停':''}</>:null}>{data=><>
    <h2>联系限制</h2>
    {data.restriction?<p className={data.restriction.active?'warning':'note'}>{data.restriction.active?'受限':'已解除'} · 规则 {data.restriction.rule_id} · 控制 revision {data.restriction.control_revision} · {when(data.restriction.restricted_at)}</p>:<p className="note">没有联系限制。</p>}
    <h2>属性</h2>
    {data.properties.status==='ok'?(Object.keys(data.properties.values).length?<dl>{Object.entries(data.properties.values).map(([k,v])=><div key={k}><dt>{k}</dt><dd>{typeof v==='string'?v:JSON.stringify(v)}</dd></div>)}</dl>:<p className="note">没有可读属性。</p>)
      :<p className="note" role="status">无属性读取权限：字段与证据不显示（AT-003）。</p>}
    {data.properties.withheld.length?<p className="note">负责人标为受限的属性不显示：{data.properties.withheld.join('、')}</p>:null}
    <h2>会话</h2>
    {data.conversations.length?<ul className="conversation-list">{[...data.conversations].sort((a,b)=>(b.last_message_at??'').localeCompare(a.last_message_at??'')).map((c,i)=>
      <li key={c.conversation_id}><button type="button" className="conversation-item" onClick={()=>onConversation(c.conversation_id)}>
        <span className="conversation-time">{when(c.last_message_at)}{i===0&&c.last_message_at?<span className="badge-latest">最新</span>:null}</span>
        <span className="conversation-meta">会话 <Short value={c.conversation_id}/> · {c.last_sequence} 条消息</span></button></li>)}</ul>:<p className="note">暂无会话。</p>}
    <h2>承诺</h2>
    {data.commitments.length?<p>{data.commitments.length} 条 · {onCommitments?<button type="button" className="link-count" onClick={()=>onCommitments(data.consumer_id)}>查看这位客户的承诺</button>:'见“承诺 / 交付”'}</p>
      :<p className="note">暂无承诺。</p>}
    <Operations actions={actions} title="对这位客户的操作"/>
  </>}</Page>;
}

export function Conversation({conversationId,actions,onContext}:{conversationId:string;actions?:ReactNode;onContext?:(context:Context)=>void}){
  const query=useQuery({queryKey:['workbench','conversation',conversationId],queryFn:()=>readConversation(conversationId)});
  const owner=useQuery({queryKey:['workbench','consumer',query.data?.consumer_id],queryFn:()=>readConsumer(query.data!.consumer_id),enabled:!!query.data});
  useEffect(()=>{const c=query.data?.consumer_id;if(c)onContext?.({consumerId:c,label:owner.data?consumerLabel(c,owner.data.display):consumerAlias(c)});},[query.data,owner.data,onContext]);
  return <Page title="会话" query={query} actions={actions}
    lead={query.data?<ConsumerHeader consumerId={query.data.consumer_id} display={owner.data?.display??null} restricted={owner.data?!!owner.data.restriction?.active:undefined}
      commitments={owner.data?.commitments.length} conversations={owner.data?.conversations.length}/>:null}>{data=><>
    {/* F10: the current conversation first; earlier exchanges in a side column (after it below 1024 px), summaries first, so opening
        or paging the history never moves what is being read. */}
    <div className="conversation-layout"><div className="conversation-main">
    <h2>本会话消息</h2>
    {data.messages.length?<ol className="workbench-messages">
    {data.messages.map(m=><li key={m.id} className={'message-'+m.direction}>
      <p className="meta">#{m.sequence} · {m.direction==='outbound'?(m.sender_kind==='agent'?'企业 Agent 回复':m.sender_kind==='human_takeover'?`人工客服回复${m.content.status==='ok'&&m.content.actor?`（${m.content.actor}）`:''}`:'企业回复'):'顾客'} · {when(m.accepted_at)}
        {m.provider?` · 渠道 ${m.provider.namespace}${m.provider.sequence!==null?` 序号 ${m.provider.sequence}`:''}${m.provider.trust==='client'?'（客户端陈述）':''}${m.provider.skewed?'（时间偏差）':''}`:''}
        {m.reply_to_message_id?<> · 回复 <Short value={m.reply_to_message_id}/></>:null}</p>
      {m.content.status==='ok'?<p>{m.content.body}</p>:<p className="note">{RESTRICTED_TEXT}</p>}
    </li>)}
  </ol>:<p className="note" role="status">{STATE_TEXT.empty}</p>}</div>
    <aside className="side-tabs" aria-label="历史往来"><CustomerHistory consumerId={data.consumer_id} currentConversationId={data.conversation_id}/></aside></div>
  </>}</Page>;
}
