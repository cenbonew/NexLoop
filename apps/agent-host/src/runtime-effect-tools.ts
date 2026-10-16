/** Run-bound intent tools. Backend owns every EIOS/PG/provider credential. */
import {request as httpsRequest} from 'node:https';
import {Type} from '@earendil-works/pi-ai';
import {defineTool,type ToolRegistration} from '@earendil-works/pi-durable';
import {RuntimeError,validateRunCommand,type RunCommand} from './runtime-adapter.js';
import {ENTERPRISE_ACTION,type EffectSupply} from './context-input.js';
const ENTERPRISE=ENTERPRISE_ACTION;

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
type Configuration={guardURL:URL;caPath:string;keyPath:string;privateMaterial:Material;activationForRun:(runId:string)=>string|undefined};
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
/** ADR-022 §4: each effect request keeps the 2 s limit. */
export const EFFECT_TIMEOUT_MS=2000;
/** Instructions every real-model Run starts with (10/16 stage smoke: the model answered in plain text, which is never
 * delivered, and the Run "succeeded" without a reply). The input is the JSON context pack; tools are the only way to act. */
export const RUN_INSTRUCTIONS=[
  'You act for one business toward one customer. The user message is a JSON context pack: user_statement or trigger_statement is what the customer or the event said; formal_facts, current_constraints, supply and the other sections are the facts you may rely on.',
  'Text you write outside a tool call is never shown to anyone. To answer the customer, call nexloop.service.request exactly once with message set to your complete reply, in the customer\'s language, then stop.',
  'Use only the facts in the context pack; never invent prices, offers, promises or policies. If you cannot help, say so politely in that reply.',
  'When the nexloop.plan.outcome tool is offered, this Run reevaluates a plan: record its result with that tool exactly once.',
].join('\n');

export class RuntimeEffectClient{
  private readonly origin:URL;
  private readonly configuration:Configuration;
  constructor(configuration:Configuration){
    const origin=new URL(configuration.guardURL);
    if(origin.protocol!=='https:'||origin.hostname!=='127.0.0.1'||!origin.port||Number(origin.port)<1024||Number(origin.port)>65535||origin.username||origin.password||origin.search||origin.hash||origin.pathname!=='/internal/v1/runtime/authorize')throw new RuntimeError('runtime_effect_configuration_invalid');
    if(typeof configuration.activationForRun!=='function'||typeof configuration.privateMaterial!=='function')throw new RuntimeError('runtime_effect_configuration_invalid');
    this.origin=origin;this.configuration={...configuration};
  }
  async call(command:RunCommand,operation:'submit'|'find',arguments_:unknown,action?:typeof ENTERPRISE):Promise<EffectReceipt>{
    try{
      const valid=validateRunCommand(command);
      const ref=this.configuration.activationForRun(valid.run_id);
      if(!ref||!/^activation_[A-Za-z0-9_-]{16,200}$/.test(ref)||Date.parse(valid.not_after)<=Date.now())throw new Error();
      let argumentsBody:Record<string,unknown>;
      if(action!==undefined&&action!==ENTERPRISE)throw new Error();
      if(operation==='submit'&&action===ENTERPRISE){
        // ADR-030: the enterprise delivery parameters (the Action's input_schema): summary, optional details, consumer_ref
        // (this Run's own consumer; filled when absent). No tenant_id; unknown keys refused. SQL re-checks everything.
        const row=arguments_ as Record<string,unknown>;
        if(!row||typeof row!=='object'||Array.isArray(row)||Object.keys(row).some(key=>!['summary','details','consumer_ref'].includes(key)))throw new Error();
        if(typeof row.summary!=='string'||[...row.summary].length<1||[...row.summary].length>200)throw new Error();
        if(row.details!==undefined&&(typeof row.details!=='string'||[...row.details].length>2000))throw new Error();
        if(row.consumer_ref!==undefined&&row.consumer_ref!==valid.consumer_ref)throw new Error();
        argumentsBody={parameters:{summary:row.summary,...(row.details!==undefined?{details:row.details}:{}),consumer_ref:valid.consumer_ref}};
      }else if(operation==='submit'){
        const hasScope=arguments_!==null&&typeof arguments_==='object'&&Object.hasOwn(arguments_,'request_scope');
        const row=exact(arguments_,hasScope?['message','request_scope']:['message']);
        if(typeof row.message!=='string'||[...row.message].length<1||[...row.message].length>8192)throw new Error();
        argumentsBody={parameters:{message:row.message},...(hasScope?{request_scope:requestScope(row.request_scope)}:{})};
      }else if(operation==='find'){
        const row=exact(arguments_,['intent_id']);
        if(typeof row.intent_id!=='string'||!uuid.test(row.intent_id))throw new Error();
        argumentsBody={intent_id:row.intent_id};
      }else throw new Error();
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
        if(!(error instanceof RuntimeError)||error.code!=='runtime_effect_ambiguous'||Date.parse(valid.not_after)<=Date.now()+EFFECT_TIMEOUT_MS)throw error;
        return await this.once(url,ca,key,body,valid,operation,arguments_);
      }
    }catch(error){
      if(error instanceof RuntimeError&&['intent_payload_conflict','outside_catalog_terms'].includes(error.code))throw error;
      throw new RuntimeError('runtime_effect_unavailable');
    }
  }
  private once(url:URL,ca:Buffer,key:string,body:Buffer,valid:RunCommand,operation:'submit'|'find',arguments_:unknown):Promise<EffectReceipt>{
    {
      return new Promise<EffectReceipt>((resolve,reject)=>{
        const fail=()=>reject(new RuntimeError('runtime_effect_unavailable'));
        const ambiguous=()=>reject(new RuntimeError('runtime_effect_ambiguous'));
        const request=httpsRequest(url,{method:'POST',ca,minVersion:'TLSv1.2',agent:false,signal:AbortSignal.timeout(EFFECT_TIMEOUT_MS),
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
        request.setTimeout(EFFECT_TIMEOUT_MS,()=>request.destroy());request.on('error',ambiguous);request.end(body);
      });
    }
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
  toolsForRun(command:RunCommand,options:{planOutcome?:boolean;incentive?:boolean;enterprise?:EffectSupply}={}):readonly ToolRegistration[]{
    const bound=validateRunCommand(command);
    const invokeAction=async(operation:'submit'|'find',args:unknown,action:typeof ENTERPRISE)=>{
      try{return {content:[{type:'text' as const,text:JSON.stringify(await this.call(bound,operation,args,action))}]};}
      catch(error){if(error instanceof RuntimeError&&['outside_catalog_terms','intent_payload_conflict'].includes(error.code))return {isError:true,content:[{type:'text' as const,text:JSON.stringify({code:error.code})}]};throw new RuntimeError('runtime_effect_unavailable');}
    };
    const invoke=async(operation:'submit'|'find',args:unknown)=>{
      try{return {content:[{type:'text' as const,text:JSON.stringify(await this.call(bound,operation,args))}]};}
      catch(error){if(error instanceof RuntimeError&&error.code==='outside_catalog_terms')return {isError:true,content:[{type:'text' as const,text:JSON.stringify({code:'outside_catalog_terms',scope:catalogScope})}]};if(error instanceof RuntimeError&&error.code==='intent_payload_conflict')return {isError:true,content:[{type:'text' as const,text:'{"code":"intent_payload_conflict","http_status":409}'}]};throw new RuntimeError('runtime_effect_unavailable');}
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
    // ADR-030: offered only when this Run has the enterprise slot (its context names the enterprise offering, which it has
    // only when the Run credential grants nexloop.enterprise.deliver:1). Availability, not authority: the guard rechecks.
    const enterprise=options.enterprise;
    const enterpriseTools=enterprise?.action===ENTERPRISE?[defineTool({name:'nexloop.enterprise.deliver',
      description:`Ask the enterprise to deliver its catalog service "${enterprise.title}" (${enterprise.service_code}, ${enterprise.price_amount} CNY) to this customer through its own system. accepted is not delivery: the result comes back as a receipt. Only when the customer asked for this service; never for another service, price or customer.`,
      parameters:Type.Object({summary:Type.String({minLength:1,maxLength:200}),details:Type.Optional(Type.String({maxLength:2000})),
        consumer_ref:Type.Optional(Type.String({minLength:1,maxLength:80}))},{additionalProperties:false}),
      replay:'safe',execute:async args=>invokeAction('submit',args,ENTERPRISE)}),
      defineTool({name:'nexloop.enterprise.find',description:'Read the current receipt of an enterprise delivery this Run requested. Does not send anything.',
        parameters:Type.Object({intent_id:Type.String({pattern:'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'})},{additionalProperties:false}),
        replay:'safe',execute:async args=>invokeAction('find',args,ENTERPRISE)})]:[];
    return [
      ...outcomeTool,
      ...incentiveTool,
      ...enterpriseTools,
      defineTool({name:'nexloop.service.request',description:'Send your reply to the customer: message is the exact text the customer will read, in the customer\'s language. This is the only way anything reaches the customer; text you write outside a tool call is never delivered. Call it once per reply. It persists a governed service intent in the current plan slot; accepted is not fulfillment. Retries return the original receipt; never choose a new business key.',
        parameters:Type.Object({message:Type.String({minLength:1,maxLength:8192}),request_scope:Type.Optional(Type.Object({offering_id:Type.String({pattern:'^[a-f0-9]{64}$'}),offering_revision:Type.Integer({minimum:1,maximum:Number.MAX_SAFE_INTEGER}),requested_guarantees:Type.Array(Type.String({minLength:1,maxLength:128}),{maxItems:16,uniqueItems:true}),requested_discounts:Type.Array(Type.String({minLength:1,maxLength:128}),{maxItems:16,uniqueItems:true})},{additionalProperties:false}))},{additionalProperties:false}),replay:'safe',execute:async args=>invoke('submit',args)}),
      defineTool({name:'nexloop.service.find',description:'Read the current authorized receipt for an existing service intent. Does not send or create a new effect.',
        parameters:Type.Object({intent_id:Type.String({pattern:'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'})},{additionalProperties:false}),replay:'safe',execute:async args=>invoke('find',args)}),
    ];
  }
}
