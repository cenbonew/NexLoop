/** Run-bound intent tools. Backend owns every EIOS/PG/provider credential. */
import {request as httpsRequest} from 'node:https';
import {Type} from '@earendil-works/pi-ai';
import {defineTool,type ToolRegistration} from '@earendil-works/pi-durable';
import {RuntimeError,validateRunCommand,type RunCommand} from './runtime-adapter.js';

type Material=(path:string,maximum:number)=>Buffer;
export type EffectReceipt={intent_id:string;receipt_id:string;state:string;payload_digest:string;provider_payload_digest:string;scope:'effect_intent';business_action_success:boolean};
type Configuration={guardURL:URL;caPath:string;keyPath:string;privateMaterial:Material;activationForRun:(runId:string)=>string|undefined};
const uuid=/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const sha=/^[a-f0-9]{64}$/;
function exact(value:unknown,keys:readonly string[]):Record<string,unknown>{
  if(!value||typeof value!=='object'||Array.isArray(value)||Object.getPrototypeOf(value)!==Object.prototype)throw new RuntimeError('runtime_effect_unavailable');
  const row=value as Record<string,unknown>;
  if(Object.keys(row).sort().join(',')!==[...keys].sort().join(','))throw new RuntimeError('runtime_effect_unavailable');
  return row;
}
export class RuntimeEffectClient{
  private readonly origin:URL;
  private readonly configuration:Configuration;
  constructor(configuration:Configuration){
    const origin=new URL(configuration.guardURL);
    if(origin.protocol!=='https:'||origin.hostname!=='127.0.0.1'||!origin.port||Number(origin.port)<1024||Number(origin.port)>65535||origin.username||origin.password||origin.search||origin.hash||origin.pathname!=='/internal/v1/runtime/authorize')throw new RuntimeError('runtime_effect_configuration_invalid');
    if(typeof configuration.activationForRun!=='function'||typeof configuration.privateMaterial!=='function')throw new RuntimeError('runtime_effect_configuration_invalid');
    this.origin=origin;this.configuration={...configuration};
  }
  async call(command:RunCommand,operation:'submit'|'find',arguments_:unknown):Promise<EffectReceipt>{
    try{
      const valid=validateRunCommand(command);
      const ref=this.configuration.activationForRun(valid.run_id);
      if(!ref||!/^activation_[A-Za-z0-9_-]{16,200}$/.test(ref)||Date.parse(valid.not_after)<=Date.now())throw new Error();
      let argumentsBody:Record<string,unknown>;
      if(operation==='submit'){
        const row=exact(arguments_,['message']);
        if(typeof row.message!=='string'||[...row.message].length<1||[...row.message].length>8192)throw new Error();
        argumentsBody={parameters:{message:row.message}};
      }else if(operation==='find'){
        const row=exact(arguments_,['intent_id']);
        if(typeof row.intent_id!=='string'||!uuid.test(row.intent_id))throw new Error();
        argumentsBody={intent_id:row.intent_id};
      }else throw new Error();
      const key=this.configuration.privateMaterial(this.configuration.keyPath,64).toString('utf8');
      if(!/^[0-9a-f]{64}$/.test(key))throw new Error();
      const ca=this.configuration.privateMaterial(this.configuration.caPath,32768);
      const body=Buffer.from(JSON.stringify({activation_ref:ref,command:valid,...argumentsBody}));
      if(body.length>65536)throw new Error();
      const url=new URL('/internal/v1/runtime/effects/'+operation,this.origin);
      return await new Promise<EffectReceipt>((resolve,reject)=>{
        const fail=()=>reject(new RuntimeError('runtime_effect_unavailable'));
        const request=httpsRequest(url,{method:'POST',ca,minVersion:'TLSv1.2',agent:false,signal:AbortSignal.timeout(2000),
          headers:{Authorization:'Bearer '+key,'Content-Type':'application/json','Content-Length':body.length}},response=>{
          let size=0;const chunks:Buffer[]=[];
          if(response.headers['content-type']!=='application/json'||response.headers['content-encoding']!==undefined){response.destroy();fail();return;}
          response.on('data',(chunk:Buffer)=>{size+=chunk.length;if(size>32768){response.destroy();request.destroy();fail();}else chunks.push(chunk);});
          response.on('error',fail);
          response.on('end',()=>{try{
            const result=JSON.parse(Buffer.concat(chunks).toString('utf8')) as unknown;
            if(response.statusCode===409){
              // Preserve the public conflict code without retaining error text,
              // headers, debug fields or arbitrary upstream exception content.
              if(result&&typeof result==='object'&&(result as Record<string,unknown>).code==='intent_payload_conflict')reject(new RuntimeError('intent_payload_conflict'));else fail();
              return;
            }
            if(response.statusCode!==200)throw new Error();
            const envelope=exact(result,['run_id','receipt']);
            if(envelope.run_id!==valid.run_id)throw new Error();
            const receipt=exact(envelope.receipt,['intent_id','receipt_id','state','payload_digest','provider_payload_digest','scope','business_action_success']);
            if(typeof receipt.intent_id!=='string'||!uuid.test(receipt.intent_id)||typeof receipt.receipt_id!=='string'||!uuid.test(receipt.receipt_id)
              ||typeof receipt.state!=='string'||!['accepted','dispatching','unknown','observed_fulfilled','fulfilled','failed','confirmed'].includes(receipt.state)
              ||typeof receipt.payload_digest!=='string'||!sha.test(receipt.payload_digest)||typeof receipt.provider_payload_digest!=='string'||!sha.test(receipt.provider_payload_digest)
              ||receipt.scope!=='effect_intent'||receipt.business_action_success!==false
              ||operation==='find'&&receipt.intent_id!==(arguments_ as {intent_id:string}).intent_id)throw new Error();
            resolve(receipt as EffectReceipt);
          }catch{fail();}});
        });
        request.setTimeout(2000,()=>request.destroy());request.on('error',fail);request.end(body);
      });
    }catch(error){
      if(error instanceof RuntimeError&&error.code==='intent_payload_conflict')throw error;
      throw new RuntimeError('runtime_effect_unavailable');
    }
  }
  toolsForRun(command:RunCommand):readonly ToolRegistration[]{
    const bound=validateRunCommand(command);
    const invoke=async(operation:'submit'|'find',args:unknown)=>{
      try{return {content:[{type:'text' as const,text:JSON.stringify(await this.call(bound,operation,args))}]};}
      catch(error){if(error instanceof RuntimeError&&error.code==='intent_payload_conflict')return {isError:true,content:[{type:'text' as const,text:'{"code":"intent_payload_conflict","http_status":409}'}]};throw new RuntimeError('runtime_effect_unavailable');}
    };
    return [
      defineTool({name:'nexloop.service.request',description:'Persist a governed service intent in the current plan slot. accepted is not fulfillment. Retries return the original receipt; never choose a new business key.',
        parameters:Type.Object({message:Type.String({minLength:1,maxLength:8192})},{additionalProperties:false}),replay:'safe',execute:async args=>invoke('submit',args)}),
      defineTool({name:'nexloop.service.find',description:'Read the current authorized receipt for an existing service intent. Does not send or create a new effect.',
        parameters:Type.Object({intent_id:Type.String({pattern:'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'})},{additionalProperties:false}),replay:'safe',execute:async args=>invoke('find',args)}),
    ];
  }
}
