import {afterEach,expect,test,vi} from 'vitest';
import {nativeAttempt,nativeMessage} from '../src/native-message';
import {sendMessage} from '../src/chat-api';
const conversation='a'.repeat(64);
const committed=(event:string,body:string)=>({provider_namespace:'native.webchat',provider_event_id:event,created:true,message:{id:'b'.repeat(64),conversation_id:conversation,sequence:1,actor:'actual-server-actor',body,accepted_at:'2026-10-08T01:00:00Z',status:'accepted'}});
afterEach(()=>vi.unstubAllGlobals());
test('new logical messages get distinct provider UUID; retries retain it and generate independent transport keys',()=>{
 const first=nativeMessage(conversation,'first');const second=nativeMessage(conversation,'second');
 expect(first.providerEventId).not.toBe(second.providerEventId);expect(Object.isFrozen(first)).toBe(true);
 const a=nativeAttempt(first),b=nativeAttempt(first);expect(a.body).toEqual(b.body);expect(a.body.provider_event_id).toBe(first.providerEventId);expect(a.transportKey).not.toBe(b.transportKey);expect(a.transportKey).not.toBe(first.providerEventId);
});
test('actual API client retries same frozen message after lost response via native endpoint',async()=>{
 const item=nativeMessage(conversation,'actual user body');const writes:{path:string;init:RequestInit}[]=[];
 vi.stubGlobal('fetch',vi.fn(async(path:string,init:RequestInit)=>{
  if(path==='/api/v1/auth/csrf')return Response.json({authenticated:true,tenant_id:'server-tenant',principal_id:'server-human',restricted:false,csrf_token:'synthetic-csrf'});
  writes.push({path,init});if(writes.length===1)throw new TypeError('synthetic lost response');return Response.json({...committed(item.providerEventId,item.body),created:false},{status:202});
 }));
 await expect(sendMessage(item)).rejects.toThrow('synthetic lost response');expect((await sendMessage(item)).created).toBe(false);
 expect(writes).toHaveLength(2);expect(writes[0].path).toBe(`/api/v1/conversations/${conversation}/native-messages`);
 expect(JSON.parse(String(writes[0].init.body))).toEqual({schema_version:'nexloop.native-message.v2',provider_event_id:item.providerEventId,body:item.body,client_sequence:item.clientSequence,client_sent_at:item.clientSentAt,reply_to:null});
 expect(writes[0].init.body).toBe(writes[1].init.body);expect((writes[0].init.headers as Record<string,string>)['Idempotency-Key']).not.toBe((writes[1].init.headers as Record<string,string>)['Idempotency-Key']);
 expect(writes[0].init.credentials).toBe('same-origin');
});
test.each(['event','namespace','conversation','body'])('client rejects mismatched committed %s response',async(field)=>{
 const item=nativeMessage(conversation,'body');const result=committed(item.providerEventId,item.body);
 if(field==='event')result.provider_event_id=crypto.randomUUID();if(field==='namespace')result.provider_namespace='other';if(field==='conversation')result.message.conversation_id='c'.repeat(64);if(field==='body')result.message.body='changed';
 vi.stubGlobal('fetch',vi.fn(async(path:string)=>Response.json(path==='/api/v1/auth/csrf'?{authenticated:true,tenant_id:'tenant',principal_id:'human',restricted:false,csrf_token:'synthetic'}:result)));
 await expect(sendMessage(item)).rejects.toThrow('响应无效');
});
test('invalid provider ID and body fail before transport',()=>{
 const item=nativeMessage(conversation,'body');expect(()=>nativeAttempt({...item,providerEventId:'internal-generated-sequence'})).toThrow();expect(()=>nativeMessage(conversation,' ')).toThrow();expect(()=>nativeMessage(conversation,'x'.repeat(8193))).toThrow();
});
test('NX-051 v2: per-conversation local order, frozen client time, optional quoted reply; legacy item still sends v1',()=>{
 const other='d'.repeat(64);const a=nativeMessage(other,'one'),b=nativeMessage(other,'two',conversation);
 expect(b.clientSequence).toBe(a.clientSequence!+1);expect(a.clientSentAt).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/);
 expect(nativeAttempt(b).body).toEqual({schema_version:'nexloop.native-message.v2',provider_event_id:b.providerEventId,body:'two',client_sequence:b.clientSequence,client_sent_at:b.clientSentAt,reply_to:{kind:'message',ref:conversation}});
 expect(nativeAttempt(b).body).toEqual(nativeAttempt(b).body);
 expect(nativeAttempt({conversationId:other,providerEventId:crypto.randomUUID(),body:'x'}).body.schema_version).toBe('nexloop.native-message.v1');
 expect(()=>nativeMessage(other,'x','not-an-id')).toThrow();expect(()=>nativeAttempt({...a,clientSequence:0})).toThrow();expect(()=>nativeAttempt({...a,clientSentAt:'yesterday'})).toThrow();
});
