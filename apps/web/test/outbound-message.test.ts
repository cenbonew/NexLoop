import {expect,test} from 'vitest';
import {message,messagePresentation} from '../src/chat-api';
const base={id:'b'.repeat(64),conversation_id:'a'.repeat(64),sequence:2,actor:'agent-principal',body:'好的，我会在明天下午前给你处理进展。',accepted_at:'2026-10-09T02:00:00Z',status:'accepted'};
const outbound={...base,direction:'outbound',sender_kind:'agent',intent_id:'5f0c8c8e-3b1a-4c6d-9e2f-1a2b3c4d5e6f',trigger_message_id:'c'.repeat(64)};
test('inbound consumer message keeps its receipt and sender label',()=>{
 const item=message(base);expect(item.direction).toBe('inbound');expect(item.sender_kind).toBe('consumer');
 expect(messagePresentation(item)).toEqual({sender:'发送者：agent-principal',status:'消息已接受',receipt:true});
});
test('outbound Agent reply is labelled, never claims resolution and has no consumer receipt',()=>{
 const item=message(outbound);expect(item).toMatchObject({direction:'outbound',sender_kind:'agent',intent_id:outbound.intent_id,trigger_message_id:'c'.repeat(64)});
 const view=messagePresentation(item);
 expect(view.sender).toBe('企业 Agent 回复');expect(view.receipt).toBe(false);
 expect(view.status).toContain('渠道已接受');expect(view.status).toContain('不代表问题已解决');
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
