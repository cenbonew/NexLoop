import {expect,test} from 'vitest';
import {handling} from '../src/chat-api';
// NX-028 D4: the customer only learns "a person is handling this", never who or until when.
test('parses agent and human handling',()=>{
 expect(handling({conversation_id:'a'.repeat(64),handled_by:'human'})).toEqual({conversation_id:'a'.repeat(64),handled_by:'human'});
 expect(handling({conversation_id:'a'.repeat(64),handled_by:'agent'}).handled_by).toBe('agent');
});
test.each([
 ['unknown handler',{conversation_id:'a'.repeat(64),handled_by:'staff'}],
 ['missing handler',{conversation_id:'a'.repeat(64)}],
 ['malformed conversation',{conversation_id:'x',handled_by:'human'}],
])('rejects %s',(_,value)=>{expect(()=>handling(value)).toThrow('响应无效');});
import {message,messagePresentation} from '../src/chat-api';
const staff={id:'b'.repeat(64),conversation_id:'a'.repeat(64),sequence:3,actor:'staff-principal',body:'您好，我是人工客服。',accepted_at:'2026-10-10T02:00:00Z',status:'accepted',
 direction:'outbound',sender_kind:'human_takeover',trigger_message_id:'c'.repeat(64)};
test('staff reply (ruling B) parses without an intent and never claims resolution',()=>{
 const item=message(staff);expect(item).toMatchObject({direction:'outbound',sender_kind:'human_takeover',trigger_message_id:'c'.repeat(64)});
 const view=messagePresentation(item);expect(view.sender).toBe('人工客服回复');expect(view.receipt).toBe(false);expect(view.status).toContain('不代表问题已解决');
});
test.each([
 ['staff reply with an intent',{...staff,intent_id:'5f0c8c8e-3b1a-4c6d-9e2f-1a2b3c4d5e6f'}],
 ['staff reply without its message',{...staff,trigger_message_id:undefined}],
])('rejects %s',(_,value)=>{expect(()=>message(value)).toThrow('响应无效');});
