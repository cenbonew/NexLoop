/** Native web identity is distinct from the per-attempt transport key. */
export type NativeReplyTo=Readonly<{kind:'message';ref:string}>;
/**
 * NX-051: clientSequence/clientSentAt are what this browser states (its local send order and clock).
 * The server keeps them only as client-level evidence: they never reorder messages and are never a
 * time anchor. They are frozen with the logical message, so retries send the same values.
 */
export type PendingNativeMessage=Readonly<{conversationId:string;providerEventId:string;body:string;clientSequence?:number;clientSentAt?:string;replyTo?:NativeReplyTo}>;
const uuid=/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const hex64=/^[0-9a-f]{64}$/;
const localOrder=new Map<string,number>();
export function nativeMessage(conversationId:string,body:string,replyToMessageId?:string):PendingNativeMessage{
  if(!hex64.test(conversationId)||typeof body!=='string'||!body.trim()||body.length>8192)throw new Error('消息格式无效');
  if(replyToMessageId!==undefined&&!hex64.test(replyToMessageId))throw new Error('消息格式无效');
  const clientSequence=(localOrder.get(conversationId)??0)+1;localOrder.set(conversationId,clientSequence);
  return Object.freeze({conversationId,body,providerEventId:crypto.randomUUID(),clientSequence,clientSentAt:new Date().toISOString(),...(replyToMessageId===undefined?{}:{replyTo:Object.freeze({kind:'message' as const,ref:replyToMessageId})})});
}
export function nativeAttempt(item:PendingNativeMessage){
  if(!uuid.test(item.providerEventId)||!hex64.test(item.conversationId)||typeof item.body!=='string'||!item.body.trim()||item.body.length>8192)throw new Error('消息格式无效');
  const path=`/api/v1/conversations/${item.conversationId}/native-messages`;
  if(item.clientSequence===undefined&&item.clientSentAt===undefined&&item.replyTo===undefined)
    return {path,transportKey:crypto.randomUUID(),body:{schema_version:'nexloop.native-message.v1',provider_event_id:item.providerEventId,body:item.body}};
  if(item.clientSequence!==undefined&&(!Number.isSafeInteger(item.clientSequence)||item.clientSequence<1))throw new Error('消息格式无效');
  if(item.clientSentAt!==undefined&&!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$/.test(item.clientSentAt))throw new Error('消息格式无效');
  if(item.replyTo!==undefined&&(item.replyTo.kind!=='message'||!hex64.test(item.replyTo.ref)))throw new Error('消息格式无效');
  // v2 carries all three keys (null when not stated); the server accepts exactly the v1 or v2 key set.
  return {path,transportKey:crypto.randomUUID(),body:{schema_version:'nexloop.native-message.v2',provider_event_id:item.providerEventId,body:item.body,
    client_sequence:item.clientSequence??null,client_sent_at:item.clientSentAt??null,reply_to:item.replyTo===undefined?null:{kind:item.replyTo.kind,ref:item.replyTo.ref}}};
}
