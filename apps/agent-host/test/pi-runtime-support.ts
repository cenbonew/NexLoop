import { randomUUID } from 'node:crypto';
import { createModels, fauxProvider, fauxAssistantMessage, calculateCost } from '../../../vendor/pi/packages/ai/dist/index.js';
import type { RunCommand } from '../src/runtime-adapter.ts';

export function command(): RunCommand {
  return {schema_version:'1.0',run_id:randomUUID(),tenant_id:randomUUID(),world_id:'real',mode:'real',
    request_id:'synthetic-pi-request-'+randomUUID(),trigger_event_id:randomUUID(),role_ref:'role:synthetic',
    consumer_ref:'consumer:synthetic',goal_version_ref:'goal:synthetic-v1',context_manifest_ref:'artifact:synthetic',
    runtime_profile:'synthetic-test',credential_ref:'credential:synthetic-reference',
    budget:{maximum_model_turns:8,maximum_tool_calls:8,active_timeout_seconds:60,maximum_cost:'1.0',currency:'USD'},
    not_after:new Date(Date.now()+300_000).toISOString(),runtime_owner_epoch:1};
}

export function deterministic(options:Parameters<typeof fauxProvider>[0]={}) {
  const faux=fauxProvider(options);
  const models=createModels();
  models.setProvider(faux.provider);
  faux.setResponses([fauxAssistantMessage('synthetic deterministic completion')]);
  // Frozen fauxProvider estimates real transcript tokens but intentionally emits
  // zero cost regardless of its model rate. For the nonzero-cost case only,
  // expose a deterministic provider stream priced from those actual tokens via
  // the frozen Pi cost calculator. The Harness and storage remain unmodified.
  if(options.models?.some(model=>model.cost&&Object.values(model.cost).some(rate=>rate!==0))){
    const priced=new Proxy(models,{get(target,key){
      if(key==='streamSimple')return (...args:Parameters<typeof models.streamSimple>)=>{
        const stream=target.streamSimple(...args);
        const price=(message:import('../../../vendor/pi/packages/ai/dist/index.js').AssistantMessage)=>{
          const copy=structuredClone(message);calculateCost(args[0],copy.usage);return copy;
        };
        return new Proxy(stream,{get(source,property){
          if(property===Symbol.asyncIterator)return async function*(){for await(const event of source){
            if(event.type==='done')yield {...event,message:price(event.message)};
            else if(event.type==='error')yield {...event,error:price(event.error)};
            else yield {...event,partial:price(event.partial)};
          }};
          if(property==='result')return async()=>price(await source.result());
          const value=Reflect.get(source,property,source);return typeof value==='function'?value.bind(source):value;
        }});
      };
      const value=Reflect.get(target,key,target);return typeof value==='function'?value.bind(target):value;
    }});
    return {models:priced,faux};
  }
  return {models,faux};
}

export async function until(predicate:()=>Promise<boolean>,timeout=10_000):Promise<void> {
  const end=Date.now()+timeout;
  while(!await predicate()) {
    if(Date.now()>end)throw new Error('Pi runtime condition not reached');
    await new Promise(resolve=>setTimeout(resolve,10));
  }
}
