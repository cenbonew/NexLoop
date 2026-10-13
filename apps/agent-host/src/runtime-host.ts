/** Optional internal Run admission. Backend retains all EIOS/PG credentials. */
import {CONTEXT_PROTOCOL,CONTEXT_PROTOCOL_V2,CONTEXT_PROTOCOL_V3,CONTEXT_PROTOCOL_V4,CONTEXT_PROTOCOL_V5,CONTEXT_PROTOCOL_V6,validateContextInput,validateContextProviderInput,validateContextAttestation,type ContextAttestation,ENTERPRISE_ACTION,type EffectSupply} from './context-input.js';
import {request as httpsRequest} from 'node:https';
import {type IncomingMessage} from 'node:http';
import {refillingResponses} from './deterministic-queue.js';
import {delayed,latencySampler,parseDeterministicLatency} from './deterministic-latency.js';
import {createModels,fauxProvider,fauxAssistantMessage,fauxToolCall,type Context} from '@earendil-works/pi-ai';
import {PiRuntimeAdapter,type ModelRequestSnapshot,type ModelResult} from './pi-runtime-adapter.js';
import {RuntimeEffectClient,requestScope} from './runtime-effect-tools.js';
import {RuntimeError,validateRunCommand,type RunCommand} from './runtime-adapter.js';
import {selectTrustedModel,validateEstimatedReservation} from './trusted-model-profile.js';
import {RunAdmissionGate,validateAdmissionConfiguration} from './run-admission-gate.js';

type Activation={ref:string;input?:string;command?:RunCommand;context?:ContextAttestation};
// ADR-022 §4: each guard call keeps its 2 s limit; an unanswered call is retried a bounded number of times.
export const GUARD_TIMEOUT_MS=2000,GUARD_ATTEMPTS=3,GUARD_BACKOFF_MS=100;
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
/** ADR-030: the enterprise offering of a Message Run's context (effect_supplies), if any. Availability of the tool only;
 * the context was validated at admission and the guard rechecks the Run credential and the catalog on every call. */
function enterpriseSupply(input:unknown):EffectSupply|undefined{
  if(typeof input!=='string')return undefined;
  try{
    const supplies=(JSON.parse(input) as {effect_supplies?:unknown}).effect_supplies;
    if(!Array.isArray(supplies))return undefined;
    const item=supplies.find(row=>row&&typeof row==='object'&&(row as {action?:unknown}).action===ENTERPRISE_ACTION) as Record<string,unknown>|undefined;
    const properties=item?.properties as Record<string,unknown>|undefined;
    if(!item||!properties||typeof item.offering_id!=='string'||typeof item.offering_revision!=='number'||typeof properties.service_code!=='string'
      ||typeof properties.title!=='string'||typeof properties.price_amount!=='string')return undefined;
    return {action:ENTERPRISE_ACTION,offering_id:item.offering_id,offering_revision:item.offering_revision,service_code:properties.service_code,
      title:properties.title,price_amount:properties.price_amount};
  }catch{return undefined;}
}
export class RuntimeHost{
  private readonly adapter:PiRuntimeAdapter;
  private readonly activations=new Map<string,Activation>();
  private readonly guard:URL;
  private readonly caPath:string;
  private readonly keyPath:string;
  private pending=0;
  private readonly runtimeProfile:string;
  private readonly admission:RunAdmissionGate;
  constructor(root:string,configPath:string,privateMaterial:Material,assertOwner:()=>void){
    const config=record(JSON.parse(privateMaterial(configPath,32768).toString('utf8')));
    const required=['guard_ca_file','guard_key_file','guard_url','runtime_profile'];
    if(required.some(key=>!Object.hasOwn(config,key))||Object.keys(config).some(key=>!required.includes(key)&&!['effect_tools','incentive_tool','maximum_active_runs','run_admission_wait_ms','deterministic_effect_request_scope','deterministic_effect_message','deterministic_message_from_input','deterministic_relationship_from_context','context_input_protocol','model_configuration_file','maximum_request_cost','plan_outcome_tool','deterministic_plan_outcome','deterministic_reply_once','deterministic_latency','deterministic_protocols'].includes(key))||!['deterministic-test','deepseek-flash'].includes(String(config.runtime_profile)))throw new Error('runtime configuration refused');
    if(config.runtime_profile==='deterministic-test'&&(config.model_configuration_file!==undefined||config.maximum_request_cost!==undefined))throw new Error('runtime configuration refused');
    if(config.runtime_profile==='deepseek-flash'&&(typeof config.model_configuration_file!=='string'||typeof config.maximum_request_cost!=='string'||!/^\d{1,8}(\.\d{1,8})?$/.test(config.maximum_request_cost)||Number(config.maximum_request_cost)<=0||Number(config.maximum_request_cost)>100||config.deterministic_effect_message!==undefined||config.deterministic_message_from_input!==undefined))throw new Error('runtime configuration refused');
    if(config.effect_tools!==undefined&&typeof config.effect_tools!=='boolean')throw new Error('runtime configuration refused');
    // NX-031 D5: injected model latency, deterministic-test only (refused for any other profile) and only with a per-context
    // scripted protocol (message from input, plan outcome, fallback reply); it sleeps, it never changes a response.
    // NX-031 slice 3: one deterministic-test Host serves every Run kind of the load lane; each Run's protocol is the one its start
    // input declares (schema_version): v2 message Runs, v6 plan-reevaluation Runs (a plan in the pack), v6 fallback-reply Runs.
    if(config.deterministic_protocols!==undefined&&(config.deterministic_protocols!==true||config.runtime_profile!=='deterministic-test'||config.effect_tools!==true
      ||config.plan_outcome_tool!==true||config.context_input_protocol!==undefined||config.deterministic_message_from_input!==undefined||config.deterministic_reply_once!==undefined
      ||config.deterministic_effect_message!==undefined||config.deterministic_effect_request_scope!==undefined||config.deterministic_relationship_from_context!==undefined))
      throw new Error('runtime configuration refused');
    const multi=config.deterministic_protocols===true;
    const latency=parseDeterministicLatency(config.deterministic_latency,config.runtime_profile);
    if(latency&&!multi&&config.deterministic_message_from_input!==true&&config.deterministic_plan_outcome===undefined&&config.deterministic_reply_once===undefined)throw new Error('runtime configuration refused');
    const sample=latency?latencySampler(latency):undefined;
    const timed=(response:(context:Context)=>ReturnType<typeof fauxAssistantMessage>)=>sample?delayed(response,sample):response;
    // ADR-022 §4: global active-Run limit of this Host (default 4); doctor binds the deployed value to <= 4.
    const limits=validateAdmissionConfiguration(config.maximum_active_runs,config.run_admission_wait_ms);
    this.admission=new RunAdmissionGate(limits.maximum,limits.waitMs);
    // NX-024: the run-outcome tool of plan reevaluation Runs, only with the Run-bound tool bridge.
    if(config.plan_outcome_tool!==undefined&&(config.plan_outcome_tool!==true||config.effect_tools!==true))throw new Error('runtime configuration refused');
    // AT-043: the incentive tool rides on the effect tools; SQL still requires the Run's credential to name the Action.
    if(config.incentive_tool!==undefined&&(config.incentive_tool!==true||config.effect_tools!==true))throw new Error('runtime configuration refused');
    if(config.deterministic_effect_message!==undefined&&(config.effect_tools!==true||typeof config.deterministic_effect_message!=='string'||[...config.deterministic_effect_message].length<1||[...config.deterministic_effect_message].length>8192))throw new Error('runtime configuration refused');
    if(config.deterministic_message_from_input!==undefined&&(config.deterministic_message_from_input!==true||config.runtime_profile!=='deterministic-test'||config.effect_tools!==true||config.deterministic_effect_message!==undefined))throw new Error('runtime configuration refused');
    // NX-025: explicit synthetic plan-reevaluation protocol (test profile only; records a fixed run-outcome for the plan in the v6 pack).
    if(config.deterministic_plan_outcome!==undefined&&(!['no_action','action_intent'].includes(String(config.deterministic_plan_outcome))||config.runtime_profile!=='deterministic-test'||config.effect_tools!==true
      ||config.plan_outcome_tool!==true||(config.context_input_protocol!==CONTEXT_PROTOCOL_V6&&!multi)||config.deterministic_message_from_input!==undefined||config.deterministic_effect_message!==undefined))throw new Error('runtime configuration refused');
    // NX-025 / ADR-023: explicit synthetic fallback-reply protocol (test profile only; one bound reply from the v6 message pack).
    if(config.deterministic_reply_once!==undefined&&(config.deterministic_reply_once!==true||config.runtime_profile!=='deterministic-test'||config.effect_tools!==true
      ||config.context_input_protocol!==CONTEXT_PROTOCOL_V6||config.deterministic_message_from_input!==undefined||config.deterministic_effect_message!==undefined
      ||config.deterministic_plan_outcome!==undefined))throw new Error('runtime configuration refused');
    if(config.context_input_protocol!==undefined&&(![CONTEXT_PROTOCOL,CONTEXT_PROTOCOL_V2,CONTEXT_PROTOCOL_V3,CONTEXT_PROTOCOL_V4,CONTEXT_PROTOCOL_V5,CONTEXT_PROTOCOL_V6].includes(config.context_input_protocol as string)||(config.runtime_profile==='deterministic-test'&&config.deterministic_message_from_input!==true&&config.deterministic_plan_outcome===undefined&&config.deterministic_reply_once===undefined)||(config.runtime_profile==='deepseek-flash'&&config.effect_tools!==true)))throw new Error('runtime configuration refused');
    if(config.deterministic_effect_request_scope!==undefined&&(config.runtime_profile!=='deterministic-test'||config.deterministic_message_from_input!==true||config.effect_tools!==true))throw new Error('runtime configuration refused');
    const syntheticScope=config.deterministic_effect_request_scope===undefined?undefined:requestScope(config.deterministic_effect_request_scope);
    if(config.deterministic_relationship_from_context!==undefined&&(config.deterministic_relationship_from_context!==true||config.runtime_profile!=='deterministic-test'||(config.context_input_protocol!==CONTEXT_PROTOCOL_V4&&config.context_input_protocol!==CONTEXT_PROTOCOL_V6)||config.deterministic_message_from_input!==true))throw new Error('runtime configuration refused');
    // Multi-protocol: no Host-wide protocol; each Run's pack is validated under its own declared protocol (v2 or v6 only).
    const contextProtocol=multi?undefined:config.context_input_protocol===CONTEXT_PROTOCOL_V6?CONTEXT_PROTOCOL_V6:config.context_input_protocol===CONTEXT_PROTOCOL_V5?CONTEXT_PROTOCOL_V5:config.context_input_protocol===CONTEXT_PROTOCOL_V4?CONTEXT_PROTOCOL_V4:config.context_input_protocol===CONTEXT_PROTOCOL_V3?CONTEXT_PROTOCOL_V3:config.context_input_protocol===CONTEXT_PROTOCOL_V2?CONTEXT_PROTOCOL_V2:CONTEXT_PROTOCOL;
    const contextMode=multi||config.context_input_protocol===CONTEXT_PROTOCOL_V6||config.context_input_protocol===CONTEXT_PROTOCOL||config.context_input_protocol===CONTEXT_PROTOCOL_V2||config.context_input_protocol===CONTEXT_PROTOCOL_V3||config.context_input_protocol===CONTEXT_PROTOCOL_V4||config.context_input_protocol===CONTEXT_PROTOCOL_V5;
    const declared=(input:unknown)=>{if(typeof input!=='string')return undefined;try{const v=record(JSON.parse(input)).schema_version;return v===CONTEXT_PROTOCOL_V2||v===CONTEXT_PROTOCOL_V6?v:undefined;}catch{return undefined;}};
    const runProtocol=(runId:string)=>multi?declared(this.activations.get(runId)?.input):contextProtocol;
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
      if((requestSnapshot!==undefined||modelResult!==undefined)&&(operation!=='model'||runProtocol(command.run_id)!==CONTEXT_PROTOCOL_V6||(requestSnapshot!==undefined&&modelResult!==undefined)))throw new RuntimeError('runtime_authorization_denied');
      const payload=Buffer.from(JSON.stringify({activation_ref:ref,command,operation,...(input===undefined?{}:{input}),...(requestSnapshot===undefined?{}:{request_snapshot:requestSnapshot}),...(modelResult===undefined?{}:{model_result:modelResult})}));
      // NX-031 (dispatcher 2026-10-11): a guard that did not answer (timeout, connection error, 503) is not a denial. The same
      // authorization is retried within the command's time limit (each call keeps the 2 s limit of ADR-022 §4); the guard records
      // a model call idempotently by call_sequence, and no external effect happens before authorization. Only an explicit answer
      // (403/400, or a 200 that fails validation) is runtime_authorization_denied.
      for(let attempt=1;;attempt++){
        try{return await guardOnce(payload,key,command,operation,input,active);}
        catch(error){
          if(!(error instanceof RuntimeError)||error.code!=='runtime_authorization_unavailable'||attempt>=GUARD_ATTEMPTS)throw error;
          const backoff=GUARD_BACKOFF_MS*attempt;
          if(Date.parse(command.not_after)<=Date.now()+backoff)throw error;
          await new Promise(resolve=>setTimeout(resolve,backoff));
        }
      }
    };
    const guardOnce=(payload:Buffer,key:string,command:RunCommand,operation:string,input:string|undefined,active:Activation|undefined)=>{
      const unavailable=()=>new RuntimeError('runtime_authorization_unavailable'),denied=()=>new RuntimeError('runtime_authorization_denied');
      return new Promise<{ever_execution_authorized:boolean}>((resolve,reject)=>{
        const request=httpsRequest(this.guard,{method:'POST',ca:privateMaterial(this.caPath,32768),minVersion:'TLSv1.2',agent:false,signal:AbortSignal.timeout(GUARD_TIMEOUT_MS),
          headers:{Authorization:'Bearer '+key,'Content-Type':'application/json','Content-Length':payload.length}},response=>{
          let size=0;const chunks:Buffer[]=[];
          response.on('data',(chunk:Buffer)=>{size+=chunk.length;if(size>32768){request.destroy();reject(denied());}else chunks.push(chunk);});
          response.on('error',()=>reject(unavailable()));
          response.on('end',()=>{if(response.statusCode===503){reject(unavailable());return;}
            try{const result=record(JSON.parse(Buffer.concat(chunks).toString('utf8')));if(response.statusCode!==200||result.authorized!==true||result.run_id!==command.run_id||typeof result.ever_execution_authorized!=='boolean'||(['model','tool'].includes(operation)&&result.ever_execution_authorized!==true))throw new Error();
            // Inspect authorization scope (0171, ADR-027): an inspect is authorized by the activation core only and its
            // answer must carry no Context; every other operation still validates the Run's Context attestation.
            if(operation==='inspect'&&result.context_artifact!==undefined)throw new Error();
            if(contextMode&&operation!=='inspect'){
            const attestation=validateContextAttestation(command,result.context_artifact);
            const original=input??active?.input;
            if(original!==undefined){
              if(multi&&declared(original)===undefined)throw new Error();
              validateContextProviderInput(original,command,attestation,contextProtocol);
            }
            if(active){active.context=attestation;active.command=command;}
          }
          resolve({ever_execution_authorized:result.ever_execution_authorized});}catch{reject(denied());}});
        });
        request.setTimeout(GUARD_TIMEOUT_MS,()=>request.destroy());request.on('error',()=>reject(unavailable()));request.end(payload);
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
    const scripted:{reply?:(context:Context)=>ReturnType<typeof fauxAssistantMessage>;plan?:(context:Context)=>ReturnType<typeof fauxAssistantMessage>;message?:(context:Context)=>ReturnType<typeof fauxAssistantMessage>}={};
    if(config.deterministic_reply_once===true||multi){
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
      scripted.reply=response;
      if(!multi)faux.setResponses(refillingResponses(faux,timed(response),16));
    }
    if(config.deterministic_plan_outcome!==undefined||multi){
      // Explicit synthetic plan-reevaluation protocol, not semantic reasoning: reads the one plan the bound v6 pack carries
      // and records a fixed outcome through nexloop.plan.outcome (action_intent first submits one governed service intent).
      const mode=String(config.deterministic_plan_outcome??'no_action');
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
      scripted.plan=response;
      if(!multi)faux.setResponses(refillingResponses(faux,timed(response),16));
    }
    if(config.deterministic_message_from_input===true||multi){
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
      scripted.message=response;
      if(!multi)faux.setResponses(refillingResponses(faux,timed(response),64));
    }
    if(!multi&&config.deterministic_reply_once!==true&&config.deterministic_plan_outcome===undefined&&config.deterministic_message_from_input!==true&&config.effect_tools===true&&typeof config.deterministic_effect_message==='string'){
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
    }else if(!multi&&config.deterministic_reply_once!==true&&config.deterministic_plan_outcome===undefined&&config.deterministic_message_from_input!==true)faux.setResponses(Array.from({length:64},()=>fauxAssistantMessage('Deterministic test runtime completion')));
    if(multi){
      // Each model turn follows its own Run's declared protocol: v2 → message, v6 with a plan → plan outcome, other v6 → fallback reply.
      const route=(context:Context)=>{
        let index=context.messages.length-1;while(index>=0&&context.messages[index]?.role!=='user')index--;
        const user=context.messages[index];
        if(user?.role!=='user')throw new RuntimeError('deterministic_input_missing');
        const raw=typeof user.content==='string'?user.content:user.content.map(part=>{if(part.type!=='text')throw new RuntimeError('deterministic_input_invalid');return part.text;}).join('');
        let unpacked:unknown;try{unpacked=JSON.parse(raw);}catch{throw new RuntimeError('runtime_context_invalid');}
        const protocol=declared(raw),run=record(record(unpacked).bindings).run_id;
        const bound=typeof run==='string'?this.activations.get(run):undefined;
        if(!protocol||!bound?.command||!bound.context||declared(bound.input)!==protocol)throw new RuntimeError('runtime_context_invalid');
        if(protocol===CONTEXT_PROTOCOL_V2)return scripted.message!(context);
        return (validateContextInput(raw,bound.command,bound.context,CONTEXT_PROTOCOL_V6).plans?.length?scripted.plan!:scripted.reply!)(context);
      };
      faux.setResponses(refillingResponses(faux,timed(route),64));
    }
    const effects=config.effect_tools===true?new RuntimeEffectClient({guardURL:this.guard,caPath:this.caPath,keyPath:this.keyPath,privateMaterial,
      activationForRun:runId=>this.activations.get(runId)?.ref}):undefined;
    this.adapter=new PiRuntimeAdapter({root,models:selection.models,model:selection.model,tools:[],toolsForRun:effects?command=>effects.toolsForRun(command,{planOutcome:config.plan_outcome_tool===true,incentive:config.incentive_tool===true,enterprise:enterpriseSupply(this.activations.get(command.run_id)?.input)}):undefined,costPolicy:selection.costPolicy,assertOwner,
      // v6 Runs record each actual model request (AT-027); in multi-protocol mode that is decided per Run.
      recordModelRequests:multi?(command:RunCommand)=>runProtocol(command.run_id)===CONTEXT_PROTOCOL_V6:contextProtocol===CONTEXT_PROTOCOL_V6,
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
    // A new Run waits for a slot before any guard request (no proof is built while waiting).
    let slot=operation==='start'||operation==='resume'?await this.admission.acquire(command.run_id,Date.parse(command.not_after)).catch(error=>{this.pending--;throw error;}):undefined;
    try{
      await this.authorizeAdmission(command,operation,body.input as string|undefined,body.activation_ref);
      this.activations.set(command.run_id,{ref:body.activation_ref,input:typeof body.input==='string'?body.input:this.activations.get(command.run_id)?.input,command,context:this.activations.get(command.run_id)?.context});
      if(operation==='start'||operation==='resume'){
        const receipt=await this.adapter[operation](command,body.input as string);
        if(slot){const held=slot;slot=undefined;void this.adapter.settled(command).finally(()=>held.release());}
        return receipt;
      }
      if(operation==='inspect')return await this.adapter.inspect(command);
      return await this.adapter.cancel(command);
    }finally{this.pending--;slot?.release();}
  }
  /** NX-029 D8: purge one settled Run's directory; body {run_id} only. */
  async purge(body:Record<string,unknown>){
    if(Object.keys(body).join(',')!=='run_id'||typeof body.run_id!=='string')throw new RuntimeError('invalid_runtime_request');
    const runId=body.run_id.toLowerCase(),outcome=await this.adapter.purge(runId);
    this.activations.delete(runId);
    return {run_id:runId,outcome};
  }
  async close(){await this.adapter.close();}
}
