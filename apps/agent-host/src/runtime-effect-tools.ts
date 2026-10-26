/** Run-bound intent tools. Backend owns every EIOS/PG/provider credential. */
import {request as httpsRequest} from 'node:https';
import {Type} from '@earendil-works/pi-ai';
import {defineTool,type ToolRegistration} from '@earendil-works/pi-durable';
import {RuntimeError,validateRunCommand,type RunCommand} from './runtime-adapter.js';
import {ENTERPRISE_ACTION,type EffectSupply} from './context-input.js';
const ENTERPRISE=ENTERPRISE_ACTION;

export type OutreachReceipt={run_id:string;intent_id:string;state:'accepted'|'dispatching'|'unknown'|'fulfilled'|'failed'|'refused';replayed:boolean;business_action_success:false};
const OUTREACH_CODES=['purpose_not_allowed','body_too_long','intent_exists','consumer_unavailable','invalid_outreach_request'];
export type IncentiveReceipt={run_id:string;intent_id:string;state:'accepted'|'dispatching'|'unknown'|'fulfilled'|'failed'|'refused';replayed:boolean;origin:'reply'|'plan'|'agent';business_action_success:false};
export type EffectRequestScope={offering_id:string;offering_revision:number;requested_guarantees:readonly string[];requested_discounts:readonly string[]};
export function requestScope(value:unknown):EffectRequestScope{
  const row=exact(value,['offering_id','offering_revision','requested_guarantees','requested_discounts']);
  if(typeof row.offering_id!=='string'||!sha.test(row.offering_id)||typeof row.offering_revision!=='number'||!Number.isSafeInteger(row.offering_revision)||row.offering_revision<1)throw new RuntimeError('runtime_effect_unavailable');
  const terms=(value:unknown):string[]=>{if(!Array.isArray(value)||value.length>16||value.some(term=>typeof term!=='string'||[...term].length<1||[...term].length>128||/\u0000|[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/u.test(term))||new Set(value).size!==value.length)throw new RuntimeError('runtime_effect_unavailable');return [...value] as string[];};
  return {offering_id:row.offering_id,offering_revision:row.offering_revision,requested_guarantees:terms(row.requested_guarantees),requested_discounts:terms(row.requested_discounts)};
}
type Material=(path:string,maximum:number)=>Buffer;
/** NX-024 run-outcome 1.0 (packages/contracts/run-outcome.schema.json); identity is derived by the server. */
export type RunOutcome={schema_version:'1.0';kind:'no_action'|'needs_information'|'waiting_external'|'escalate'|'plan_update'|'action_intent';reasons:string[];
  reassess_at:string|null;evidence_refs:string[];intent_ref:string|null;plan_update:{strategy:string|null;steps:Record<string,unknown>[]}|null};
export type RecordedOutcome={run_id:string;recorded:true;replay:boolean;kind:RunOutcome['kind'];plan_version:number;new_version:number|null};
const OUTCOME_KINDS=['no_action','needs_information','waiting_external','escalate','plan_update','action_intent'] as const;
const instant=/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,9})?(Z|\+00:00)$/;
const reference=/^[A-Za-z][A-Za-z0-9_.-]*:\S+$/;
const shortText=(value:unknown,maximum:number)=>typeof value==='string'&&[...value].length>=1&&[...value].length<=maximum&&!/\u0000/.test(value);
function planStep(value:unknown):Record<string,unknown>{
  const step=exact(value,['step_key','step_object_id','prerequisites','expected_result','stop_if','reassess_at','budget','intent_ref']);
  if(typeof step.step_key!=='string'||!/^[a-z0-9][a-z0-9._-]{0,63}$/.test(step.step_key)||!(step.step_object_id===null||(typeof step.step_object_id==='string'&&sha.test(step.step_object_id)))
    ||!Array.isArray(step.prerequisites)||step.prerequisites.length>32||step.prerequisites.some(item=>!shortText(item,500))||!shortText(step.expected_result,2000)
    ||!Array.isArray(step.stop_if)||step.stop_if.length>16||!(step.reassess_at===null||(typeof step.reassess_at==='string'&&instant.test(step.reassess_at)))
    ||!(step.intent_ref===null||(typeof step.intent_ref==='string'&&uuid.test(step.intent_ref))))throw new RuntimeError('invalid_run_outcome');
  for(const condition of step.stop_if){const row=exact(condition,['type_name','object_id','property','equals']);
    if(typeof row.type_name!=='string'||!/^[A-Za-z][A-Za-z0-9_]{0,63}$/.test(row.type_name)||typeof row.object_id!=='string'||!sha.test(row.object_id)||typeof row.property!=='string'||!/^[A-Za-z][A-Za-z0-9_]{0,63}$/.test(row.property))throw new RuntimeError('invalid_run_outcome');}
  const budget=exact(step.budget,['maximum_model_turns','maximum_tool_calls','active_timeout_seconds']);
  for(const [key,maximum] of [['maximum_model_turns',64],['maximum_tool_calls',128],['active_timeout_seconds',3600]] as const)
    if(typeof budget[key]!=='number'||!Number.isSafeInteger(budget[key])||(budget[key] as number)<1||(budget[key] as number)>maximum)throw new RuntimeError('invalid_run_outcome');
  return step;
}
export function runOutcome(value:unknown):RunOutcome{
  try{
    const row=exact(value,['schema_version','kind','reasons','reassess_at','evidence_refs','intent_ref','plan_update']);
    if(row.schema_version!=='1.0'||!OUTCOME_KINDS.includes(row.kind as RunOutcome['kind'])||!Array.isArray(row.reasons)||row.reasons.length>8||row.reasons.some(item=>!shortText(item,500))
      ||!Array.isArray(row.evidence_refs)||row.evidence_refs.length>32||row.evidence_refs.some(item=>typeof item!=='string'||item.length>512||!reference.test(item))
      ||!(row.reassess_at===null||(typeof row.reassess_at==='string'&&instant.test(row.reassess_at)))||!(row.intent_ref===null||(typeof row.intent_ref==='string'&&uuid.test(row.intent_ref))))throw new Error();
    if((row.kind==='plan_update')!==(row.plan_update!==null))throw new Error();
    if(row.kind==='action_intent'&&row.intent_ref===null)throw new Error();
    if(row.kind==='waiting_external'&&row.reassess_at===null&&row.intent_ref===null)throw new Error();
    if(row.plan_update!==null){const update=exact(row.plan_update,['strategy','steps']);
      if(!(update.strategy===null||shortText(update.strategy,8192))||!Array.isArray(update.steps)||update.steps.length<1||update.steps.length>32)throw new Error();
      update.steps.forEach(planStep);if(new Set(update.steps.map(step=>(step as {step_key:string}).step_key)).size!==update.steps.length)throw new Error();}
    return row as RunOutcome;
  }catch{throw new RuntimeError('invalid_run_outcome');}
}
export type EffectReceipt={intent_id:string;receipt_id:string;state:string;payload_digest:string;provider_payload_digest:string;scope:'effect_intent';business_action_success:boolean};
type Configuration={guardURL:URL;caPath:string;keyPath:string;privateMaterial:Material;activationForRun:(runId:string)=>string|undefined;
  /** NX-041 S6b: a simulation Host (world_group simulation): every effect tool records a stand-in intent instead. */
  simulation?:boolean;
  /** ADR-029b: the per-request effect limit (runtime configuration effect_timeout_ms); EFFECT_TIMEOUT_MS when absent. */
  effectTimeoutMs?:number};
/** NX-041 S6b: a stand-in's answer as the guard returns it - a simulated outcome, never a fact. */
export type SimulatedReceipt={intent_id:string;world:string;state:string;stand_in_id:string;stand_in_version:string;simulated:true;output_kind:'simulated_output'};
const STAND_IN_ACTIONS=['nexloop.service.request','nexloop.incentive.issue','nexloop.outreach.send'] as const;
type StandInAction=typeof STAND_IN_ACTIONS[number];
const SIMULATED_NOTE='Simulated: a stand-in recorded this request in a simulation world. Nothing was sent, issued or delivered to anyone, and this is not a fact about any real customer; treat it only as the simulated outcome of this request.';
/** What the model reads for a stand-in receipt: the simulated marker first, no digest or internal field. */
export function presentSimulated(receipt:SimulatedReceipt){
  return {simulated:true as const,output_kind:'simulated_output' as const,request_id:receipt.intent_id,status:receipt.state,stand_in_version:receipt.stand_in_version,note:SIMULATED_NOTE};
}
const uuid=/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const sha=/^[a-f0-9]{64}$/;
const catalogScope={service_code:'local.json-export',deliverable:'固定私有目录内可核验的 JSON 文本导出文件',price_amount:'0',currency:'CNY',guarantees:[],discounts:[],limitations:['不提供目录外保证或折扣','不代表第三方渠道送达、付款或问题解决'],evidence_kind:'fsynced_json_export'} as const;
function validateRejectedScope(value:unknown):void{const row=exact(value,Object.keys(catalogScope));for(const [name,expected] of Object.entries(catalogScope)){if(Array.isArray(expected)){if(!Array.isArray(row[name])||JSON.stringify(row[name])!==JSON.stringify(expected))throw new Error();}else if(row[name]!==expected)throw new Error();}}
function exact(value:unknown,keys:readonly string[]):Record<string,unknown>{
  if(!value||typeof value!=='object'||Array.isArray(value)||Object.getPrototypeOf(value)!==Object.prototype)throw new RuntimeError('runtime_effect_unavailable');
  const row=value as Record<string,unknown>;
  if(Object.keys(row).sort().join(',')!==[...keys].sort().join(','))throw new RuntimeError('runtime_effect_unavailable');
  return row;
}
/** ADR-022 §4: each effect request keeps the 2 s limit by default. ADR-029b: a Host may configure it up to EFFECT_TIMEOUT_MAX_MS. */
export const EFFECT_TIMEOUT_MS=2000;
export const EFFECT_TIMEOUT_MIN_MS=1000;
export const EFFECT_TIMEOUT_MAX_MS=5000;
/** ADR-029b: an absent value is the default; anything else must be an integer within the bounds (a Host refuses to start otherwise). */
export function effectTimeoutMs(value:unknown):number{
  if(value===undefined)return EFFECT_TIMEOUT_MS;
  if(typeof value!=='number'||!Number.isInteger(value)||value<EFFECT_TIMEOUT_MIN_MS||value>EFFECT_TIMEOUT_MAX_MS)throw new RuntimeError('runtime_effect_configuration_invalid');
  return value;
}
/** Instructions every real-model Run starts with (10/16 stage smoke: the model answered in plain text, which is never
 * delivered, and the Run "succeeded" without a reply). The input is the JSON context pack; tools are the only way to act. */
export const RUN_INSTRUCTIONS=[
  'You act for one business toward one customer. The user message is a JSON context pack: user_statement or trigger_statement is what the customer or the event said; formal_facts, current_constraints, supply and the other sections are the facts you may rely on.',
  'Text you write outside a tool call is never shown to anyone. To answer the customer, call nexloop.service.request exactly once with message set to your complete reply, in the customer\'s language, then stop.',
  'Use only the facts in the context pack; never invent prices, offers, promises or policies. If you cannot help, say so politely in that reply.',
  'When the nexloop.plan.outcome tool is offered, this Run reevaluates a plan: record its result with that tool exactly once.',
].join('\n');

/** 10/17 calibration: the model read business_action_success:false as "delivery failed" and offered to try again. The enterprise
 * tools answer in two separate plain fields instead of the raw receipt: whether the enterprise has the order (the effect
 * state) and the enterprise's own business result, which is always still pending here (its staff handle the order and report
 * back later). No internal field names, digests or receipt ids reach the model. */
export const ENTERPRISE_RESULT_GUIDE='The result has request_status (sending: the request is on its way to the enterprise; order_placed: the enterprise has the order; checking: not yet known whether the enterprise received it, it is being checked automatically; not_placed: the enterprise system did not take it), enterprise_result (pending_enterprise: the enterprise staff have not yet reported the outcome; this is normal and not a failure) and next_step. Within this Run, once the result is back, do not request the same order again (that would be a duplicate). Tell the customer in plain words, never these field names or codes.';
const REQUEST_STATUS:Record<string,'sending'|'order_placed'|'checking'|'not_placed'>={accepted:'sending',dispatching:'sending',observed_fulfilled:'order_placed',
  fulfilled:'order_placed',confirmed:'order_placed',unknown:'checking',failed:'not_placed'};
const NEXT_STEP={
  sending:'Tell the customer the request has been passed to the enterprise and they will follow up. Do not request it again.',
  order_placed:'Tell the customer the order has been placed with the enterprise and its staff will handle it and follow up. Do not request it again.',
  checking:'Tell the customer the request was sent and is being confirmed; they will be updated. Do not request it again.',
  not_placed:'Tell the customer the order could not be placed right now and a person will follow up. Do not request it again yourself.',
} as const;
export function presentEnterpriseReceipt(receipt:EffectReceipt){
  const status=REQUEST_STATUS[receipt.state];
  if(status===undefined)throw new RuntimeError('runtime_effect_unavailable');
  return {request_id:receipt.intent_id,request_status:status,enterprise_result:'pending_enterprise' as const,next_step:NEXT_STEP[status]};
}

function effectUnavailableLine(command:RunCommand,operation:string,limit:number,elapsed:number){
  try{
    const run=typeof command?.run_id==='string'&&uuid.test(command.run_id)?command.run_id:null;
    process.stderr.write(JSON.stringify({event:'runtime_effect_unavailable',run_id:run,operation,effect_timeout_ms:limit,elapsed_ms:elapsed})+'\n');
  }catch{/* a diagnosis never changes the outcome */}
}
export class RuntimeEffectClient{
  private readonly origin:URL;
  private readonly configuration:Configuration;
  constructor(configuration:Configuration){
    const origin=new URL(configuration.guardURL);
    if(origin.protocol!=='https:'||origin.hostname!=='127.0.0.1'||!origin.port||Number(origin.port)<1024||Number(origin.port)>65535||origin.username||origin.password||origin.search||origin.hash||origin.pathname!=='/internal/v1/runtime/authorize')throw new RuntimeError('runtime_effect_configuration_invalid');
    if(typeof configuration.activationForRun!=='function'||typeof configuration.privateMaterial!=='function')throw new RuntimeError('runtime_effect_configuration_invalid');
    this.origin=origin;this.configuration={...configuration};this.timeoutMs=effectTimeoutMs(configuration.effectTimeoutMs);
  }
  /** The effect limit this client actually uses (ADR-029b). */
  readonly timeoutMs:number;
  async call(command:RunCommand,operation:'submit'|'find',arguments_:unknown,action?:typeof ENTERPRISE):Promise<EffectReceipt>{
    const began=Date.now();
    try{
      const valid=validateRunCommand(command);
      const ref=this.configuration.activationForRun(valid.run_id);
      if(!ref||!/^activation_[A-Za-z0-9_-]{16,200}$/.test(ref)||Date.parse(valid.not_after)<=Date.now())throw new Error();
      let argumentsBody:Record<string,unknown>={};
      try{
        if(action!==undefined&&action!==ENTERPRISE)throw new RuntimeError('invalid_effect_request');
        if(operation==='submit'&&action===ENTERPRISE){
          // ADR-030: the enterprise delivery parameters (the Action's input_schema): summary, optional details, consumer_ref
          // (this Run's own consumer; filled when absent). No tenant_id; unknown keys refused. SQL re-checks everything.
          const row=arguments_ as Record<string,unknown>;
          if(!row||typeof row!=='object'||Array.isArray(row)||Object.keys(row).some(key=>!['summary','details','consumer_ref'].includes(key)))throw new RuntimeError('invalid_effect_request');
          if(typeof row.summary!=='string'||[...row.summary].length<1||[...row.summary].length>200)throw new RuntimeError('invalid_effect_request');
          if(row.details!==undefined&&(typeof row.details!=='string'||[...row.details].length>2000))throw new RuntimeError('invalid_effect_request');
          if(row.consumer_ref!==undefined&&row.consumer_ref!==valid.consumer_ref)throw new RuntimeError('invalid_effect_request');
          argumentsBody={parameters:{summary:row.summary,...(row.details!==undefined?{details:row.details}:{}),consumer_ref:valid.consumer_ref}};
        }else if(operation==='submit'){
          const hasScope=arguments_!==null&&typeof arguments_==='object'&&Object.hasOwn(arguments_,'request_scope');
          const row=exact(arguments_,hasScope?['message','request_scope']:['message']);
          if(typeof row.message!=='string'||[...row.message].length<1||[...row.message].length>8192)throw new RuntimeError('invalid_effect_request');
          argumentsBody={parameters:{message:row.message},...(hasScope?{request_scope:requestScope(row.request_scope)}:{})};
        }else if(operation==='find'){
          const row=exact(arguments_,['intent_id']);
          if(typeof row.intent_id!=='string'||!uuid.test(row.intent_id))throw new RuntimeError('invalid_effect_request');
          argumentsBody={intent_id:row.intent_id};
        }else throw new RuntimeError('invalid_effect_request');
      }catch{throw new RuntimeError('invalid_effect_request');}  // any refused parameter, before the network
      const key=this.configuration.privateMaterial(this.configuration.keyPath,64).toString('utf8');
      if(!/^[0-9a-f]{64}$/.test(key))throw new Error();
      const ca=this.configuration.privateMaterial(this.configuration.caPath,32768);
      const body=Buffer.from(JSON.stringify({activation_ref:ref,command:valid,...argumentsBody,...(action!==undefined?{action:'nexloop.enterprise.deliver'}:{})}));
      if(body.length>65536)throw new Error();
      const url=new URL('/internal/v1/runtime/effects/'+operation,this.origin);
      // ADR-028: an ambiguous answer (timeout, connection error, 503) is followed by exactly one more request with the same
      // body, within the Run's time limit. The guard keys the intent on the Run's consumer, goal, step and slot (0040): the same
      // payload returns the same intent and reserves nothing again, a different one is a conflict; find is a read. Each request
      // keeps the 2 s limit (ADR-022 §4). Still ambiguous: runtime_effect_unavailable.
      try{return await this.once(url,ca,key,body,valid,operation,arguments_);}
      catch(error){
        if(!(error instanceof RuntimeError)||error.code!=='runtime_effect_ambiguous'||Date.parse(valid.not_after)<=Date.now()+this.timeoutMs)throw error;
        return await this.once(url,ca,key,body,valid,operation,arguments_);
      }
    }catch(error){
      if(error instanceof RuntimeError&&['intent_payload_conflict','outside_catalog_terms','invalid_effect_request','effect_intent_unavailable'].includes(error.code))throw error;
      // ADR-029b diagnosis: one line per unavailable effect request - the limit in use and the time spent (numbers and codes only).
      effectUnavailableLine(command,operation,this.timeoutMs,Date.now()-began);
      throw new RuntimeError('runtime_effect_unavailable');
    }
  }
  private once(url:URL,ca:Buffer,key:string,body:Buffer,valid:RunCommand,operation:'submit'|'find',arguments_:unknown):Promise<EffectReceipt>{
    {
      return new Promise<EffectReceipt>((resolve,reject)=>{
        const fail=()=>reject(new RuntimeError('runtime_effect_unavailable'));
        const ambiguous=()=>reject(new RuntimeError('runtime_effect_ambiguous'));
        const request=httpsRequest(url,{method:'POST',ca,minVersion:'TLSv1.2',agent:false,signal:AbortSignal.timeout(this.timeoutMs),
          headers:{Authorization:'Bearer '+key,'Content-Type':'application/json','Content-Length':body.length}},response=>{
          let size=0;const chunks:Buffer[]=[];
          if(response.statusCode===503){response.resume();ambiguous();return;}
          if(response.headers['content-type']!=='application/json'||response.headers['content-encoding']!==undefined){response.destroy();fail();return;}
          response.on('data',(chunk:Buffer)=>{size+=chunk.length;if(size>32768){response.destroy();request.destroy();fail();}else chunks.push(chunk);});
          response.on('error',ambiguous);
          response.on('end',()=>{try{
            const result=JSON.parse(Buffer.concat(chunks).toString('utf8')) as unknown;
            if(response.statusCode===409){
              // Preserve the public conflict code without retaining error text,
              // headers, debug fields or arbitrary upstream exception content.
              if(result&&typeof result==='object'&&(result as Record<string,unknown>).code==='intent_payload_conflict')reject(new RuntimeError('intent_payload_conflict'));else fail();
              return;
            }
            // 10/18 stage (Run 1ab1d785): the guard's fail-closed answers (403 effect_intent_unavailable, e.g. a find of an intent
            // this Run did not create; 400 invalid_effect_request) were dropped as malformed and the tool threw. They are fixed
            // codes the model can act on: kept as such (still no text, header or debug field).
            if(response.statusCode===403&&result&&typeof result==='object'&&Object.keys(result).length===1&&(result as Record<string,unknown>).code==='effect_intent_unavailable'){reject(new RuntimeError('effect_intent_unavailable'));return;}
            if(response.statusCode===400&&result&&typeof result==='object'&&Object.keys(result).length===1&&(result as Record<string,unknown>).code==='invalid_effect_request'){reject(new RuntimeError('invalid_effect_request'));return;}
            if(response.statusCode===403){const denied=exact(result,['code','scope']);if(denied.code!=='outside_catalog_terms')throw new Error();validateRejectedScope(denied.scope);reject(new RuntimeError('outside_catalog_terms'));return;}
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
        request.setTimeout(this.timeoutMs,()=>request.destroy());request.on('error',ambiguous);request.end(body);
      });
    }
  }
  /** NX-041 S6b: a simulation Run's effect tool through the guard's stand-in route; a real Host never calls this. */
  async standIn(command:RunCommand,request:{action:StandInAction;parameters:Record<string,unknown>}|{intent_id:string}):Promise<SimulatedReceipt>{
    if(this.configuration.simulation!==true)throw new RuntimeError('runtime_effect_unavailable');
    const valid=validateRunCommand(command);
    const find='intent_id' in request;
    try{
      if(find?typeof request.intent_id!=='string'||!uuid.test(request.intent_id):!STAND_IN_ACTIONS.includes(request.action))throw new RuntimeError('invalid_effect_request');
      const ref=this.configuration.activationForRun(valid.run_id);
      if(!ref||!/^activation_[A-Za-z0-9_-]{16,200}$/.test(ref)||Date.parse(valid.not_after)<=Date.now())throw new Error();
      const key=this.configuration.privateMaterial(this.configuration.keyPath,64).toString('utf8');
      if(!/^[0-9a-f]{64}$/.test(key))throw new Error();
      const ca=this.configuration.privateMaterial(this.configuration.caPath,32768);
      const body=Buffer.from(JSON.stringify(find?{activation_ref:ref,command:valid,intent_id:request.intent_id.toLowerCase()}:{activation_ref:ref,command:valid,action:request.action,parameters:request.parameters}));
      if(body.length>65536)throw new Error();
      const url=new URL('/internal/v1/runtime/stand-ins/'+(find?'find':'submit'),this.origin);
      return await new Promise<SimulatedReceipt>((resolve,reject)=>{
        const fail=(code='runtime_effect_unavailable')=>reject(new RuntimeError(code));
        const request_=httpsRequest(url,{method:'POST',ca,minVersion:'TLSv1.2',agent:false,signal:AbortSignal.timeout(this.timeoutMs),
          headers:{Authorization:'Bearer '+key,'Content-Type':'application/json','Content-Length':body.length}},response=>{
          let size=0;const chunks:Buffer[]=[];
          if(response.headers['content-type']!=='application/json'||response.headers['content-encoding']!==undefined){response.destroy();fail();return;}
          response.on('data',(chunk:Buffer)=>{size+=chunk.length;if(size>8192){response.destroy();request_.destroy();fail();}else chunks.push(chunk);});
          response.on('error',()=>fail());
          response.on('end',()=>{try{
            const result=JSON.parse(Buffer.concat(chunks).toString('utf8')) as unknown;
            if(response.statusCode!==200){const denied=exact(result,['code']);
              if(typeof denied.code==='string'&&['invalid_effect_request','effect_intent_unavailable','intent_payload_conflict'].includes(denied.code)){fail(denied.code);return;}
              throw new Error();}
            const envelope=exact(result,['run_id','receipt']);
            const receipt=exact(envelope.receipt,['intent_id','world','state','stand_in_id','stand_in_version','simulated','output_kind']);
            if(envelope.run_id!==valid.run_id||receipt.world!==valid.world_id||receipt.simulated!==true||receipt.output_kind!=='simulated_output'
              ||typeof receipt.intent_id!=='string'||!uuid.test(receipt.intent_id)||typeof receipt.state!=='string'||!['accepted','fulfilled','failed','unknown'].includes(receipt.state)
              ||typeof receipt.stand_in_id!=='string'||typeof receipt.stand_in_version!=='string'||find&&receipt.intent_id!==(request as {intent_id:string}).intent_id.toLowerCase())throw new Error();
            resolve(receipt as SimulatedReceipt);
          }catch{fail();}});
        });
        request_.setTimeout(this.timeoutMs,()=>request_.destroy());request_.on('error',()=>fail());request_.end(body);
      });
    }catch(error){
      if(error instanceof RuntimeError&&['invalid_effect_request','effect_intent_unavailable','intent_payload_conflict'].includes(error.code))throw error;
      throw new RuntimeError('runtime_effect_unavailable');
    }
  }
  /** NX-041 S6b: the simulation Run's tools - same names and parameters as the real ones, each answered by a stand-in. */
  private simulatedTools(bound:RunCommand,options:{planOutcome?:boolean;incentive?:boolean;outreach?:boolean},outcomeTool:readonly ToolRegistration[]):readonly ToolRegistration[]{
    const answer=async(request:Parameters<RuntimeEffectClient['standIn']>[1])=>{
      try{return {content:[{type:'text' as const,text:JSON.stringify(presentSimulated(await this.standIn(bound,request)))}]};}
      catch(error){if(error instanceof RuntimeError&&['invalid_effect_request','effect_intent_unavailable','intent_payload_conflict'].includes(error.code))return {isError:true,content:[{type:'text' as const,text:JSON.stringify({code:error.code,simulated:true})}]};throw new RuntimeError('runtime_effect_unavailable');}
    };
    const parametersOf=(args:unknown)=>{if(!args||typeof args!=='object'||Array.isArray(args))throw new RuntimeError('invalid_effect_request');return args as Record<string,unknown>;};
    return [
      ...outcomeTool,
      ...(options.incentive===true?[defineTool({name:'nexloop.incentive.issue',description:'Simulation world: ask for one incentive from the customer\'s catalog. A stand-in records it; nothing is issued. The answer is a simulated outcome, not a fact.',
        parameters:Type.Object({incentive_offering_id:Type.String({pattern:'^[a-f0-9]{64}$'}),offering_revision:Type.Integer({minimum:1,maximum:Number.MAX_SAFE_INTEGER}),
          binding_id:Type.String({pattern:'^[a-f0-9]{64}$'}),amount_minor:Type.String({pattern:'^[1-9][0-9]{0,15}$'}),currency:Type.String({pattern:'^[A-Z]{3}$'}),
          notification_text:Type.String({maxLength:2000})},{additionalProperties:false}),
        replay:'safe',execute:async args=>answer({action:'nexloop.incentive.issue',parameters:parametersOf(args)})})]:[]),
      ...(options.outreach===true&&options.planOutcome===true?[defineTool({name:'nexloop.outreach.send',description:'Simulation world: ask for one proactive message to this customer (purpose from the outreach section, body the exact text). A stand-in records it; nothing is sent. The answer is a simulated outcome, not a fact. Then record the outcome with nexloop.plan.outcome.',
        parameters:Type.Object({purpose:Type.String({pattern:'^[a-z][a-z0-9_.]{0,63}$'}),body:Type.String({minLength:1,maxLength:2000})},{additionalProperties:false}),
        replay:'safe',execute:async args=>answer({action:'nexloop.outreach.send',parameters:parametersOf(args)})})]:[]),
      defineTool({name:'nexloop.service.request',description:'Simulation world: your reply to the customer (message is the exact text, in the customer\'s language). Call it exactly once per Run, then end. A stand-in records it; it reaches no real customer. The answer is a simulated outcome, not a fact.',
        parameters:Type.Object({message:Type.String({minLength:1,maxLength:8192})},{additionalProperties:false}),replay:'safe',
        execute:async args=>answer({action:'nexloop.service.request',parameters:parametersOf(args)})}),
      defineTool({name:'nexloop.service.find',description:'Simulation world: read the simulated outcome of the reply this Run sent (its request_id). Does not send anything.',
        parameters:Type.Object({intent_id:Type.String({pattern:'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'})},{additionalProperties:false}),replay:'safe',
        execute:async args=>answer({intent_id:String(parametersOf(args).intent_id)})}),
    ];
  }
  /** AT-043: issue a catalog incentive inside this Run; accepted is not issued (the executor admits it at dispatch under the budget). */
  async issueIncentive(command:RunCommand,arguments_:unknown):Promise<IncentiveReceipt>{
    const valid=validateRunCommand(command);
    try{
      const row=exact(arguments_,['incentive_offering_id','offering_revision','binding_id','amount_minor','currency','notification_text']);
      if(typeof row.incentive_offering_id!=='string'||!sha.test(row.incentive_offering_id)||typeof row.binding_id!=='string'||!sha.test(row.binding_id)
        ||typeof row.offering_revision!=='number'||!Number.isSafeInteger(row.offering_revision)||row.offering_revision<1
        ||typeof row.amount_minor!=='string'||!/^[1-9][0-9]{0,15}$/.test(row.amount_minor)||typeof row.currency!=='string'||!/^[A-Z]{3}$/.test(row.currency)
        ||typeof row.notification_text!=='string'||[...row.notification_text].length>2000)throw new RuntimeError('invalid_incentive_request');
      const ref=this.configuration.activationForRun(valid.run_id);
      if(!ref||!/^activation_[A-Za-z0-9_-]{16,200}$/.test(ref)||Date.parse(valid.not_after)<=Date.now())throw new Error();
      const key=this.configuration.privateMaterial(this.configuration.keyPath,64).toString('utf8');
      if(!/^[0-9a-f]{64}$/.test(key))throw new Error();
      const ca=this.configuration.privateMaterial(this.configuration.caPath,32768);
      const body=Buffer.from(JSON.stringify({activation_ref:ref,command:valid,parameters:row}));
      if(body.length>65536)throw new Error();
      const url=new URL('/internal/v1/runtime/incentives/issue',this.origin);
      return await new Promise<IncentiveReceipt>((resolve,reject)=>{
        const fail=(code='incentive_unavailable')=>reject(new RuntimeError(code));
        const request=httpsRequest(url,{method:'POST',ca,minVersion:'TLSv1.2',agent:false,signal:AbortSignal.timeout(2000),
          headers:{Authorization:'Bearer '+key,'Content-Type':'application/json','Content-Length':body.length}},response=>{
          let size=0;const chunks:Buffer[]=[];
          if(response.headers['content-type']!=='application/json'||response.headers['content-encoding']!==undefined){response.destroy();fail();return;}
          response.on('data',(chunk:Buffer)=>{size+=chunk.length;if(size>8192){response.destroy();request.destroy();fail();}else chunks.push(chunk);});
          response.on('error',()=>fail());
          response.on('end',()=>{try{
            const result=JSON.parse(Buffer.concat(chunks).toString('utf8')) as unknown;
            if(response.statusCode!==200){const denied=exact(result,['code']);
              if(typeof denied.code==='string'&&['outside_catalog_terms','incentive_payload_conflict','invalid_incentive_request'].includes(denied.code)){fail(denied.code);return;}
              throw new Error();}
            const out=exact(result,['run_id','intent_id','state','replayed','origin','business_action_success']);
            if(out.run_id!==valid.run_id||typeof out.intent_id!=='string'||!/^[A-Za-z0-9._:-]{1,190}$/.test(out.intent_id)||typeof out.state!=='string'
              ||!['accepted','dispatching','unknown','fulfilled','failed','refused'].includes(out.state)||typeof out.replayed!=='boolean'
              ||typeof out.origin!=='string'||!['reply','plan','agent'].includes(out.origin)||out.business_action_success!==false)throw new Error();
            resolve(out as IncentiveReceipt);
          }catch{fail();}});
        });
        request.setTimeout(2000,()=>request.destroy());request.on('error',()=>fail());request.end(body);
      });
    }catch(error){
      if(error instanceof RuntimeError&&['outside_catalog_terms','incentive_payload_conflict','invalid_incentive_request'].includes(error.code))throw error;
      throw new RuntimeError('incentive_unavailable');
    }
  }
  /** Outreach slice 5: a plan reevaluation Run asks for one proactive message; accepted is not sent (every gate is SQL's at dispatch). */
  async sendOutreach(command:RunCommand,arguments_:unknown):Promise<OutreachReceipt>{
    const valid=validateRunCommand(command);
    try{
      // Anything but exactly purpose and body is the model's to correct (it never names the consumer, origin, channel or topic).
      let row:Record<string,unknown>;try{row=exact(arguments_,['purpose','body']);}catch{throw new RuntimeError('invalid_outreach_request');}
      if(typeof row.purpose!=='string'||!/^[a-z][a-z0-9_.]{0,63}$/.test(row.purpose)||typeof row.body!=='string'||[...row.body].length<1||[...row.body].length>2000)
        throw new RuntimeError('invalid_outreach_request');
      const ref=this.configuration.activationForRun(valid.run_id);
      if(!ref||!/^activation_[A-Za-z0-9_-]{16,200}$/.test(ref)||Date.parse(valid.not_after)<=Date.now())throw new Error();
      const key=this.configuration.privateMaterial(this.configuration.keyPath,64).toString('utf8');
      if(!/^[0-9a-f]{64}$/.test(key))throw new Error();
      const ca=this.configuration.privateMaterial(this.configuration.caPath,32768);
      const body=Buffer.from(JSON.stringify({activation_ref:ref,command:valid,parameters:row}));
      if(body.length>65536)throw new Error();
      const url=new URL('/internal/v1/runtime/outreach/send',this.origin);
      return await new Promise<OutreachReceipt>((resolve,reject)=>{
        const fail=(code='outreach_unavailable')=>reject(new RuntimeError(code));
        const request=httpsRequest(url,{method:'POST',ca,minVersion:'TLSv1.2',agent:false,signal:AbortSignal.timeout(2000),
          headers:{Authorization:'Bearer '+key,'Content-Type':'application/json','Content-Length':body.length}},response=>{
          let size=0;const chunks:Buffer[]=[];
          if(response.headers['content-type']!=='application/json'||response.headers['content-encoding']!==undefined){response.destroy();fail();return;}
          response.on('data',(chunk:Buffer)=>{size+=chunk.length;if(size>8192){response.destroy();request.destroy();fail();}else chunks.push(chunk);});
          response.on('error',()=>fail());
          response.on('end',()=>{try{
            const result=JSON.parse(Buffer.concat(chunks).toString('utf8')) as unknown;
            if(response.statusCode!==200){const denied=exact(result,['code']);
              if(typeof denied.code==='string'&&OUTREACH_CODES.includes(denied.code)){fail(denied.code);return;}
              throw new Error();}
            const out=exact(result,['run_id','intent_id','state','replayed','business_action_success']);
            if(out.run_id!==valid.run_id||typeof out.intent_id!=='string'||!/^outreach:[0-9a-f]{64}$/.test(out.intent_id)||typeof out.state!=='string'
              ||!['accepted','dispatching','unknown','fulfilled','failed','refused'].includes(out.state)||typeof out.replayed!=='boolean'||out.business_action_success!==false)throw new Error();
            resolve(out as OutreachReceipt);
          }catch{fail();}});
        });
        request.setTimeout(2000,()=>request.destroy());request.on('error',()=>fail());request.end(body);
      });
    }catch(error){
      if(error instanceof RuntimeError&&OUTREACH_CODES.includes(error.code))throw error;
      throw new RuntimeError('outreach_unavailable');
    }
  }
  /** NX-024: the reevaluation Run's outcome; the guard re-authorizes the live activation and SQL binds it to the plan. */
  async recordOutcome(command:RunCommand,outcome:unknown):Promise<RecordedOutcome>{
    const valid=validateRunCommand(command);const body_=runOutcome(outcome);
    try{
      const ref=this.configuration.activationForRun(valid.run_id);
      if(!ref||!/^activation_[A-Za-z0-9_-]{16,200}$/.test(ref)||Date.parse(valid.not_after)<=Date.now())throw new Error();
      const key=this.configuration.privateMaterial(this.configuration.keyPath,64).toString('utf8');
      if(!/^[0-9a-f]{64}$/.test(key))throw new Error();
      const ca=this.configuration.privateMaterial(this.configuration.caPath,32768);
      const body=Buffer.from(JSON.stringify({activation_ref:ref,command:valid,outcome:body_}));
      if(body.length>65536)throw new Error();
      const url=new URL('/internal/v1/runtime/outcomes/record',this.origin);
      return await new Promise<RecordedOutcome>((resolve,reject)=>{
        const fail=(code='run_outcome_unavailable')=>reject(new RuntimeError(code));
        const request=httpsRequest(url,{method:'POST',ca,minVersion:'TLSv1.2',agent:false,signal:AbortSignal.timeout(2000),
          headers:{Authorization:'Bearer '+key,'Content-Type':'application/json','Content-Length':body.length}},response=>{
          let size=0;const chunks:Buffer[]=[];
          if(response.headers['content-type']!=='application/json'||response.headers['content-encoding']!==undefined){response.destroy();fail();return;}
          response.on('data',(chunk:Buffer)=>{size+=chunk.length;if(size>8192){response.destroy();request.destroy();fail();}else chunks.push(chunk);});
          response.on('error',()=>fail());
          response.on('end',()=>{try{
            const result=JSON.parse(Buffer.concat(chunks).toString('utf8')) as unknown;
            if(response.statusCode===409){const row=exact(result,['code']);if(row.code!=='plan_version_not_current')throw new Error();fail('plan_version_not_current');return;}
            if(response.statusCode===400){fail('invalid_run_outcome');return;}
            if(response.statusCode!==200)throw new Error();
            const row=exact(result,['run_id','recorded','replay','kind','plan_version','new_version']);
            if(row.run_id!==valid.run_id||row.recorded!==true||typeof row.replay!=='boolean'||row.kind!==body_.kind||typeof row.plan_version!=='number'||!Number.isSafeInteger(row.plan_version)
              ||!(row.new_version===null||(typeof row.new_version==='number'&&Number.isSafeInteger(row.new_version))))throw new Error();
            resolve(row as RecordedOutcome);
          }catch{fail();}});
        });
        request.setTimeout(2000,()=>request.destroy());request.on('error',()=>fail());request.end(body);
      });
    }catch(error){
      if(error instanceof RuntimeError&&['plan_version_not_current','invalid_run_outcome'].includes(error.code))throw error;
      throw new RuntimeError('run_outcome_unavailable');
    }
  }
  toolsForRun(command:RunCommand,options:{planOutcome?:boolean;incentive?:boolean;outreach?:boolean;enterprise?:EffectSupply;replyEndsRun?:boolean}={}):readonly ToolRegistration[]{
    const bound=validateRunCommand(command);
    const invokeAction=async(operation:'submit'|'find',args:unknown,action:typeof ENTERPRISE)=>{
      try{return {content:[{type:'text' as const,text:JSON.stringify(presentEnterpriseReceipt(await this.call(bound,operation,args,action)))}]};}
      catch(error){if(error instanceof RuntimeError&&['outside_catalog_terms','intent_payload_conflict','invalid_effect_request','effect_intent_unavailable'].includes(error.code))return {isError:true,content:[{type:'text' as const,text:JSON.stringify({code:error.code})}]};throw new RuntimeError('runtime_effect_unavailable');}
    };
    // 10/17 stage smoke: the service intent is keyed on this Run's slot (0040), so one Run has one reply. A second, different
    // service.request (the model adding to a reply already submitted) is a 409 conflict; it is answered as a normal result that
    // says the reply was already sent and names its intent, not as a tool error that would fail a Run whose reply went out.
    let submitted:string|undefined;
    // 10/18 stage (Run 1fb1e35a): after a message Run's reply was accepted, a closing model turn failed and the whole Run was
    // recorded failed. In a message Run (replyEndsRun) the reply is the last step: once the reply tool has its receipt the Run
    // ends at that tool round (pi-durable terminates only when every call of the round asks to), with no closing model call.
    const end=options.replyEndsRun===true?{control:{terminate:true as const}}:{};
    // 10/18 release 13 (Run eede1a41): the model put the enterprise offering in the reply's request_scope; the guard refused it
    // (effect_intent_unavailable) twice before a reply without scope went out. request_scope is the reply's own catalog terms
    // only: naming the enterprise offering is refused here, before any request, with what to do instead.
    const enterpriseOffering=options.enterprise?.offering_id;
    const invoke=async(operation:'submit'|'find',args:unknown)=>{
      const scope=operation==='submit'&&args&&typeof args==='object'?(args as {request_scope?:{offering_id?:unknown}}).request_scope:undefined;
      if(enterpriseOffering!==undefined&&scope&&typeof scope==='object'&&scope.offering_id===enterpriseOffering)
        return {isError:true,content:[{type:'text' as const,text:JSON.stringify({code:'request_scope_wrong_service',next:'request_scope is only for this reply\'s own catalog terms. Send the reply without request_scope; the enterprise order is placed with nexloop.enterprise.deliver.'})}]};
      try{
        const receipt=await this.call(bound,operation,args);
        if(operation==='submit')submitted=receipt.intent_id;
        return {content:[{type:'text' as const,text:JSON.stringify(receipt)}],...(operation==='submit'?end:{})};
      }
      catch(error){if(error instanceof RuntimeError&&error.code==='outside_catalog_terms')return {isError:true,content:[{type:'text' as const,text:JSON.stringify({code:'outside_catalog_terms',scope:catalogScope})}]};
        // 10/18: parameters this Host refuses before any request (unknown keys, lengths, another consumer) come back as a code the
        // model can correct, instead of an exception that ends the call with no reason.
        if(error instanceof RuntimeError&&['invalid_effect_request','effect_intent_unavailable'].includes(error.code))return {isError:true,content:[{type:'text' as const,text:JSON.stringify({code:error.code})}]};
        if(error instanceof RuntimeError&&error.code==='intent_payload_conflict'&&operation==='submit')return {content:[{type:'text' as const,text:JSON.stringify({code:'reply_already_submitted',...(submitted!==undefined?{intent_id:submitted}:{}),next:'This Run already sent its one reply; nothing more was sent. End the Run now.'})}],...end};
        if(error instanceof RuntimeError&&error.code==='intent_payload_conflict')return {isError:true,content:[{type:'text' as const,text:'{"code":"intent_payload_conflict","http_status":409}'}]};throw new RuntimeError('runtime_effect_unavailable');}
    };
    const outcomeTool=options.planOutcome===true?[defineTool({name:'nexloop.plan.outcome',
      description:'Record the result of this plan reevaluation exactly once. no_action, needs_information, waiting_external and escalate are normal results. plan_update replaces the plan with new steps; action_intent names an intent this Run already submitted. Read current facts first; never re-run an old step mechanically.',
      parameters:Type.Object({schema_version:Type.Literal('1.0'),kind:Type.Union(OUTCOME_KINDS.map(kind=>Type.Literal(kind))),reasons:Type.Array(Type.String({minLength:1,maxLength:500}),{maxItems:8}),
        reassess_at:Type.Union([Type.Null(),Type.String({pattern:instant.source})]),evidence_refs:Type.Array(Type.String({minLength:3,maxLength:512}),{maxItems:32}),
        intent_ref:Type.Union([Type.Null(),Type.String({pattern:uuid.source})]),plan_update:Type.Union([Type.Null(),Type.Object({strategy:Type.Union([Type.Null(),Type.String({minLength:1,maxLength:8192})]),
          steps:Type.Array(Type.Record(Type.String(),Type.Unknown()),{minItems:1,maxItems:32})},{additionalProperties:false})])},{additionalProperties:false}),
      replay:'safe',execute:async args=>{
        try{return {content:[{type:'text' as const,text:JSON.stringify(await this.recordOutcome(bound,args))}]};}
        catch(error){if(error instanceof RuntimeError&&['plan_version_not_current','invalid_run_outcome'].includes(error.code))return {isError:true,content:[{type:'text' as const,text:JSON.stringify({code:error.code})}]};throw new RuntimeError('run_outcome_unavailable');}
      }})]:[];
    // AT-043: offered only when the Run is meant to issue incentives (its credential names nexloop.incentive.issue:1; SQL re-checks it).
    const incentiveTool=options.incentive===true?[defineTool({name:'nexloop.incentive.issue',
      description:'Issue one incentive from the customer\'s own catalog (an IncentiveOffering bound to this customer). accepted is not issued: the executor admits it at dispatch under the budget and may refuse it. Never promise an incentive that is not in the catalog. One issue per offering per Run; retries return the original receipt.',
      parameters:Type.Object({incentive_offering_id:Type.String({pattern:'^[a-f0-9]{64}$'}),offering_revision:Type.Integer({minimum:1,maximum:Number.MAX_SAFE_INTEGER}),
        binding_id:Type.String({pattern:'^[a-f0-9]{64}$'}),amount_minor:Type.String({pattern:'^[1-9][0-9]{0,15}$'}),currency:Type.String({pattern:'^[A-Z]{3}$'}),
        notification_text:Type.String({maxLength:2000})},{additionalProperties:false}),
      replay:'safe',execute:async args=>{
        try{return {content:[{type:'text' as const,text:JSON.stringify(await this.issueIncentive(bound,args))}]};}
        catch(error){if(error instanceof RuntimeError&&['outside_catalog_terms','incentive_payload_conflict','invalid_incentive_request'].includes(error.code))return {isError:true,content:[{type:'text' as const,text:JSON.stringify({code:error.code})}]};throw new RuntimeError('incentive_unavailable');}
      }})]:[];
    // Outreach slice 5: offered only to a plan reevaluation Run (with its outcome tool) when the deployment turns it on; SQL still
    // requires a plan Run, the Run's credential and Role ceiling naming nexloop.outreach.send:1, and a listed purpose.
    const outreachTool=options.outreach===true&&options.planOutcome===true?[defineTool({name:'nexloop.outreach.send',
      description:'Ask for one proactive message to this customer, only when the plan and its evidence call for it; deciding not to contact (no_action) is a normal result. purpose must be one of the purposes in the context\'s outreach section; body is the exact text, within that purpose\'s max_chars, in the customer\'s language, written in the operator persona\'s voice, only about that purpose, promising no new service, price or benefit. Never claim to be human; follow the persona\'s AI disclosure settings. Do not add an unsubscribe line: the server appends it to marketing messages. accepted is not sent: every check (contact restriction, quiet hours, frequency, preferences, switches) runs again at dispatch and may refuse it. At most one per purpose per day; retries return the original receipt. Then record the outcome with nexloop.plan.outcome.',
      parameters:Type.Object({purpose:Type.String({pattern:'^[a-z][a-z0-9_.]{0,63}$'}),body:Type.String({minLength:1,maxLength:2000})},{additionalProperties:false}),
      replay:'safe',execute:async args=>{
        try{return {content:[{type:'text' as const,text:JSON.stringify(await this.sendOutreach(bound,args))}]};}
        catch(error){if(error instanceof RuntimeError&&OUTREACH_CODES.includes(error.code))return {isError:true,content:[{type:'text' as const,text:JSON.stringify({code:error.code})}]};throw new RuntimeError('outreach_unavailable');}
      }})]:[];
    // ADR-030: offered only when this Run has the enterprise slot (its context names the enterprise offering, which it has
    // only when the Run credential grants nexloop.enterprise.deliver:1). Availability, not authority: the guard rechecks.
    const enterprise=options.enterprise;
    const enterpriseTools=enterprise?.action===ENTERPRISE?[defineTool({name:'nexloop.enterprise.deliver',
      description:`Ask the enterprise to deliver its catalog service "${enterprise.title}" (${enterprise.service_code}, ${enterprise.price_amount} CNY) to this customer through its own system. Only when the customer asked for this service; never for another service, price or customer. When the customer's current message clearly asks to order it, that is a new order: place it now with this tool, even if the conversation history shows an earlier order (an earlier order is never a reason to skip this one). Write summary and details in the customer's own language (the language of the customer's messages): the enterprise staff read them as the order. ${ENTERPRISE_RESULT_GUIDE}`,
      parameters:Type.Object({summary:Type.String({minLength:1,maxLength:200}),details:Type.Optional(Type.String({maxLength:2000})),
        consumer_ref:Type.Optional(Type.String({minLength:1,maxLength:80}))},{additionalProperties:false}),
      replay:'safe',execute:async args=>invokeAction('submit',args,ENTERPRISE)}),
      defineTool({name:'nexloop.enterprise.find',description:`Read the current state of an enterprise delivery this Run requested: only a request_id that nexloop.enterprise.deliver returned in this Run (earlier orders cannot be read here; effect_intent_unavailable means it is not this Run's). Not being able to read an earlier order never stops a new one: when the customer asks to order, use nexloop.enterprise.deliver. Does not send anything and never places it again. ${ENTERPRISE_RESULT_GUIDE}`,
        parameters:Type.Object({request_id:Type.String({pattern:'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'})},{additionalProperties:false}),
        replay:'safe',execute:async args=>invokeAction('find',args!==null&&typeof args==='object'&&!Array.isArray(args)&&Object.keys(args).length===1&&Object.hasOwn(args,'request_id')
          ?{intent_id:(args as {request_id:unknown}).request_id}:args,ENTERPRISE)})]:[];
    // NX-041 S6b: in a simulation Host every effect tool is its stand-in (the enterprise delivery has none and is not offered).
    if(this.configuration.simulation===true)return this.simulatedTools(bound,options,outcomeTool);
    return [
      ...outcomeTool,
      ...incentiveTool,
      ...outreachTool,
      ...enterpriseTools,
      defineTool({name:'nexloop.service.request',description:'Send your reply to the customer: message is the exact text the customer will read, in the customer\'s language. This is the only way anything reaches the customer; text you write outside a tool call is never delivered. Call it exactly once per Run; once it returns, the reply is sent and the Run is done: end without another call. It persists a governed service intent in the current plan slot; accepted is not fulfillment. Retries return the original receipt; never choose a new business key. request_scope is optional and only for this reply\'s own catalog service when the customer asked for a guarantee or discount: omit it otherwise, and never put the enterprise offering or a request_id in it.',
        parameters:Type.Object({message:Type.String({minLength:1,maxLength:8192}),request_scope:Type.Optional(Type.Object({offering_id:Type.String({pattern:'^[a-f0-9]{64}$'}),offering_revision:Type.Integer({minimum:1,maximum:Number.MAX_SAFE_INTEGER}),requested_guarantees:Type.Array(Type.String({minLength:1,maxLength:128}),{maxItems:16,uniqueItems:true}),requested_discounts:Type.Array(Type.String({minLength:1,maxLength:128}),{maxItems:16,uniqueItems:true})},{additionalProperties:false}))},{additionalProperties:false}),replay:'safe',execute:async args=>invoke('submit',args)}),
      defineTool({name:'nexloop.service.find',description:'Read the current authorized receipt of a reply this Run sent (an intent_id nexloop.service.request returned in this Run; any other gives effect_intent_unavailable). Does not send or create a new effect.',
        parameters:Type.Object({intent_id:Type.String({pattern:'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'})},{additionalProperties:false}),replay:'safe',execute:async args=>invoke('find',args)}),
    ];
  }
}
