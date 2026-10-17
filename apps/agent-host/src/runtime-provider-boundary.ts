/** Strip provider error/debug payloads before Pi can persist them as transcript. */
import type {AssistantMessage,AssistantMessageEventStream} from '@earendil-works/pi-ai';
import {RuntimeError} from './runtime-adapter.js';
const safeCodes=new Set(['runtime_model_unavailable','runtime_budget_exhausted','runtime_cost_policy_missing','runtime_model_cost_unavailable','runtime_model_cost_mismatch','runtime_authorization_denied','runtime_authorization_unavailable','runtime_command_expired','runtime_owner_unavailable']);
function safeFailure(error:unknown):RuntimeError{return new RuntimeError(error instanceof RuntimeError&&safeCodes.has(error.code)?error.code:'runtime_model_unavailable');}
export function safeProviderMessage(message:AssistantMessage):AssistantMessage{
  const value=structuredClone(message);
  const allowed=new Set(['role','content','api','provider','model','responseModel','responseId','providerThinkingLevel','thinkingLevel','usage','stopReason','deferred','errorMessage','endTurn','timestamp','durationMs']);
  for(const key of Object.keys(value))if(!allowed.has(key))Reflect.deleteProperty(value,key);
  delete value.diagnostics;delete value.rawStopReason;
  if(value.errorMessage!==undefined&&!safeCodes.has(value.errorMessage))value.errorMessage='runtime_model_unavailable';
  if(value.stopReason==='error'||value.stopReason==='aborted'){
    value.content=[];value.errorMessage=value.errorMessage&&safeCodes.has(value.errorMessage)?value.errorMessage:'runtime_model_unavailable';
    delete value.responseId;delete value.responseModel;
  }
  return value;
}
/** settle: optional trusted outcome recorder (Context v6), awaited before the final event is
 * released; `null` when the provider failed without a message. A failed record fails the call. */
/** 10/18 stage (Run 1fb1e35a): a provider failure became runtime_model_unavailable with nothing to say why. The failure is
 * reported, before any payload is stripped, as fixed fields only: its kind, the HTTP status when one is found (three digits,
 * 4xx/5xx) and an exception class name. Never a message, a body, a header or a key. */
export type ProviderFailure={kind:'provider_error'|'aborted'|'stream_exception'|'settle_failed';http_status:number|null;exception:string|null;
  reason?:'timeout'|'connection'|'aborted'|'http'|'other';cause_code?:string|null;
  /** 10/18: the stalled connection phase of the final attempt (connect: no TCP; tls: handshake), and all connection failures. */
  connect_phase?:'connect'|'tls'|null;connect_failures?:number};
const STATUS=/\b([45][0-9]{2})\b/;
function statusOf(value:unknown):number|null{
  const direct=value&&typeof value==='object'?(value as {status?:unknown;statusCode?:unknown}).status??(value as {statusCode?:unknown}).statusCode:undefined;
  if(typeof direct==='number'&&Number.isInteger(direct)&&direct>=400&&direct<=599)return direct;
  const text=typeof value==='string'?value:value instanceof Error?value.message:typeof (value as {errorMessage?:unknown})?.errorMessage==='string'?(value as {errorMessage:string}).errorMessage:'';
  const match=STATUS.exec(text);return match?Number(match[1]):null;
}
function className(value:unknown):string|null{
  const name=value instanceof Error?value.constructor.name:null;return name&&/^[A-Za-z][A-Za-z0-9_]{0,63}$/.test(name)?name:null;
}
/** 10/18 (Runs ee7a82d2, 2f7101e8, 71eef156: all at 10.49 s, status and exception null): a fixed reason read from the provider's
 * own failure text, never the text itself, and a Node or undici error code (e.g. UND_ERR_CONNECT_TIMEOUT) when there is one. */
function textOf(value:unknown):string{
  return typeof value==='string'?value:value instanceof Error?value.message:typeof (value as {errorMessage?:unknown})?.errorMessage==='string'?(value as {errorMessage:string}).errorMessage:'';
}
function causeCode(value:unknown):string|null{
  for(let current:unknown=value,depth=0;current&&typeof current==='object'&&depth<4;current=(current as {cause?:unknown}).cause,depth++){
    const code=(current as {code?:unknown}).code;
    if(typeof code==='string'&&/^[A-Z][A-Z0-9_]{2,40}$/.test(code))return code;
  }
  return null;
}
function reasonOf(kind:ProviderFailure['kind'],status:number|null,cause:string|null,text:string):NonNullable<ProviderFailure['reason']>{
  if(kind==='aborted')return 'aborted';
  if(status!==null)return 'http';
  if(/TIMEOUT|ETIMEDOUT/.test(cause??'')||/timed? ?out|timeout/i.test(text))return 'timeout';
  if(/^(ECONN|ENOTFOUND|EAI_|EPIPE|UND_ERR_SOCKET|UND_ERR_CLOSED)/.test(cause??'')||/connection error|socket hang up|fetch failed|terminated|network|ECONNRESET|ECONNREFUSED|ENOTFOUND/i.test(text))return 'connection';
  return 'other';
}
export function providerFailure(kind:ProviderFailure['kind'],raw:unknown):ProviderFailure{
  const http_status=statusOf(raw),cause_code=causeCode(raw);
  return {kind,http_status,exception:className(raw),reason:reasonOf(kind,http_status,cause_code,textOf(raw)),cause_code};
}
export function safeProviderStream(stream:AssistantMessageEventStream,verify?:(message:AssistantMessage)=>void,settle?:(message:AssistantMessage|null)=>Promise<void>,report?:(failure:ProviderFailure)=>void):AssistantMessageEventStream{
  const tell=(kind:ProviderFailure['kind'],raw:unknown)=>{try{report?.(providerFailure(kind,raw));}catch{/* diagnostics never change the outcome */}};
  const settled=async(message:AssistantMessage|null)=>{try{await settle?.(message);}catch(error){tell('settle_failed',error);throw error;}};
  return new Proxy(stream,{get(target,key){
    if(key===Symbol.asyncIterator)return async function*(){
      try{
        for await(const event of target){
          if(event.type==='error'){tell(event.error.stopReason==='aborted'?'aborted':'provider_error',event.error);const error=safeProviderMessage(event.error);verify?.(error);await settled(error);yield {...event,error};}
          else if(event.type==='done'){const message=safeProviderMessage(event.message);verify?.(message);await settled(message);yield {...event,message};}
          else yield {...event,partial:safeProviderMessage(event.partial)};
        }
      }catch(error){if(!(error instanceof RuntimeError))tell('stream_exception',error);if(settle)await settle(null).catch(()=>{});throw safeFailure(error);}
    };
    if(key==='result')return async()=>{try{const raw=await target.result();if(raw.stopReason==='error'||raw.stopReason==='aborted')tell(raw.stopReason==='aborted'?'aborted':'provider_error',raw);const message=safeProviderMessage(raw);verify?.(message);await settled(message);return message;}catch(error){if(!(error instanceof RuntimeError))tell('stream_exception',error);if(settle)await settle(null).catch(()=>{});throw safeFailure(error);}};
    const value=Reflect.get(target,key,target);return typeof value==='function'?value.bind(target):value;
  }});
}
