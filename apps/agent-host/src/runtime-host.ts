/** Optional internal Run admission. Backend retains all EIOS/PG credentials. */
import {request as httpsRequest} from 'node:https';
import {type IncomingMessage} from 'node:http';
import {createModels,fauxProvider,fauxAssistantMessage,fauxToolCall} from '@earendil-works/pi-ai';
import {PiRuntimeAdapter} from './pi-runtime-adapter.js';
import {RuntimeEffectClient} from './runtime-effect-tools.js';
import {RuntimeError,validateRunCommand,type RunCommand} from './runtime-adapter.js';

type Material=(path:string,maximum:number)=>Buffer;
function record(value:unknown):Record<string,unknown>{
  if(!value||typeof value!=='object'||Array.isArray(value))throw new RuntimeError('invalid_runtime_request');
  return value as Record<string,unknown>;
}
export async function readRuntimeBody(request:IncomingMessage){
  if(request.headers['content-type']!=='application/json'||request.headers['content-encoding']!==undefined)throw new RuntimeError('invalid_runtime_request');
  const chunks:Buffer[]=[];let size=0;
  for await(const chunk of request){size+=chunk.length;if(size>262144)throw new RuntimeError('runtime_request_too_large');chunks.push(chunk);}
  try{return record(JSON.parse(Buffer.concat(chunks).toString('utf8')));}catch{throw new RuntimeError('invalid_runtime_request');}
}
export class RuntimeHost{
  private readonly adapter:PiRuntimeAdapter;
  private readonly activations=new Map<string,{ref:string;input?:string}>();
  private readonly guard:URL;
  private readonly caPath:string;
  private readonly keyPath:string;
  private pending=0;
  constructor(root:string,configPath:string,privateMaterial:Material,assertOwner:()=>void){
    const config=record(JSON.parse(privateMaterial(configPath,32768).toString('utf8')));
    const required=['guard_ca_file','guard_key_file','guard_url','runtime_profile'];
    if(required.some(key=>!Object.hasOwn(config,key))||Object.keys(config).some(key=>!required.includes(key)&&!['effect_tools','deterministic_effect_message'].includes(key))||config.runtime_profile!=='deterministic-test')throw new Error('runtime configuration refused');
    if(config.effect_tools!==undefined&&typeof config.effect_tools!=='boolean')throw new Error('runtime configuration refused');
    if(config.deterministic_effect_message!==undefined&&(config.effect_tools!==true||typeof config.deterministic_effect_message!=='string'||[...config.deterministic_effect_message].length<1||[...config.deterministic_effect_message].length>8192))throw new Error('runtime configuration refused');
    this.guard=new URL(String(config.guard_url));
    if(this.guard.protocol!=='https:'||this.guard.hostname!=='127.0.0.1'||!this.guard.port||Number(this.guard.port)<1024||Number(this.guard.port)>65535||this.guard.username||this.guard.password||this.guard.search||this.guard.hash||this.guard.pathname!=='/internal/v1/runtime/authorize')throw new Error('runtime guard refused');
    if(typeof config.guard_ca_file!=='string'||typeof config.guard_key_file!=='string')throw new Error('runtime guard files required');
    this.caPath=config.guard_ca_file;this.keyPath=config.guard_key_file;
    const guard=async(command:RunCommand,operation:string,input?:string,activationRef?:string)=>{
      const active=this.activations.get(command.run_id);
      const ref=activationRef??active?.ref;
      if(input===undefined&&(operation==='start'||operation==='resume'))input=active?.input;
      if(!ref)throw new RuntimeError('runtime_authorization_denied');
      const key=privateMaterial(this.keyPath,64).toString('utf8');
      if(!/^[0-9a-f]{64}$/.test(key))throw new RuntimeError('runtime_authorization_denied');
      const payload=Buffer.from(JSON.stringify({activation_ref:ref,command,operation,...(input===undefined?{}:{input})}));
      return await new Promise<{ever_execution_authorized:boolean}>((resolve,reject)=>{
        const request=httpsRequest(this.guard,{method:'POST',ca:privateMaterial(this.caPath,32768),minVersion:'TLSv1.2',agent:false,signal:AbortSignal.timeout(2000),
          headers:{Authorization:'Bearer '+key,'Content-Type':'application/json','Content-Length':payload.length}},response=>{
          let size=0;const chunks:Buffer[]=[];
          response.on('data',(chunk:Buffer)=>{size+=chunk.length;if(size>32768){request.destroy();reject(new RuntimeError('runtime_authorization_denied'));}else chunks.push(chunk);});
          response.on('error',()=>reject(new RuntimeError('runtime_authorization_denied')));
          response.on('end',()=>{try{const result=record(JSON.parse(Buffer.concat(chunks).toString('utf8')));if(response.statusCode!==200||result.authorized!==true||result.run_id!==command.run_id||typeof result.ever_execution_authorized!=='boolean'||(['model','tool'].includes(operation)&&result.ever_execution_authorized!==true))throw new Error();resolve({ever_execution_authorized:result.ever_execution_authorized});}catch{reject(new RuntimeError('runtime_authorization_denied'));}});
        });
        request.setTimeout(2000,()=>request.destroy());request.on('error',()=>reject(new RuntimeError('runtime_authorization_denied')));request.end(payload);
      });
    };
    this.authorizeAdmission=guard;
    const models=createModels(),faux=fauxProvider();models.setProvider(faux.provider);
    // Explicit test profile only; no model secret or real-provider success claim.
    if(config.effect_tools===true&&typeof config.deterministic_effect_message==='string'){
      // Explicit deterministic acceptance profile, never a real model claim.
      // Distinct tool-call IDs exercise the stable backend business key.
      faux.setResponses([
        fauxAssistantMessage(fauxToolCall('nexloop.service.request',{message:config.deterministic_effect_message},{id:'service-request-first'}),{stopReason:'toolUse'}),
        fauxAssistantMessage(fauxToolCall('nexloop.service.request',{message:config.deterministic_effect_message},{id:'service-request-rebuilt'}),{stopReason:'toolUse'}),
        context=>{
          const previous=context.messages.slice().reverse().find(message=>message.role==='toolResult'&&message.toolName==='nexloop.service.request'&&!message.isError);
          const text=previous?.role==='toolResult'?previous.content.find(content=>content.type==='text'):undefined;
          let receipt:unknown;
          try{receipt=JSON.parse(text?.type==='text'?text.text:'');}catch{throw new RuntimeError('deterministic_effect_receipt_missing');}
          const intent=record(receipt).intent_id;
          if(typeof intent!=='string')throw new RuntimeError('deterministic_effect_receipt_missing');
          return fauxAssistantMessage(fauxToolCall('nexloop.service.find',{intent_id:intent},{id:'service-receipt-find'}),{stopReason:'toolUse'});
        },
        fauxAssistantMessage('Deterministic service intent test completion'),
      ]);
    }else faux.setResponses(Array.from({length:64},()=>fauxAssistantMessage('Deterministic test runtime completion')));
    const effects=config.effect_tools===true?new RuntimeEffectClient({guardURL:this.guard,caPath:this.caPath,keyPath:this.keyPath,privateMaterial,
      activationForRun:runId=>this.activations.get(runId)?.ref}):undefined;
    this.adapter=new PiRuntimeAdapter({root,models,model:{provider:'faux',modelId:'faux-1'},tools:[],toolsForRun:effects?command=>effects.toolsForRun(command):undefined,costPolicy:{kind:'deterministic_zero',currency:'USD'},assertOwner,
      authorize:async(command,operation)=>guard(command,operation)});
  }
  private readonly authorizeAdmission:(command:RunCommand,operation:string,input?:string,ref?:string)=>Promise<{ever_execution_authorized:boolean}>;
  async dispatch(operation:string,body:Record<string,unknown>){
    if(!['start','resume','inspect','cancel'].includes(operation))throw new RuntimeError('invalid_runtime_request');
    const expected=operation==='start'||operation==='resume'?['activation_ref','command','input']:['activation_ref','command'];
    if(Object.keys(body).sort().join(',')!==expected.sort().join(',')||typeof body.activation_ref!=='string'||!/^activation_[A-Za-z0-9_-]{16,200}$/.test(body.activation_ref))throw new RuntimeError('invalid_runtime_request');
    const command=validateRunCommand(body.command);
    if(command.runtime_profile!=='deterministic-test'||((operation==='start'||operation==='resume')&&typeof body.input!=='string'))throw new RuntimeError('invalid_runtime_request');
    if(this.pending>=8)throw new RuntimeError('runtime_busy');this.pending++;
    try{
      await this.authorizeAdmission(command,operation,body.input as string|undefined,body.activation_ref);
      this.activations.set(command.run_id,{ref:body.activation_ref,input:typeof body.input==='string'?body.input:this.activations.get(command.run_id)?.input});
      if(operation==='start'||operation==='resume')return await this.adapter[operation](command,body.input as string);
      if(operation==='inspect')return await this.adapter.inspect(command);
      return await this.adapter.cancel(command);
    }finally{this.pending--;}
  }
  async close(){await this.adapter.close();}
}
