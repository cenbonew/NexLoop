import {nativeMessage,type PendingNativeMessage} from './native-message';
import {useEffect,useState} from 'react';
import {MessageReceipt} from './MessageReceipt';
import {useMutation,useQuery,useQueryClient} from '@tanstack/react-query';
import {chatError,createConversation,listConversations,mergeCommitted,messagePresentation,readHandling,readMessages,sendMessage,subscribe,type Message} from './chat-api';
/** A conversation by its times (X4: first message, count); without them (an older server) by its place in the list. The customer sees
 * no identifier (UX audit B12, owner 2026-10-28). */
export function conversationLabel(index:number,_id:string,times?:{started_at?:string|null;message_count?:number}):string{
  if(times?.message_count===undefined)return `对话 ${index+1}`;
  if(!times.message_count&&!times.started_at)return '新对话（还没有消息）';
  if(!times.started_at||!times.message_count)return `对话 ${index+1}`;  // earliest messages cleared (retention, erasure): no start to name it by
  const d=new Date(times.started_at);
  if(Number.isNaN(d.getTime()))return `对话 ${index+1}`;
  const day=d.toLocaleString('zh-CN',{month:'long',day:'numeric',hour:'2-digit',minute:'2-digit',hour12:false});
  return `${day} 开始 · 共 ${times.message_count} 条`;
}
/** Local date and time, 24-hour, without seconds' fractions. */
export const localTime=(value:string)=>{const d=new Date(value);return Number.isNaN(d.getTime())?value:d.toLocaleString('zh-CN',{hour12:false});};
export function WebChat(){
  const cache=useQueryClient();const [selected,setSelected]=useState('');const [draft,setDraft]=useState('');const [streamUnavailable,setStreamUnavailable]=useState(false);const [streamGeneration,setStreamGeneration]=useState(0);
  const [pending,setPending]=useState<PendingNativeMessage|null>(null);const [createKey,setCreateKey]=useState(()=>crypto.randomUUID());
  const conversations=useQuery({queryKey:['conversations'],queryFn:()=>listConversations(),retry:false});
  const moreConversations=useMutation({mutationFn:()=>listConversations(conversations.data?.next_cursor??''),retry:false,onSuccess:page=>cache.setQueryData(['conversations'],(old:{items:NonNullable<typeof conversations.data>['items'];next_cursor:string|null}|undefined)=>old?{items:[...old.items,...page.items.filter(item=>!old.items.some(previous=>previous.id===item.id))],next_cursor:page.next_cursor}:page)});
  const messages=useQuery({queryKey:['messages',selected],queryFn:()=>readMessages(selected),enabled:!!selected,retry:false,structuralSharing:(old,fresh)=>mergeCommitted(old as Parameters<typeof mergeCommitted>[0],fresh as Parameters<typeof mergeCommitted>[1])});
  const handled=useQuery({queryKey:['handling',selected],queryFn:()=>readHandling(selected),enabled:!!selected,retry:false,refetchInterval:30000});
  const more=useMutation({mutationFn:()=>readMessages(selected,messages.data?.items.reduce((n,item)=>Math.max(n,item.sequence),0)??0),retry:false,onSuccess:page=>cache.setQueryData(['messages',selected],(old:{items:Message[];next_cursor:string|null}|undefined)=>old?{items:[...old.items,...page.items.filter(item=>!old.items.some(previous=>previous.id===item.id))].sort((a,b)=>a.sequence-b.sequence),next_cursor:page.next_cursor}:page)});
  const create=useMutation({mutationFn:()=>createConversation(createKey),retry:false,onSuccess:result=>{setSelected(result.id);setCreateKey(crypto.randomUUID());void cache.invalidateQueries({queryKey:['conversations']});}});
  const send=useMutation({mutationFn:(item:PendingNativeMessage)=>sendMessage(item),retry:false,onSuccess:()=>{setPending(null);setDraft('');void cache.invalidateQueries({queryKey:['messages',selected]});}});
  useEffect(()=>{if(!selected&&conversations.data?.items.length)setSelected(conversations.data.items[0].id);},[selected,conversations.data]);
  // A stream the server closed (or the browser abandoned) is opened again after the rows on screen, 5 s later.
  useEffect(()=>{if(!selected||!messages.data)return;const after=messages.data.items.reduce((n,item)=>Math.max(n,item.sequence),0);let retry:ReturnType<typeof setTimeout>|undefined;const close=subscribe(selected,after,event=>{setStreamUnavailable(false);cache.setQueryData(['messages',selected],(old:{items:Message[];next_cursor:string|null}|undefined)=>old?{...old,items:[...old.items.filter(item=>item.id!==event.data.id),event.data].sort((a,b)=>a.sequence-b.sequence)}:old);},()=>setStreamUnavailable(true),()=>setStreamUnavailable(false),()=>{retry=setTimeout(()=>setStreamGeneration(n=>n+1),5000);});return ()=>{clearTimeout(retry);close();};},[selected,!!messages.data,cache,streamGeneration]);
  const current=conversations.data?.items.find(item=>item.id===selected);
  function submit(){const item=pending??nativeMessage(selected,draft);setPending(item);send.mutate(item);}
  // B12 (owner 2026-10-28): the customer sees the conversation and simple states only - no execution profile, world or receipt
  // vocabulary. A test deployment is still said plainly, so a test reply is never taken for the real service.
  const test=current?.execution_profile==='deterministic-test'||(!!current&&current.world_id!=='real');
  const latest=messages.data?.items.reduce<string|null>((t,item)=>!t||item.accepted_at>t?item.accepted_at:t,null)??null;
  return <section className="chat" aria-labelledby="chat-heading">
    <div className="chat-bar"><h2 id="chat-heading">我的对话</h2>
      {conversations.isPending?<p role="status">正在读取你的对话…</p>:conversations.isError?<p role="alert">{chatError(conversations.error)}</p>:<div className="chat-picker"><label htmlFor="conversation">你的对话</label><select id="conversation" value={selected} disabled={!!pending||send.isPending} onChange={event=>{setSelected(event.target.value);setStreamUnavailable(false);}}><option value="">选择对话</option>{conversations.data.items.map((item,i)=><option key={item.id} value={item.id}>{conversationLabel(i,item.id,item)}</option>)}</select>
        <button type="button" disabled={create.isPending||!!pending} onClick={()=>create.mutate()}>{create.isPending?'正在创建…':'新建对话'}</button></div>}
      {test||latest?<p className="note chat-meta">{test?<span className="chat-test-badge">测试环境，回复不代表正式服务</span>:null}{test&&latest?' · ':null}{latest?<>最近消息 <time dateTime={latest}>{localTime(latest)}</time></>:null}</p>:null}
      {conversations.data&&!conversations.data.items.length&&<p>还没有对话，点“新建对话”开始。</p>}{conversations.data?.next_cursor&&<button type="button" disabled={moreConversations.isPending} onClick={()=>moreConversations.mutate()}>加载更早的对话</button>}{moreConversations.isError&&<p role="alert">{chatError(moreConversations.error)}</p>}
      {create.isError&&<p role="alert">{chatError(create.error)}</p>}
      {selected&&handled.data?.handled_by==='human'&&<p role="status" className="banner">人工客服处理中</p>}
    </div>
    {selected&&<>
      {messages.isPending?<p role="status">正在读取消息…</p>:messages.isError?<p role="alert">{chatError(messages.error)}</p>:<ol className="messages">{messages.data?.items.map(item=>{const view=messagePresentation(item);return <li key={item.id} className={item.direction==='outbound'?'message outbound':'message inbound'} data-direction={item.direction}><span>{view.sender}</span><time dateTime={item.accepted_at}>{localTime(item.accepted_at)}</time><p>{item.body}</p>{view.status?<small>{view.status}</small>:null}{view.receipt&&<MessageReceipt messageId={item.id}/>}</li>;})}</ol>}
      {messages.data?.next_cursor&&<button type="button" disabled={more.isPending} onClick={()=>more.mutate()}>{more.isPending?'正在读取…':'加载更多消息'}</button>}{more.isError&&<p role="alert">{chatError(more.error)}</p>}
      <div className="chat-composer">
        {streamUnavailable&&<p role="status">实时连接暂不可用，正在重新连接；可以点“刷新消息”查看最新消息。</p>}
        <form onSubmit={event=>{event.preventDefault();submit();}}><label htmlFor="chat-message">消息</label><textarea id="chat-message" value={pending?.body??draft} maxLength={8192} disabled={!!pending} onChange={event=>setDraft(event.target.value)} aria-describedby="chat-limit"/><p id="chat-limit" className="note">目前只支持文字消息。退订推广或删除个人信息的请求暂时需要另行联系企业。</p><div className="chat-composer-actions"><button type="button" className="btn-secondary" onClick={()=>void messages.refetch()}>刷新消息</button><button disabled={send.isPending||(!pending&&!draft.trim())}>{send.isPending?'正在发送…':pending?'重新发送这条消息':'发送'}</button></div></form>
        {send.isError&&<p role="alert">{chatError(send.error)}</p>}
      </div>
    </>}
  </section>;
}
