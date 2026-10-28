import {expect,test} from 'vitest';
import {message,messagePresentation} from '../src/chat-api';
const base={id:'b'.repeat(64),conversation_id:'a'.repeat(64),sequence:2,actor:'agent-principal',body:'好的，我会在明天下午前给你处理进展。',accepted_at:'2026-10-09T02:00:00Z',status:'accepted'};
const outbound={...base,direction:'outbound',sender_kind:'agent',intent_id:'5f0c8c8e-3b1a-4c6d-9e2f-1a2b3c4d5e6f',trigger_message_id:'c'.repeat(64)};
test('the customer\'s own message reads as 我 / 已发送 with its progress check, never the sender principal (B12)',()=>{
 const item=message(base);expect(item.direction).toBe('inbound');expect(item.sender_kind).toBe('consumer');
 expect(messagePresentation(item)).toEqual({sender:'我',status:'已发送',receipt:true});
 expect(JSON.stringify(messagePresentation(item))).not.toContain('agent-principal');
});
test('an AI reply is labelled as such, with no delivery vocabulary and no progress check (B12)',()=>{
 const item=message(outbound);expect(item).toMatchObject({direction:'outbound',sender_kind:'agent',intent_id:outbound.intent_id,trigger_message_id:'c'.repeat(64)});
 const view=messagePresentation(item);
 expect(view.sender).toBe('智能客服');expect(view.receipt).toBe(false);expect(view.status).toBe('');
 expect(messagePresentation(message({...outbound,sender_kind:'human_takeover',intent_id:undefined}))).toEqual({sender:'人工客服',status:'',receipt:false});
});
test.each([
 ['unknown direction',{...outbound,direction:'sideways'}],
 ['outbound without agent sender',{...outbound,sender_kind:'consumer'}],
 ['outbound without governed intent',{...outbound,intent_id:undefined}],
 ['outbound with malformed intent',{...outbound,intent_id:'not-a-uuid'}],
 ['outbound without trigger message',{...outbound,trigger_message_id:undefined}],
 ['inbound with forged sender fields',{...base,sender_kind:'agent'}],
 ['inbound with forged intent',{...base,intent_id:outbound.intent_id}],
])('rejects %s',(_,value)=>{expect(()=>message(value)).toThrow('响应无效');});
const evidence={namespace:'native.webchat',message_ref:'5f0c8c8e-3b1a-4c6d-9e2f-1a2b3c4d5e6f',sequence:2,sent_at:'2026-10-09T01:59:58.000Z',trust:'client',skewed:false};
test('NX-051 projection: reply target and provider evidence parse; receipt sequence is untouched',()=>{
 const item=message({...base,reply_to_message_id:'c'.repeat(64),provider:evidence});
 expect(item).toMatchObject({sequence:2,reply_to_message_id:'c'.repeat(64),provider:evidence});
 expect(message({...outbound,reply_to_message_id:'c'.repeat(64),provider:{...evidence,namespace:'nexloop.agent',message_ref:null,sequence:null,sent_at:null,trust:'server'}}).provider?.trust).toBe('server');
 expect(message({...base,reply_to_message_id:null,provider:null})).toMatchObject({reply_to_message_id:null,provider:null});
});
test.each([
 ['malformed reply target',{...base,reply_to_message_id:'x'}],
 ['unknown trust',{...base,provider:{...evidence,trust:'verified'}}],
 ['extra provider field',{...base,provider:{...evidence,order:1}}],
 ['missing provider field',{...base,provider:{...evidence,skewed:undefined}}],
 ['non-positive provider sequence',{...base,provider:{...evidence,sequence:0}}],
])('NX-051 rejects %s',(_,value)=>{expect(()=>message(value)).toThrow('响应无效');});

test('B12: message progress in plain words, never claiming more than the governed receipt (AT-033: unknown asks not to resend)',async()=>{
  const {receiptText}=await import('../src/MessageReceipt');
  const run={run_id:'0b0c6c57-0000-4000-8000-000000000001',request_id:'r',task_id:'t',status:'queued' as const};
  const receipt=(state:string,success=false)=>({message_id:'m',run,receipt:{intent_id:'i',receipt_id:'r',state,scope:'service_delivery',provider_state:null,business_action_success:success}} as never);
  expect(receiptText({message_id:'m',run:null,receipt:null})).toBe('已收到，等待处理。');
  expect(receiptText({message_id:'m',run,receipt:null})).toBe('处理中。');
  expect(receiptText(receipt('confirmed'))).toBe('已办理完成。');expect(receiptText(receipt('fulfilled',true))).toBe('已办理完成。');
  expect(receiptText(receipt('observed_fulfilled'))).toBe('处理中，正在确认结果。');  // a provider report alone is not done
  expect(receiptText(receipt('unknown'))).toContain('请不要重复发送');
  expect(receiptText(receipt('failed'))).toBe('暂未办成。如需帮助，请继续留言。');
  for(const s of ['accepted','dispatching','unknown','observed_fulfilled','fulfilled','failed','confirmed'])expect(receiptText(receipt(s))).not.toMatch(/治理|回执|Provider|意图|履行/);
});

test('B12: a failed progress read says the message was sent and only the progress is not readable now',async()=>{
  const {progressError}=await import('../src/MessageReceipt');const {ApiError}=await import('../src/api');
  expect(progressError(new ApiError(503))).toBe('暂时查不到处理进度，请稍后再看；你的消息已经发出。');
  expect(progressError(new ApiError(401))).toBe('登录已失效，请重新登录。');
});

test('X4 (l2/ui-backend-x1-x4, fixtures): a conversation is named by when it started and how long it is; an older server keeps the list place',async()=>{
  const {conversation}=await import('../src/chat-api');const {conversationLabel}=await import('../src/WebChat');
  const c={id:'a'.repeat(64),consumer_id:'b'.repeat(64),world_id:'real',revision:1};
  const timed=conversation({...c,started_at:'2026-10-28T02:30:00.000000Z',last_message_at:'2026-10-28T03:00:00.000000Z',message_count:5});
  expect(timed).toMatchObject({message_count:5,started_at:'2026-10-28T02:30:00.000000Z'});
  const day=new Date('2026-10-28T02:30:00Z').toLocaleString('zh-CN',{month:'long',day:'numeric',hour:'2-digit',minute:'2-digit',hour12:false});
  expect(conversationLabel(0,c.id,timed)).toBe(`${day} 开始 · 共 5 条`);expect(day).toMatch(/10月28日/);
  expect(conversationLabel(1,c.id,conversation({...c,started_at:null,last_message_at:null,message_count:0}))).toBe('新对话（还没有消息）');
  expect(conversationLabel(2,c.id,conversation(c))).toBe('对话 3');
  for(const bad of [{started_at:'x',last_message_at:'x',message_count:1},{started_at:null,last_message_at:null},{message_count:-1,started_at:null,last_message_at:null},{message_count:1.5,started_at:null,last_message_at:null}])
    expect(()=>conversation({...c,...bad})).toThrow();
});

test('L6 I1: a conversation whose earliest messages were cleared (retention, erasure) has a count and no times; it parses and keeps its list place',async()=>{
  const {conversation,listConversations}=await import('../src/chat-api');const {conversationLabel}=await import('../src/WebChat');
  const c={id:'a'.repeat(64),consumer_id:'b'.repeat(64),world_id:'real',revision:1};
  const cleared=conversation({...c,started_at:null,last_message_at:null,message_count:7});
  expect(cleared).toMatchObject({message_count:7,started_at:null,last_message_at:null});
  expect(conversationLabel(1,c.id,cleared)).toBe('对话 2');
  // The whole list still reads when one conversation is like that.
  const ok={...c,id:'c'.repeat(64),started_at:'2026-10-28T02:30:00.000000Z',last_message_at:'2026-10-28T03:00:00.000000Z',message_count:2};
  const fetch=async()=>new Response(JSON.stringify({items:[ok,{...c,started_at:null,last_message_at:null,message_count:7}],next_cursor:null}),{status:200});
  const original=globalThis.fetch;globalThis.fetch=fetch as typeof globalThis.fetch;
  try{expect((await listConversations()).items.map(x=>x.message_count)).toEqual([2,7]);}finally{globalThis.fetch=original;}
});
