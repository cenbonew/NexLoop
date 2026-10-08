/** Native web identity is distinct from the per-attempt transport key. */
export type PendingNativeMessage=Readonly<{conversationId:string;providerEventId:string;body:string}>;
const uuid=/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
export function nativeMessage(conversationId:string,body:string):PendingNativeMessage{
  if(!/^[0-9a-f]{64}$/.test(conversationId)||typeof body!=='string'||!body.trim()||body.length>8192)throw new Error('消息格式无效');
  return Object.freeze({conversationId,body,providerEventId:crypto.randomUUID()});
}
export function nativeAttempt(item:PendingNativeMessage){
  if(!uuid.test(item.providerEventId)||!/^[0-9a-f]{64}$/.test(item.conversationId)||typeof item.body!=='string'||!item.body.trim()||item.body.length>8192)throw new Error('消息格式无效');
  return {path:`/api/v1/conversations/${item.conversationId}/native-messages`,transportKey:crypto.randomUUID(),body:{schema_version:'nexloop.native-message.v1',provider_event_id:item.providerEventId,body:item.body}};
}
