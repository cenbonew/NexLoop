/** Strip provider error/debug payloads before Pi can persist them as transcript. */
import type {AssistantMessage,AssistantMessageEventStream} from '@earendil-works/pi-ai';
import {RuntimeError} from './runtime-adapter.js';
const safeCodes=new Set(['runtime_model_unavailable','runtime_budget_exhausted','runtime_cost_policy_missing','runtime_model_cost_unavailable','runtime_model_cost_mismatch','runtime_authorization_denied','runtime_command_expired','runtime_owner_unavailable']);
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
export function safeProviderStream(stream:AssistantMessageEventStream,verify?:(message:AssistantMessage)=>void):AssistantMessageEventStream{
  return new Proxy(stream,{get(target,key){
    if(key===Symbol.asyncIterator)return async function*(){
      try{
        for await(const event of target){
          if(event.type==='error'){const error=safeProviderMessage(event.error);verify?.(error);yield {...event,error};}
          else if(event.type==='done'){const message=safeProviderMessage(event.message);verify?.(message);yield {...event,message};}
          else yield {...event,partial:safeProviderMessage(event.partial)};
        }
      }catch(error){throw safeFailure(error);}
    };
    if(key==='result')return async()=>{try{const message=safeProviderMessage(await target.result());verify?.(message);return message;}catch(error){throw safeFailure(error);}};
    const value=Reflect.get(target,key,target);return typeof value==='function'?value.bind(target):value;
  }});
}
