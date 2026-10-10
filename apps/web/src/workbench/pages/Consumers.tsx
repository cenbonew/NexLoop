import {useState,type ReactNode} from 'react';
import {useQuery} from '@tanstack/react-query';
import {readConsumer,readConsumers,readConversation,RESTRICTED_TEXT} from '../api';
import {Page,Short} from './Page';

export function Consumers({actions,onOpen}:{actions?:ReactNode;onOpen:(consumerId:string)=>void}){
  const [after,setAfter]=useState<string[]>([]);
  const cursor=after[after.length-1];
  const query=useQuery({queryKey:['workbench','consumers',cursor??''],queryFn:()=>readConsumers(cursor)});
  return <Page title="消费者" query={query} actions={actions} empty={d=>d.items.length===0&&!cursor}>{data=><>
    <table><thead><tr><th>客户</th><th>联系限制</th><th>未结束承诺</th><th>活跃计划</th><th>会话</th></tr></thead>
      <tbody>{data.items.map(c=><tr key={c.consumer_id}><td><button type="button" className="link" onClick={()=>onOpen(c.consumer_id)}><Short value={c.consumer_id}/></button></td>
        <td>{c.restricted?'受限':'—'}</td><td>{c.open_commitments}</td><td>{c.active_plans}</td><td>{c.conversations}</td></tr>)}</tbody></table>
    <div className="pager">{after.length?<button type="button" onClick={()=>setAfter(after.slice(0,-1))}>上一页</button>:null}
      {data.next_cursor?<button type="button" onClick={()=>setAfter([...after,data.next_cursor!])}>下一页</button>:null}</div>
  </>}</Page>;
}

export function ConsumerDetail({consumerId,actions,onConversation}:{consumerId:string;actions?:ReactNode;onConversation:(conversationId:string)=>void}){
  const query=useQuery({queryKey:['workbench','consumer',consumerId],queryFn:()=>readConsumer(consumerId)});
  return <Page title="消费者详情" query={query} actions={actions}>{data=><>
    <p>客户 <Short value={data.consumer_id}/>{data.paused?' · 主动联系已暂停':''}</p>
    <h2>联系限制</h2>
    {data.restriction?<p className={data.restriction.active?'warning':'note'}>{data.restriction.active?'受限':'已解除'} · 规则 {data.restriction.rule_id} · 控制 revision {data.restriction.control_revision} · {data.restriction.restricted_at}</p>:<p className="note">没有联系限制。</p>}
    <h2>属性</h2>
    {data.properties.status==='ok'?(Object.keys(data.properties.values).length?<dl>{Object.entries(data.properties.values).map(([k,v])=><div key={k}><dt>{k}</dt><dd>{typeof v==='string'?v:JSON.stringify(v)}</dd></div>)}</dl>:<p className="note">没有可读属性。</p>)
      :<p className="note" role="status">无属性读取权限：字段与证据不显示（AT-003）。</p>}
    <h2>会话</h2>
    {data.conversations.length?<ul>{data.conversations.map(c=><li key={c.conversation_id}><button type="button" className="link" onClick={()=>onConversation(c.conversation_id)}><Short value={c.conversation_id}/></button>（{c.last_sequence} 条）</li>)}</ul>:<p className="note">暂无会话。</p>}
    <h2>承诺</h2>
    {data.commitments.length?<p>{data.commitments.length} 条，见“承诺 / 交付”。</p>:<p className="note">暂无承诺。</p>}
  </>}</Page>;
}

export function Conversation({conversationId,actions}:{conversationId:string;actions?:ReactNode}){
  const query=useQuery({queryKey:['workbench','conversation',conversationId],queryFn:()=>readConversation(conversationId)});
  return <Page title="会话" query={query} actions={actions} empty={d=>d.messages.length===0}>{data=><ol className="workbench-messages">
    {data.messages.map(m=><li key={m.id} className={'message-'+m.direction}>
      <p className="meta">#{m.sequence} · {m.direction==='outbound'?(m.sender_kind==='agent'?'企业 Agent 回复':'企业回复'):'顾客'} · {m.accepted_at}
        {m.provider?` · 渠道 ${m.provider.namespace}${m.provider.sequence!==null?` 序号 ${m.provider.sequence}`:''}${m.provider.trust==='client'?'（客户端陈述）':''}${m.provider.skewed?'（时间偏差）':''}`:''}
        {m.reply_to_message_id?<> · 回复 <Short value={m.reply_to_message_id}/></>:null}</p>
      {m.content.status==='ok'?<p>{m.content.body}</p>:<p className="note">{RESTRICTED_TEXT}</p>}
    </li>)}
  </ol>}</Page>;
}
