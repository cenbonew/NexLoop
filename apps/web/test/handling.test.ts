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
