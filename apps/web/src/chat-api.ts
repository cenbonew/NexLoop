import {ApiError,refreshCsrf} from './api';

export type Conversation={id:string;consumer_id:string;world_id:string;revision:number;execution_profile?:'deterministic-test'|'real-provider'|'disabled'};
export type Message={id:string;conversation_id:string;sequence:number;actor:string;body:string;accepted_at:string;status:'accepted'};
export type CommittedEvent={id:string;type:'message.accepted';data:Message};
type Page<T>={items:T[];next_cursor:string|null};
const opaqueId=/^[0-9a-f]{64}$/;
function object(value:unknown):Record<string,unknown>{if(!value||typeof value!=='object'||Array.isArray(value))throw new Error('响应无效');return value as Record<string,unknown>;}
function string(value:unknown):string{if(typeof value!=='string'||!value)throw new Error('响应无效');return value;}
function id(value:unknown):string{const result=string(value);if(!opaqueId.test(result))throw new Error('响应无效');return result;}
export function conversation(value:unknown):Conversation{const v=object(value);if(!Number.isSafeInteger(v.revision)||Number(v.revision)<1)throw new Error('响应无效');if(v.execution_profile!==undefined&&!['deterministic-test','real-provider','disabled'].includes(String(v.execution_profile)))throw new Error('响应无效');if(v.execution_profile!==undefined&&typeof v.execution_profile!=='string')throw new Error('响应无效');return {id:id(v.id),consumer_id:id(v.consumer_id),world_id:string(v.world_id),revision:Number(v.revision),...(v.execution_profile===undefined?{}:{execution_profile:v.execution_profile as Conversation['execution_profile']})};}
export function message(value:unknown):Message{const v=object(value);if(!Number.isSafeInteger(v.sequence)||Number(v.sequence)<1||v.status!=='accepted'||typeof v.body!=='string'||!Number.isFinite(Date.parse(string(v.accepted_at))))throw new Error('响应无效');return {id:id(v.id),conversation_id:id(v.conversation_id),sequence:Number(v.sequence),actor:string(v.actor),body:v.body,accepted_at:string(v.accepted_at),status:'accepted'};}
function page<T>(value:unknown,validate:(value:unknown)=>T):Page<T>{const v=object(value);if(!Array.isArray(v.items)||(v.next_cursor!==null&&typeof v.next_cursor!=='string'))throw new Error('响应无效');return {items:v.items.map(validate),next_cursor:v.next_cursor as string|null};}
async function request(path:string,init?:RequestInit):Promise<unknown>{const response=await fetch(path,{...init,credentials:'same-origin',cache:'no-store'});if(!response.ok)throw new ApiError(response.status);return response.json();}
async function write(path:string,body:unknown,key:string):Promise<unknown>{const {csrf}=await refreshCsrf();return request(path,{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf,'Idempotency-Key':key},body:JSON.stringify(body)});}
export async function listConversations(after=''){return page(await request('/api/v1/conversations'+(after?'?after='+encodeURIComponent(id(after)):'')),conversation);}
export async function createConversation(key:string){return conversation(await write('/api/v1/conversations',{},key));}
export async function readMessages(conversationId:string,after=0){return page(await request(`/api/v1/conversations/${encodeURIComponent(id(conversationId))}/messages?after=${after}`),message);}
export async function sendMessage(conversationId:string,body:string,key:string){const v=object(await write(`/api/v1/conversations/${encodeURIComponent(id(conversationId))}/messages`,{body},key));if(typeof v.created!=='boolean')throw new Error('响应无效');return {message:message(v.message),created:v.created};}
export function subscribe(conversationId:string,after:number,onCommitted:(event:CommittedEvent)=>void,onUnavailable:()=>void,onAvailable:()=>void=()=>{}):()=>void{
  const source=new EventSource(`/api/v1/conversations/${encodeURIComponent(id(conversationId))}/events?after=${after}`,{withCredentials:true});
  source.addEventListener('committed',raw=>{try{const event=raw as MessageEvent;const v=object(JSON.parse(event.data));if(typeof v.id!=='string'||! /^[1-9][0-9]*$/.test(v.id)||v.id!==event.lastEventId||v.type!=='message.accepted')throw new Error('响应无效');const data=message(v.data);if(String(data.sequence)!==v.id||data.conversation_id!==conversationId)throw new Error('响应无效');onCommitted({id:v.id,type:'message.accepted',data});}catch{source.close();onUnavailable();}});
  source.addEventListener('unavailable',()=>{source.close();onUnavailable();});source.onerror=onUnavailable;source.onopen=onAvailable;
  return ()=>source.close();
}
export function chatError(error:unknown){if(error instanceof ApiError){if(error.status===403)return '你没有此对话的访问权限。';if(error.status===401)return '登录已失效，请重新登录。';if(error.status===409)return '此请求编号已有不同内容，请核对原消息。';if(error.status===422)return '消息格式无效，请检查输入。';}return '服务暂不可用；消息是否已接受需核对，重试会使用同一请求编号。';}

export type ServiceReceipt={intent_id:string;receipt_id:string;state:'accepted'|'dispatching'|'unknown'|'observed_fulfilled'|'fulfilled'|'failed'|'confirmed';scope:'service_delivery';provider_state:'accepted'|'fulfilled'|'not_found'|null;governed_claim_finalized:boolean;business_action_success:boolean};
export type MessageServiceReceipt={message_id:string;run:{run_id:string;request_id:string;task_id:string;status:'queued'}|null;receipt:ServiceReceipt|null};
function uuidId(value:unknown):string{const result=string(value);if(!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(result))throw new Error('响应无效');return result;}
export function serviceReceipt(value:unknown):MessageServiceReceipt{
  const v=object(value);const messageId=id(v.message_id);let run:MessageServiceReceipt['run']=null;let receipt:ServiceReceipt|null=null;
  if(v.run!==null){const r=object(v.run);if(r.status!=='queued')throw new Error('响应无效');run={run_id:uuidId(r.run_id),request_id:string(r.request_id),task_id:string(r.task_id),status:'queued'};}
  if(v.receipt!==null){const r=object(v.receipt);if(typeof r.state!=='string'||!['accepted','dispatching','unknown','observed_fulfilled','fulfilled','failed','confirmed'].includes(r.state)||r.scope!=='service_delivery'||typeof r.business_action_success!=='boolean'||typeof r.governed_claim_finalized!=='boolean'||(r.provider_state!==null&&!(typeof r.provider_state==='string'&&['accepted','fulfilled','not_found'].includes(r.provider_state))))throw new Error('响应无效');
    const complete=r.state==='fulfilled'||r.state==='confirmed';if(r.business_action_success!==complete||r.governed_claim_finalized!==complete||(complete&&r.provider_state!=='fulfilled')||!run)throw new Error('响应无效');
    receipt={intent_id:uuidId(r.intent_id),receipt_id:uuidId(r.receipt_id),state:r.state as ServiceReceipt['state'],scope:'service_delivery',provider_state:r.provider_state as ServiceReceipt['provider_state'],governed_claim_finalized:r.governed_claim_finalized,business_action_success:r.business_action_success};}
  return {message_id:messageId,run,receipt};
}
export async function readServiceReceipt(messageId:string){const result=serviceReceipt(await request(`/api/v1/messages/${encodeURIComponent(id(messageId))}/receipt`));if(result.message_id!==messageId)throw new Error('响应无效');return result;}
