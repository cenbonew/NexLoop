import {useState,type ReactNode} from 'react';
import {useInfiniteQuery,useQuery} from '@tanstack/react-query';
import {COMMITMENT_STATUS_TEXT,DISPLAY_NOTE,STATE_TEXT,consumerAlias,kindOf,readConsumer,readConsumerActions,readConsumerCommitments,readConsumerHistory,readConsumers,readConversation,RESTRICTED_TEXT,
  type DisplayName,type HistoryItem} from '../api';
import {Page,Short} from './Page';
import {DataTable} from '../DataTable';

/** The customer's readable name, or a non-identifying alias with the reason no name is shown (ADR-025). */
export function ConsumerName({consumerId,display}:{consumerId:string;display:DisplayName}){
  return display.status==='ok'?<span className="consumer-name">{display.name}</span>
    :<span className="consumer-name consumer-alias">{consumerAlias(consumerId)}<small>（{DISPLAY_NOTE[display.status]}）</small></span>;
}
/** A PostgreSQL or ISO timestamp as local date and time; anything unparsable is shown as given (never hidden). */
export function when(value:string|null,timeZone?:string):string{
  if(!value)return '—';
  const iso=value.replace(' ','T').replace(/(\.\d{3})\d+/,'$1').replace(/([+-]\d{2})$/,'$1:00');
  const d=new Date(iso);return Number.isNaN(d.getTime())?value:d.toLocaleString('zh-CN',{hour12:false,...(timeZone?{timeZone}:{})});
}
/** Beijing time, as the owner reads it whatever the member's machine is set to. */
export const beijing=(value:string|null)=>when(value,'Asia/Shanghai');
const senderText=(m:{direction:string;sender_kind:string})=>m.direction==='inbound'?'顾客':m.sender_kind==='agent'?'Agent':m.sender_kind==='human_takeover'?'人工客服':'企业';

/** "客户近期往来" (owner feedback 2026-10-18): what this customer said and was told across all of their conversations, newest first.
 * Collapsed it shows the latest exchange; expanded the latest 10, and "查看更早" pages back 10 at a time (cursor). Bodies come through
 * the per-message READ (restricted ones are labelled), so a member sees no more than the conversation page would show. */
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
      {m.content.status==='ok'?<p className="history-body">{m.content.body}</p>:<p className="note">{RESTRICTED_TEXT}</p>}</li>)}</ol>
      :<p className="note">这位客户还没有消息。</p>}
    {open&&history.hasNextPage?<button type="button" className="history-more" disabled={history.isFetchingNextPage} onClick={()=>void history.fetchNextPage()}>
      {history.isFetchingNextPage?'正在加载…':'查看更早'}</button>:null}
    {open&&!history.hasNextPage&&items.length>1?<p className="note">已显示全部 {items.length} 条。</p>:null}
    {open?<div className="history-summary">
      <div><h3>承诺</h3>{commitments.isPending?<p role="status">{STATE_TEXT.loading}</p>:commitments.isError?<p className="note">{STATE_TEXT[kindOf(commitments.error)]}</p>
        :commitments.data.commitments.length?<ul>{commitments.data.commitments.slice(0,5).map(c=><li key={c.commitment_id}><span className="badge">{COMMITMENT_STATUS_TEXT[c.status]??c.status}</span>
          {typeof c.properties.content==='string'?c.properties.content:c.quote??<Short value={c.commitment_id}/>}</li>)}</ul>:<p className="note">暂无承诺。</p>}</div>
      <div><h3>最近的订单与 Action</h3>{actions.isPending?<p role="status">{STATE_TEXT.loading}</p>:actions.isError?<p className="note">{STATE_TEXT[kindOf(actions.error)]}</p>
        :actions.data.intents.length?<ul>{actions.data.intents.slice(0,5).map(a=><li key={a.intent_id}><span className="badge">{a.state}</span>{a.action_name.replace(/^nexloop\./,'')} · {beijing(a.created_at)}</li>)}</ul>
        :<p className="note">暂无。</p>}</div>
    </div>:null}
  </section>;
}

export function Consumers({actions,onOpen}:{actions?:ReactNode;onOpen:(consumerId:string)=>void}){
  const [after,setAfter]=useState<string[]>([]);
  const cursor=after[after.length-1];
  const query=useQuery({queryKey:['workbench','consumers',cursor??''],queryFn:()=>readConsumers(cursor)});
  return <Page title="消费者" query={query} actions={actions} empty={d=>d.items.length===0&&!cursor}>{data=><>
    <DataTable label="消费者" rows={data.items} rowKey={c=>c.consumer_id} columns={[
      {label:'客户',main:true,cell:c=><button type="button" className="link" onClick={()=>onOpen(c.consumer_id)}><ConsumerName consumerId={c.consumer_id} display={c.display}/></button>},
      {label:'ID',cell:c=><Short value={c.consumer_id}/>},
      {label:'联系限制',cell:c=>c.restricted?'受限':'—'},
      {label:'未结束承诺',num:true,cell:c=>c.open_commitments},{label:'活跃计划',num:true,cell:c=>c.active_plans},{label:'会话',num:true,cell:c=>c.conversations}]}/>
    <div className="pager">{after.length?<button type="button" onClick={()=>setAfter(after.slice(0,-1))}>上一页</button>:null}
      {data.next_cursor?<button type="button" onClick={()=>setAfter([...after,data.next_cursor!])}>下一页</button>:null}</div>
  </>}</Page>;
}

export function ConsumerDetail({consumerId,actions,onConversation}:{consumerId:string;actions?:ReactNode;onConversation:(conversationId:string)=>void}){
  const query=useQuery({queryKey:['workbench','consumer',consumerId],queryFn:()=>readConsumer(consumerId)});
  const d=query.data;
  return <Page title="消费者详情" query={query} actions={actions}
    lead={d?<><ConsumerName consumerId={d.consumer_id} display={d.display}/> <Short value={d.consumer_id}/>{d.paused?' · 主动联系已暂停':''}</>:null}>{data=><>
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
    {data.commitments.length?<p>{data.commitments.length} 条，见“承诺 / 交付”。</p>:<p className="note">暂无承诺。</p>}
  </>}</Page>;
}

export function Conversation({conversationId,actions}:{conversationId:string;actions?:ReactNode}){
  const query=useQuery({queryKey:['workbench','conversation',conversationId],queryFn:()=>readConversation(conversationId)});
  return <Page title="会话" query={query} actions={actions}>{data=><>
    <CustomerHistory consumerId={data.consumer_id} currentConversationId={data.conversation_id}/>
    <h2>本会话消息</h2>
    {data.messages.length?<ol className="workbench-messages">
    {data.messages.map(m=><li key={m.id} className={'message-'+m.direction}>
      <p className="meta">#{m.sequence} · {m.direction==='outbound'?(m.sender_kind==='agent'?'企业 Agent 回复':m.sender_kind==='human_takeover'?`人工客服回复${m.content.status==='ok'&&m.content.actor?`（${m.content.actor}）`:''}`:'企业回复'):'顾客'} · {when(m.accepted_at)}
        {m.provider?` · 渠道 ${m.provider.namespace}${m.provider.sequence!==null?` 序号 ${m.provider.sequence}`:''}${m.provider.trust==='client'?'（客户端陈述）':''}${m.provider.skewed?'（时间偏差）':''}`:''}
        {m.reply_to_message_id?<> · 回复 <Short value={m.reply_to_message_id}/></>:null}</p>
      {m.content.status==='ok'?<p>{m.content.body}</p>:<p className="note">{RESTRICTED_TEXT}</p>}
    </li>)}
  </ol>:<p className="note" role="status">{STATE_TEXT.empty}</p>}
  </>}</Page>;
}
