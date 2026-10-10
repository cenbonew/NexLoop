import {nativeAttempt,type PendingNativeMessage} from './native-message';
import {ApiError,refreshCsrf} from './api';

export type Conversation={id:string;consumer_id:string;world_id:string;revision:number;execution_profile?:'deterministic-test'|'real-provider'|'disabled'};
export type Message={id:string;conversation_id:string;sequence:number;actor:string;body:string;accepted_at:string;status:'accepted';direction:'inbound'|'outbound';sender_kind:'consumer'|'agent';intent_id?:string;trigger_message_id?:string;reply_to_message_id?:string|null;provider?:MessageProvider|null};
// NX-051: channel order/time/reference as evidence. Display only; the list order stays `sequence`.
export type MessageProvider={namespace:string;message_ref:string|null;sequence:number|null;sent_at:string|null;trust:'server'|'signed'|'client';skewed:boolean};
export type CommittedEvent={id:string;type:'message.accepted';data:Message};
type Page<T>={items:T[];next_cursor:string|null};
const opaqueId=/^[0-9a-f]{64}$/;
function object(value:unknown):Record<string,unknown>{if(!value||typeof value!=='object'||Array.isArray(value))throw new Error('响应无效');return value as Record<string,unknown>;}
function string(value:unknown):string{if(typeof value!=='string'||!value)throw new Error('响应无效');return value;}
function id(value:unknown):string{const result=string(value);if(!opaqueId.test(result))throw new Error('响应无效');return result;}
export function conversation(value:unknown):Conversation{const v=object(value);if(!Number.isSafeInteger(v.revision)||Number(v.revision)<1)throw new Error('响应无效');if(v.execution_profile!==undefined&&!['deterministic-test','real-provider','disabled'].includes(String(v.execution_profile)))throw new Error('响应无效');if(v.execution_profile!==undefined&&typeof v.execution_profile!=='string')throw new Error('响应无效');return {id:id(v.id),consumer_id:id(v.consumer_id),world_id:string(v.world_id),revision:Number(v.revision),...(v.execution_profile===undefined?{}:{execution_profile:v.execution_profile as Conversation['execution_profile']})};}
function provider(value:unknown):MessageProvider|null{if(value===null)return null;const v=object(value);const keys=['namespace','message_ref','sequence','sent_at','trust','skewed'];
  if(Object.keys(v).length!==keys.length||!keys.every(k=>k in v)||!/^[a-z][a-z0-9.-]{1,63}$/.test(string(v.namespace))||(v.message_ref!==null&&typeof v.message_ref!=='string')||(v.sequence!==null&&(!Number.isSafeInteger(v.sequence)||Number(v.sequence)<1))||(v.sent_at!==null&&!Number.isFinite(Date.parse(string(v.sent_at))))||!['server','signed','client'].includes(String(v.trust))||typeof v.skewed!=='boolean')throw new Error('响应无效');
  return {namespace:v.namespace as string,message_ref:v.message_ref as string|null,sequence:v.sequence as number|null,sent_at:v.sent_at as string|null,trust:v.trust as MessageProvider['trust'],skewed:v.skewed};}
function evidence(v:Record<string,unknown>):Pick<Message,'reply_to_message_id'|'provider'>{return {...(v.reply_to_message_id===undefined?{}:{reply_to_message_id:v.reply_to_message_id===null?null:id(v.reply_to_message_id)}),...(v.provider===undefined?{}:{provider:provider(v.provider)})};}
export function message(value:unknown):Message{const v=object(value);if(!Number.isSafeInteger(v.sequence)||Number(v.sequence)<1||v.status!=='accepted'||typeof v.body!=='string'||!Number.isFinite(Date.parse(string(v.accepted_at))))throw new Error('响应无效');
  const base={id:id(v.id),conversation_id:id(v.conversation_id),sequence:Number(v.sequence),actor:string(v.actor),body:v.body,accepted_at:string(v.accepted_at),status:'accepted' as const,...evidence(v)};
  // NX-047: server-derived direction. Inbound items carry no sender fields; an outbound item is an
  // Agent reply the channel already accepted, bound to its governed intent and triggering message.
  if(v.direction===undefined){if(v.sender_kind!==undefined||v.intent_id!==undefined||v.trigger_message_id!==undefined)throw new Error('响应无效');return {...base,direction:'inbound',sender_kind:'consumer'};}
  if(v.direction!=='outbound'||v.sender_kind!=='agent'||typeof v.intent_id!=='string'||!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(v.intent_id))throw new Error('响应无效');
  return {...base,direction:'outbound',sender_kind:'agent',intent_id:v.intent_id,trigger_message_id:id(v.trigger_message_id)};}
export function messagePresentation(item:Message):{sender:string;status:string;receipt:boolean}{
  // A visible outbound Message means the channel accepted it; it never claims the problem is solved.
  return item.direction==='outbound'?{sender:'企业 Agent 回复',status:'渠道已接受；不代表问题已解决或承诺已兑现',receipt:false}:{sender:'发送者：'+item.actor,status:'消息已接受',receipt:true};}
function page<T>(value:unknown,validate:(value:unknown)=>T):Page<T>{const v=object(value);if(!Array.isArray(v.items)||(v.next_cursor!==null&&typeof v.next_cursor!=='string'))throw new Error('响应无效');return {items:v.items.map(validate),next_cursor:v.next_cursor as string|null};}
async function request(path:string,init?:RequestInit):Promise<unknown>{const response=await fetch(path,{...init,credentials:'same-origin',cache:'no-store'});if(!response.ok)throw new ApiError(response.status);return response.json();}
async function write(path:string,body:unknown,key:string):Promise<unknown>{const {csrf}=await refreshCsrf();return request(path,{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf,'Idempotency-Key':key},body:JSON.stringify(body)});}
export async function listConversations(after=''){return page(await request('/api/v1/conversations'+(after?'?after='+encodeURIComponent(id(after)):'')),conversation);}
export async function createConversation(key:string){return conversation(await write('/api/v1/conversations',{},key));}
export async function readMessages(conversationId:string,after=0){return page(await request(`/api/v1/conversations/${encodeURIComponent(id(conversationId))}/messages?after=${after}`),message);}
export async function sendMessage(item:PendingNativeMessage){const attempt=nativeAttempt(item);const v=object(await write(attempt.path,attempt.body,attempt.transportKey));if(typeof v.created!=='boolean'||v.provider_namespace!=='native.webchat'||v.provider_event_id!==item.providerEventId)throw new Error('响应无效');const committed=message(v.message);if(committed.conversation_id!==item.conversationId||committed.body!==item.body)throw new Error('响应无效');return {message:committed,created:v.created};}
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

export type DeliveryScope={service_code:'local.json-export';deliverable:'固定私有目录内可核验的 JSON 文本导出文件';price_amount:'0';currency:'CNY';guarantees:[];discounts:[];limitations:['不提供目录外保证或折扣','不代表第三方渠道送达、付款或问题解决'];evidence_kind:'fsynced_json_export'};
export type MessageScopeDenial={message_id:string;denial:null|{record_id:string;code:'outside_catalog_terms';scope:DeliveryScope;recorded_at:string}};
function exact(value:Record<string,unknown>,keys:string[]){if(Object.keys(value).length!==keys.length||keys.some(key=>!Object.hasOwn(value,key)))throw new Error('响应无效');}
export function scopeDenial(value:unknown):MessageScopeDenial{
  const v=object(value);exact(v,['message_id','denial']);const messageId=id(v.message_id);
  if(v.denial===null)return {message_id:messageId,denial:null};
  const d=object(v.denial);exact(d,['record_id','code','scope','recorded_at']);
  const s=object(d.scope);exact(s,['service_code','deliverable','price_amount','currency','guarantees','discounts','limitations','evidence_kind']);
  if(d.code!=='outside_catalog_terms'||typeof d.recorded_at!=='string'||!Number.isFinite(Date.parse(d.recorded_at))||s.service_code!=='local.json-export'||s.deliverable!=='固定私有目录内可核验的 JSON 文本导出文件'||s.price_amount!=='0'||s.currency!=='CNY'||s.evidence_kind!=='fsynced_json_export'||!Array.isArray(s.guarantees)||s.guarantees.length!==0||!Array.isArray(s.discounts)||s.discounts.length!==0||JSON.stringify(s.limitations)!==JSON.stringify(['不提供目录外保证或折扣','不代表第三方渠道送达、付款或问题解决']))throw new Error('响应无效');
  return {message_id:messageId,denial:{record_id:id(d.record_id),code:'outside_catalog_terms',scope:s as DeliveryScope,recorded_at:d.recorded_at}};
}
export async function readScopeDenial(messageId:string){const value=scopeDenial(await request(`/api/v1/messages/${encodeURIComponent(id(messageId))}/scope-denial`));if(value.message_id!==messageId)throw new Error('响应无效');return value;}
// NX-028 D4: whether a person is handling the conversation; only "agent" | "human", nothing about who or until when.
export type Handling={conversation_id:string;handled_by:'agent'|'human'};
export function handling(value:unknown):Handling{const v=object(value);if(!['agent','human'].includes(String(v.handled_by))||typeof v.handled_by!=='string')throw new Error('响应无效');return {conversation_id:id(v.conversation_id),handled_by:v.handled_by as Handling['handled_by']};}
export async function readHandling(conversationId:string){const value=handling(await request(`/api/v1/conversations/${encodeURIComponent(id(conversationId))}/handling`));if(value.conversation_id!==conversationId)throw new Error('响应无效');return value;}
