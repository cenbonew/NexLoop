/** Optional internal Run admission. Backend retains all EIOS/PG credentials. */
import {CONTEXT_PROTOCOL,CONTEXT_PROTOCOL_V2,CONTEXT_PROTOCOL_V3,CONTEXT_PROTOCOL_V4,CONTEXT_PROTOCOL_V5,CONTEXT_PROTOCOL_V6,validateContextInput,validateContextProviderInput,validateContextAttestation,type ContextAttestation} from './context-input.js';
import {request as httpsRequest} from 'node:https';
import {type IncomingMessage} from 'node:http';
import {createModels,fauxProvider,fauxAssistantMessage,fauxToolCall,type Context} from '@earendil-works/pi-ai';
import {PiRuntimeAdapter,type ModelRequestSnapshot,type ModelResult} from './pi-runtime-adapter.js';
import {RuntimeEffectClient,requestScope} from './runtime-effect-tools.js';
import {RuntimeError,validateRunCommand,type RunCommand} from './runtime-adapter.js';
import {selectTrustedModel,validateEstimatedReservation} from './trusted-model-profile.js';

type Material=(path:string,maximum:number,allowEmpty?:boolean)=>Buffer;
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
  private readonly activations=new Map<string,{ref:string;input?:string;command?:RunCommand;context?:ContextAttestation}>();
  private readonly guard:URL;
  private readonly caPath:string;
  private readonly keyPath:string;
  private pending=0;
  private readonly runtimeProfile:string;
  constructor(root:string,configPath:string,privateMaterial:Material,assertOwner:()=>void){
    const config=record(JSON.parse(privateMaterial(configPath,32768).toString('utf8')));
    const required=['guard_ca_file','guard_key_file','guard_url','runtime_profile'];
    if(required.some(key=>!Object.hasOwn(config,key))||Object.keys(config).some(key=>!required.includes(key)&&!['effect_tools','deterministic_effect_request_scope','deterministic_effect_message','deterministic_message_from_input','deterministic_relationship_from_context','context_input_protocol','model_configuration_file','maximum_request_cost','plan_outcome_tool','deterministic_plan_outcome','deterministic_reply_once'].includes(key))||!['deterministic-test','deepseek-flash'].includes(String(config.runtime_profile)))throw new Error('runtime configuration refused');
    if(config.runtime_profile==='deterministic-test'&&(config.model_configuration_file!==undefined||config.maximum_request_cost!==undefined))throw new Error('runtime configuration refused');
    if(config.runtime_profile==='deepseek-flash'&&(typeof config.model_configuration_file!=='string'||typeof config.maximum_request_cost!=='string'||!/^\d{1,8}(\.\d{1,8})?$/.test(config.maximum_request_cost)||Number(config.maximum_request_cost)<=0||Number(config.maximum_request_cost)>100||config.deterministic_effect_message!==undefined||config.deterministic_message_from_input!==undefined))throw new Error('runtime configuration refused');
    if(config.effect_tools!==undefined&&typeof config.effect_tools!=='boolean')throw new Error('runtime configuration refused');
    // NX-024: the run-outcome tool of plan reevaluation Runs, only with the Run-bound tool bridge.
    if(config.plan_outcome_tool!==undefined&&(config.plan_outcome_tool!==true||config.effect_tools!==true))throw new Error('runtime configuration refused');
    if(config.deterministic_effect_message!==undefined&&(config.effect_tools!==true||typeof config.deterministic_effect_message!=='string'||[...config.deterministic_effect_message].length<1||[...config.deterministic_effect_message].length>8192))throw new Error('runtime configuration refused');
    if(config.deterministic_message_from_input!==undefined&&(config.deterministic_message_from_input!==true||config.runtime_profile!=='deterministic-test'||config.effect_tools!==true||config.deterministic_effect_message!==undefined))throw new Error('runtime configuration refused');
    // NX-025: explicit synthetic plan-reevaluation protocol (test profile only; records a fixed run-outcome for the plan in the v6 pack).
    if(config.deterministic_plan_outcome!==undefined&&(!['no_action','action_intent'].includes(String(config.deterministic_plan_outcome))||config.runtime_profile!=='deterministic-test'||config.effect_tools!==true
      ||config.plan_outcome_tool!==true||config.context_input_protocol!==CONTEXT_PROTOCOL_V6||config.deterministic_message_from_input!==undefined||config.deterministic_effect_message!==undefined))throw new Error('runtime configuration refused');
    // NX-025 / ADR-023: explicit synthetic fallback-reply protocol (test profile only; one bound reply from the v6 message pack).
    if(config.deterministic_reply_once!==undefined&&(config.deterministic_reply_once!==true||config.runtime_profile!=='deterministic-test'||config.effect_tools!==true
      ||config.context_input_protocol!==CONTEXT_PROTOCOL_V6||config.deterministic_message_from_input!==undefined||config.deterministic_effect_message!==undefined
      ||config.deterministic_plan_outcome!==undefined))throw new Error('runtime configuration refused');
    if(config.context_input_protocol!==undefined&&(![CONTEXT_PROTOCOL,CONTEXT_PROTOCOL_V2,CONTEXT_PROTOCOL_V3,CONTEXT_PROTOCOL_V4,CONTEXT_PROTOCOL_V5,CONTEXT_PROTOCOL_V6].includes(config.context_input_protocol as string)||(config.runtime_profile==='deterministic-test'&&config.deterministic_message_from_input!==true&&config.deterministic_plan_outcome===undefined&&config.deterministic_reply_once===undefined)||(config.runtime_profile==='deepseek-flash'&&config.effect_tools!==true)))throw new Error('runtime configuration refused');
    if(config.deterministic_effect_request_scope!==undefined&&(config.runtime_profile!=='deterministic-test'||config.deterministic_message_from_input!==true||config.effect_tools!==true))throw new Error('runtime configuration refused');
    const syntheticScope=config.deterministic_effect_request_scope===undefined?undefined:requestScope(config.deterministic_effect_request_scope);
    if(config.deterministic_relationship_from_context!==undefined&&(config.deterministic_relationship_from_context!==true||config.runtime_profile!=='deterministic-test'||(config.context_input_protocol!==CONTEXT_PROTOCOL_V4&&config.context_input_protocol!==CONTEXT_PROTOCOL_V6)||config.deterministic_message_from_input!==true))throw new Error('runtime configuration refused');
    const contextProtocol=config.context_input_protocol===CONTEXT_PROTOCOL_V6?CONTEXT_PROTOCOL_V6:config.context_input_protocol===CONTEXT_PROTOCOL_V5?CONTEXT_PROTOCOL_V5:config.context_input_protocol===CONTEXT_PROTOCOL_V4?CONTEXT_PROTOCOL_V4:config.context_input_protocol===CONTEXT_PROTOCOL_V3?CONTEXT_PROTOCOL_V3:config.context_input_protocol===CONTEXT_PROTOCOL_V2?CONTEXT_PROTOCOL_V2:CONTEXT_PROTOCOL;
    const contextMode=config.context_input_protocol===CONTEXT_PROTOCOL_V6||config.context_input_protocol===CONTEXT_PROTOCOL||config.context_input_protocol===CONTEXT_PROTOCOL_V2||config.context_input_protocol===CONTEXT_PROTOCOL_V3||config.context_input_protocol===CONTEXT_PROTOCOL_V4||config.context_input_protocol===CONTEXT_PROTOCOL_V5;
    this.guard=new URL(String(config.guard_url));
    if(this.guard.protocol!=='https:'||this.guard.hostname!=='127.0.0.1'||!this.guard.port||Number(this.guard.port)<1024||Number(this.guard.port)>65535||this.guard.username||this.guard.password||this.guard.search||this.guard.hash||this.guard.pathname!=='/internal/v1/runtime/authorize')throw new Error('runtime guard refused');
    if(typeof config.guard_ca_file!=='string'||typeof config.guard_key_file!=='string')throw new Error('runtime guard files required');
    this.caPath=config.guard_ca_file;this.keyPath=config.guard_key_file;
    const guard=async(command:RunCommand,operation:string,input?:string,activationRef?:string,requestSnapshot?:ModelRequestSnapshot,modelResult?:ModelResult)=>{
      const active=this.activations.get(command.run_id);
      const ref=activationRef??active?.ref;
      if(input===undefined&&(operation==='start'||operation==='resume'))input=active?.input;
      if(!ref)throw new RuntimeError('runtime_authorization_denied');
      const key=privateMaterial(this.keyPath,64).toString('utf8');
      if(!/^[0-9a-f]{64}$/.test(key))throw new RuntimeError('runtime_authorization_denied');
      // v6: each actual model request is recorded by the guard before the call (AT-027).
      if((requestSnapshot!==undefined||modelResult!==undefined)&&(operation!=='model'||contextProtocol!==CONTEXT_PROTOCOL_V6||(requestSnapshot!==undefined&&modelResult!==undefined)))throw new RuntimeError('runtime_authorization_denied');
      const payload=Buffer.from(JSON.stringify({activation_ref:ref,command,operation,...(input===undefined?{}:{input}),...(requestSnapshot===undefined?{}:{request_snapshot:requestSnapshot}),...(modelResult===undefined?{}:{model_result:modelResult})}));
      return await new Promise<{ever_execution_authorized:boolean}>((resolve,reject)=>{
        const request=httpsRequest(this.guard,{method:'POST',ca:privateMaterial(this.caPath,32768),minVersion:'TLSv1.2',agent:false,signal:AbortSignal.timeout(2000),
          headers:{Authorization:'Bearer '+key,'Content-Type':'application/json','Content-Length':payload.length}},response=>{
          let size=0;const chunks:Buffer[]=[];
          response.on('data',(chunk:Buffer)=>{size+=chunk.length;if(size>32768){request.destroy();reject(new RuntimeError('runtime_authorization_denied'));}else chunks.push(chunk);});
          response.on('error',()=>reject(new RuntimeError('runtime_authorization_denied')));
          response.on('end',()=>{try{const result=record(JSON.parse(Buffer.concat(chunks).toString('utf8')));if(response.statusCode!==200||result.authorized!==true||result.run_id!==command.run_id||typeof result.ever_execution_authorized!=='boolean'||(['model','tool'].includes(operation)&&result.ever_execution_authorized!==true))throw new Error();if(contextMode){
            const attestation=validateContextAttestation(command,result.context_artifact);
            const original=input??active?.input;
            if(original!==undefined)validateContextProviderInput(original,command,attestation,contextProtocol);
            if(active){active.context=attestation;active.command=command;}
          }
          resolve({ever_execution_authorized:result.ever_execution_authorized});}catch{reject(new RuntimeError('runtime_authorization_denied'));}});
        });
        request.setTimeout(2000,()=>request.destroy());request.on('error',()=>reject(new RuntimeError('runtime_authorization_denied')));request.end(payload);
      });
    };
    this.authorizeAdmission=guard;
    const models=createModels({authContext:{env:async()=>undefined,fileExists:async()=>false}}),faux=fauxProvider();models.setProvider(faux.provider);
    let selection:{models:typeof models|import('@earendil-works/pi-ai').Models;model:{provider:string;modelId:string};runtimeProfile:string;costPolicy:import('./pi-runtime-adapter.js').PiRuntimeOptions['costPolicy']}={models,model:{provider:'faux',modelId:'faux-1'},runtimeProfile:'deterministic-test',costPolicy:{kind:'deterministic_zero',currency:'USD'}};
    if(config.runtime_profile==='deepseek-flash'){
      const modelConfig=record(JSON.parse(privateMaterial(config.model_configuration_file as string,32768).toString('utf8')));
      if(Object.keys(modelConfig).some(key=>!['MODEL_PROVIDER','MODEL_ID','MODEL_BASE_URL','MODEL_CREDENTIALS_FILE','stage'].includes(key))||typeof modelConfig.stage!=='boolean'||Object.entries(modelConfig).some(([key,value])=>key!=='stage'&&typeof value!=='string'))throw new Error('runtime configuration refused');
      const environment:Record<string,string|undefined>={};
      for(const key of ['MODEL_PROVIDER','MODEL_ID','MODEL_BASE_URL','MODEL_CREDENTIALS_FILE'])environment[key]=modelConfig[key] as string|undefined;
      if(!modelConfig.stage)environment.MODEL_API_KEY=process.env.MODEL_API_KEY;
      const selected=selectTrustedModel(environment,privateMaterial,modelConfig.stage);
      if(selected.runtimeProfile==='deepseek-flash'){if(!selected.estimatedReservationFloor)throw new Error('runtime estimated cost reservation unavailable');validateEstimatedReservation(config.maximum_request_cost,selected.estimatedReservationFloor);}
      selection={...selected,costPolicy:selected.runtimeProfile==='deterministic-test'?{kind:'deterministic_zero',currency:'USD'}:{kind:'bounded_request',currency:'USD',maximum_request_cost:config.maximum_request_cost as string}};
    }
    this.runtimeProfile=selection.runtimeProfile;
    // Explicit test profile only; no model secret or real-provider success claim.
    if(config.deterministic_reply_once===true){
      // Explicit synthetic fallback-reply protocol, not semantic reasoning: one reply to the pack's own user statement.
      const response=(context:Context)=>{
        let index=context.messages.length-1;while(index>=0&&context.messages[index]?.role!=='user')index--;
        const user=context.messages[index];
        if(user?.role!=='user')throw new RuntimeError('deterministic_input_missing');
        const raw=typeof user.content==='string'?user.content:user.content.map(part=>{if(part.type!=='text')throw new RuntimeError('deterministic_input_invalid');return part.text;}).join('');
        let unpacked:unknown;try{unpacked=JSON.parse(raw);}catch{throw new RuntimeError('runtime_context_invalid');}
        const run=record(record(unpacked).bindings).run_id;
        const bound=typeof run==='string'?this.activations.get(run):undefined;
        if(!bound?.command||!bound.context)throw new RuntimeError('runtime_context_invalid');
        const current=validateContextInput(raw,bound.command,bound.context,contextProtocol);
        const after=context.messages.slice(index+1);
        if(after.some(item=>item.role==='toolResult'))return fauxAssistantMessage('Synthetic fallback reply protocol complete; no semantic inference.');
        return fauxAssistantMessage(fauxToolCall('nexloop.service.request',{message:'已收到您的来信：'+[...current.body].slice(0,60).join('')},{id:'fallback-reply'}),{stopReason:'toolUse'});
      };
      faux.setResponses(Array.from({length:16},()=>response));
    }else if(config.deterministic_plan_outcome!==undefined){
      // Explicit synthetic plan-reevaluation protocol, not semantic reasoning: reads the one plan the bound v6 pack carries
      // and records a fixed outcome through nexloop.plan.outcome (action_intent first submits one governed service intent).
      const mode=String(config.deterministic_plan_outcome);
      const response=(context:Context)=>{
        let index=context.messages.length-1;while(index>=0&&context.messages[index]?.role!=='user')index--;
        const user=context.messages[index];
        if(user?.role!=='user')throw new RuntimeError('deterministic_input_missing');
        const raw=typeof user.content==='string'?user.content:user.content.map(part=>{if(part.type!=='text')throw new RuntimeError('deterministic_input_invalid');return part.text;}).join('');
        let unpacked:unknown;try{unpacked=JSON.parse(raw);}catch{throw new RuntimeError('runtime_context_invalid');}
        const run=record(record(unpacked).bindings).run_id;
        const bound=typeof run==='string'?this.activations.get(run):undefined;
        if(!bound?.command||!bound.context)throw new RuntimeError('runtime_context_invalid');
        const current=validateContextInput(raw,bound.command,bound.context,contextProtocol);
        if(current.plans?.length!==1)throw new RuntimeError('deterministic_plan_missing');
        const plan=current.plans[0]!;
        const after=context.messages.slice(index+1);
        const recorded=after.find(item=>item.role==='toolResult'&&item.toolName==='nexloop.plan.outcome');
        if(recorded){if(recorded.role!=='toolResult'||recorded.isError)throw new RuntimeError('deterministic_plan_outcome_failed');return fauxAssistantMessage('Synthetic plan reevaluation protocol complete; no semantic inference.');}
        const base={schema_version:'1.0',reassess_at:null,evidence_refs:[plan.plan_ref],plan_update:null};
        if(mode==='no_action')return fauxAssistantMessage(fauxToolCall('nexloop.plan.outcome',{...base,kind:'no_action',reasons:[plan.plan_ref+'：当前无合理触达必要'],intent_ref:null},{id:'plan-outcome'}),{stopReason:'toolUse'});
        const submitted=after.find(item=>item.role==='toolResult'&&item.toolName==='nexloop.service.request');
        if(!submitted)return fauxAssistantMessage(fauxToolCall('nexloop.service.request',{message:current.body},{id:'plan-service-request'}),{stopReason:'toolUse'});
        if(submitted.role!=='toolResult'||submitted.isError)throw new RuntimeError('deterministic_effect_receipt_missing');
        const part=submitted.content.find(item=>item.type==='text');let receipt:unknown;
        try{receipt=JSON.parse(part?.type==='text'?part.text:'');}catch{throw new RuntimeError('deterministic_effect_receipt_missing');}
        const intent=record(receipt).intent_id;if(typeof intent!=='string')throw new RuntimeError('deterministic_effect_receipt_missing');
        return fauxAssistantMessage(fauxToolCall('nexloop.plan.outcome',{...base,kind:'action_intent',reasons:[plan.plan_ref+'：提交一次受治理服务意图'],intent_ref:intent},{id:'plan-outcome'}),{stopReason:'toolUse'});
      };
      faux.setResponses(Array.from({length:16},()=>response));
    }else if(config.deterministic_message_from_input===true){
      // Explicit synthetic byte-preserving test protocol, not semantic reasoning.
      // Use each conversation's genuine latest user message, never a global
      // response index or an ambient/private configured business output.
      const response=(context:Context)=>{
        let index=context.messages.length-1;while(index>=0&&context.messages[index]?.role!=='user')index--;
        const user=context.messages[index];
        if(user?.role!=='user')throw new RuntimeError('deterministic_input_missing');
        const raw=typeof user.content==='string'?user.content:user.content.map(part=>{
          if(part.type!=='text')throw new RuntimeError('deterministic_input_invalid');return part.text;
        }).join('');
        let message=raw;
        if(contextMode){
          let unpacked:unknown;try{unpacked=JSON.parse(raw);}catch{throw new RuntimeError('runtime_context_invalid');}
          const run=record(record(unpacked).bindings).run_id;
          if(typeof run!=='string')throw new RuntimeError('runtime_context_invalid');
          const bound=this.activations.get(run);
          if(!bound?.command||!bound.context)throw new RuntimeError('runtime_context_invalid');
          const currentContext=validateContextInput(raw,bound.command,bound.context,contextProtocol);
          message=currentContext.body;
          // Explicit deterministic fixture consumes the typed current statement,
          // never evidence or formal attributes; actual providers keep the whole pack.
          if(config.deterministic_relationship_from_context===true&&currentContext.relationship_context?.current_statements.length){
            if(currentContext.relationship_context.current_statements.length!==1)throw new RuntimeError('deterministic_relationship_ambiguous');
            message=currentContext.relationship_context.current_statements[0]!.conclusion;
          }
        }
        if(!message||[...message].length>8192||message.includes('\u0000'))throw new RuntimeError('deterministic_input_invalid');
        const current=context.messages.slice(index+1);
        const submitted=current.filter(item=>item.role==='toolResult'&&item.toolName==='nexloop.service.request');
        if(submitted.length<2)return fauxAssistantMessage(fauxToolCall('nexloop.service.request',{message,...(syntheticScope?{request_scope:syntheticScope}:{})},
          {id:submitted.length===0?'message-service-first':'message-service-rebuilt'}),{stopReason:'toolUse'});
        if(syntheticScope&&submitted.every(item=>item.role==='toolResult'&&item.isError))return fauxAssistantMessage('Synthetic scope refusal protocol complete; no fulfillment.');
        const found=current.some(item=>item.role==='toolResult'&&item.toolName==='nexloop.service.find');
        if(found)return fauxAssistantMessage('Synthetic message-driven protocol completion; no semantic inference.');
        const previous=submitted.at(-1);
        if(previous?.role!=='toolResult'||previous.isError)throw new RuntimeError('deterministic_effect_receipt_missing');
        const text=previous.content.find(part=>part.type==='text');let receipt:unknown;
        try{receipt=JSON.parse(text?.type==='text'?text.text:'');}catch{throw new RuntimeError('deterministic_effect_receipt_missing');}
        const intent=record(receipt).intent_id;if(typeof intent!=='string')throw new RuntimeError('deterministic_effect_receipt_missing');
        return fauxAssistantMessage(fauxToolCall('nexloop.service.find',{intent_id:intent},{id:'message-service-find'}),{stopReason:'toolUse'});
      };
      faux.setResponses(Array.from({length:64},()=>response));
    }else if(config.effect_tools===true&&typeof config.deterministic_effect_message==='string'){
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
    this.adapter=new PiRuntimeAdapter({root,models:selection.models,model:selection.model,tools:[],toolsForRun:effects?command=>effects.toolsForRun(command,{planOutcome:config.plan_outcome_tool===true}):undefined,costPolicy:selection.costPolicy,assertOwner,
      recordModelRequests:contextProtocol===CONTEXT_PROTOCOL_V6,
      authorize:async(command,operation,request,result)=>guard(command,operation,undefined,undefined,request,result)});
  }
  private readonly authorizeAdmission:(command:RunCommand,operation:string,input?:string,ref?:string)=>Promise<{ever_execution_authorized:boolean}>;
  async dispatch(operation:string,body:Record<string,unknown>){
    if(!['start','resume','inspect','cancel'].includes(operation))throw new RuntimeError('invalid_runtime_request');
    const expected=operation==='start'||operation==='resume'?['activation_ref','command','input']:['activation_ref','command'];
    if(Object.keys(body).sort().join(',')!==expected.sort().join(',')||typeof body.activation_ref!=='string'||!/^activation_[A-Za-z0-9_-]{16,200}$/.test(body.activation_ref))throw new RuntimeError('invalid_runtime_request');
    const command=validateRunCommand(body.command);
    if(command.runtime_profile!==this.runtimeProfile||((operation==='start'||operation==='resume')&&typeof body.input!=='string'))throw new RuntimeError('invalid_runtime_request');
    if(this.pending>=8)throw new RuntimeError('runtime_busy');this.pending++;
    try{
      await this.authorizeAdmission(command,operation,body.input as string|undefined,body.activation_ref);
      this.activations.set(command.run_id,{ref:body.activation_ref,input:typeof body.input==='string'?body.input:this.activations.get(command.run_id)?.input,command,context:this.activations.get(command.run_id)?.context});
      if(operation==='start'||operation==='resume')return await this.adapter[operation](command,body.input as string);
      if(operation==='inspect')return await this.adapter.inspect(command);
      return await this.adapter.cancel(command);
    }finally{this.pending--;}
  }
  async close(){await this.adapter.close();}
}
