import {nativeMessage,type PendingNativeMessage} from './native-message';
import {useEffect,useState} from 'react';
import {MessageReceipt} from './MessageReceipt';
import {useMutation,useQuery,useQueryClient} from '@tanstack/react-query';
import {chatError,createConversation,listConversations,messagePresentation,readHandling,readMessages,sendMessage,subscribe,type Message} from './chat-api';
export function WebChat(){
  const cache=useQueryClient();const [selected,setSelected]=useState('');const [draft,setDraft]=useState('');const [streamUnavailable,setStreamUnavailable]=useState(false);
  const [pending,setPending]=useState<PendingNativeMessage|null>(null);const [createKey,setCreateKey]=useState(()=>crypto.randomUUID());
  const conversations=useQuery({queryKey:['conversations'],queryFn:()=>listConversations(),retry:false});
  const moreConversations=useMutation({mutationFn:()=>listConversations(conversations.data?.next_cursor??''),retry:false,onSuccess:page=>cache.setQueryData(['conversations'],(old:{items:NonNullable<typeof conversations.data>['items'];next_cursor:string|null}|undefined)=>old?{items:[...old.items,...page.items.filter(item=>!old.items.some(previous=>previous.id===item.id))],next_cursor:page.next_cursor}:page)});
  const messages=useQuery({queryKey:['messages',selected],queryFn:()=>readMessages(selected),enabled:!!selected,retry:false});
  const handled=useQuery({queryKey:['handling',selected],queryFn:()=>readHandling(selected),enabled:!!selected,retry:false,refetchInterval:30000});
  const more=useMutation({mutationFn:()=>readMessages(selected,messages.data?.items.reduce((n,item)=>Math.max(n,item.sequence),0)??0),retry:false,onSuccess:page=>cache.setQueryData(['messages',selected],(old:{items:Message[];next_cursor:string|null}|undefined)=>old?{items:[...old.items,...page.items.filter(item=>!old.items.some(previous=>previous.id===item.id))].sort((a,b)=>a.sequence-b.sequence),next_cursor:page.next_cursor}:page)});
  const create=useMutation({mutationFn:()=>createConversation(createKey),retry:false,onSuccess:result=>{setSelected(result.id);setCreateKey(crypto.randomUUID());void cache.invalidateQueries({queryKey:['conversations']});}});
  const send=useMutation({mutationFn:(item:PendingNativeMessage)=>sendMessage(item),retry:false,onSuccess:()=>{setPending(null);setDraft('');void cache.invalidateQueries({queryKey:['messages',selected]});}});
  useEffect(()=>{if(!selected&&conversations.data?.items.length)setSelected(conversations.data.items[0].id);},[selected,conversations.data]);
  useEffect(()=>{if(!selected||!messages.data)return;const after=messages.data.items.reduce((n,item)=>Math.max(n,item.sequence),0);return subscribe(selected,after,event=>{setStreamUnavailable(false);cache.setQueryData(['messages',selected],(old:{items:Message[];next_cursor:string|null}|undefined)=>old?{...old,items:[...old.items.filter(item=>item.id!==event.data.id),event.data].sort((a,b)=>a.sequence-b.sequence)}:old);},()=>setStreamUnavailable(true),()=>setStreamUnavailable(false));},[selected,!!messages.data,cache]);
  const current=conversations.data?.items.find(item=>item.id===selected);
  function submit(){const item=pending??nativeMessage(selected,draft);setPending(item);send.mutate(item);}
  return <section className="chat" aria-labelledby="chat-heading"><h2 id="chat-heading">消费者对话</h2>
    <p className="note">执行模式：{current?.execution_profile==='deterministic-test'?'确定性测试（不代表真实模型或真实渠道）':current?.execution_profile==='real-provider'?'受控 Provider（调用结果以实际回执为准）':current?.execution_profile==='disabled'?'未启用':'待核验'}</p>
    <p className="note">企业服务入口 · {current?.world_id==='real'?'业务世界：real':current?'业务世界：'+current.world_id:'业务世界待核验'}</p>
    {conversations.isPending?<p role="status">正在读取你的对话…</p>:conversations.isError?<p role="alert">{chatError(conversations.error)}</p>:<><label htmlFor="conversation">你的对话</label><select id="conversation" value={selected} disabled={!!pending||send.isPending} onChange={event=>{setSelected(event.target.value);setStreamUnavailable(false);}}><option value="">选择对话</option>{conversations.data.items.map(item=><option key={item.id} value={item.id}>{item.id}</option>)}</select>{!conversations.data.items.length&&<p>还没有对话。</p>}{conversations.data.next_cursor&&<button type="button" disabled={moreConversations.isPending} onClick={()=>moreConversations.mutate()}>读取更多对话</button>}{moreConversations.isError&&<p role="alert">{chatError(moreConversations.error)}</p>}</>}
    <button type="button" disabled={create.isPending||!!pending} onClick={()=>create.mutate()}>{create.isPending?'正在创建…':'建立服务对话'}</button>
    {create.isError&&<p role="alert">{chatError(create.error)}</p>}
    {selected&&handled.data?.handled_by==='human'&&<p role="status" className="banner">人工客服处理中</p>}
    {selected&&<><p className="note">仅显示已提交消息。“已接受”表示消息入库，尚不代表服务已履行。执行模式以服务端配置标签为准；可查询实际治理回执，未取得回执时不推断履行。</p>
      {messages.isPending?<p role="status">正在读取消息…</p>:messages.isError?<p role="alert">{chatError(messages.error)}</p>:<ol className="messages">{messages.data?.items.map(item=>{const view=messagePresentation(item);return <li key={item.id} className={item.direction==='outbound'?'message outbound':'message inbound'} data-direction={item.direction}><span>{view.sender}</span><time dateTime={item.accepted_at}>{new Date(item.accepted_at).toLocaleString('zh-CN')}</time><p>{item.body}</p><small>{view.status}</small>{view.receipt&&<MessageReceipt messageId={item.id}/>}</li>;})}</ol>}
      {messages.data?.next_cursor&&<button type="button" disabled={more.isPending} onClick={()=>more.mutate()}>{more.isPending?'正在读取…':'读取后续已提交消息'}</button>}{more.isError&&<p role="alert">{chatError(more.error)}</p>}
      {streamUnavailable&&<p role="status">实时连接暂不可用，正在等待重新连接；可手动核对已提交消息。</p>}
      <button type="button" onClick={()=>void messages.refetch()}>核对已提交消息</button>
      <form onSubmit={event=>{event.preventDefault();submit();}}><label htmlFor="chat-message">消息</label><textarea id="chat-message" value={pending?.body??draft} maxLength={8192} disabled={!!pending} onChange={event=>setDraft(event.target.value)} aria-describedby="chat-limit"/><p id="chat-limit" className="note">仅支持文本；附件、停止促销及删除请求专用处理尚未接通。</p><button disabled={send.isPending||(!pending&&!draft.trim())}>{send.isPending?'正在提交…':pending?'核对并重试同一消息':'发送消息'}</button></form>
      {send.isError&&<p role="alert">{chatError(send.error)}</p>}
    </>}
  </section>;
}
