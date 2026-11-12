import {createHash,randomUUID} from 'node:crypto';
import {canonicalContextJSON,contextCommandDigest,CONTEXT_PROTOCOL,CONTEXT_PROTOCOL_V2,CONTEXT_PROTOCOL_V3} from '../src/context-input.js';
import {command} from './pi-runtime-support.ts';
const sha=(value:string)=>createHash('sha256').update(value).digest('hex');
export function declaration(v2=true){
 const cmd=command();cmd.runtime_profile='deterministic-test';cmd.consumer_ref='consumer:'+'a'.repeat(64);cmd.goal_version_ref='goal:'+'b'.repeat(64)+':revision:1:step:1:control:1';cmd.credential_ref='run:'+cmd.run_id;
 const source='synthetic-source',artifact=sha(canonicalContextJSON([cmd.tenant_id,'real',source,'context:'+cmd.run_id])).slice(0,32);cmd.context_manifest_ref='artifact:'+artifact;
 const supply={offering_id:'1'.repeat(64),offering_revision:1,binding_id:'2'.repeat(64),binding_revision:1,provenance:'eios:object:'+'1'.repeat(64),properties:{service_code:'local.json-export',title:'当前正式免费 JSON 供给',delivery_action:'nexloop.service.request:1',content_kind:'json-message-export',price_amount:'0',currency:'CNY',eligibility:'current_consumer_plan',allowed_guarantees:[],allowed_discounts:[],evidence_kind:'fsynced_json_export',active:true,valid_until:cmd.not_after}};
 const pack={schema_version:v2?CONTEXT_PROTOCOL_V2:CONTEXT_PROTOCOL,bindings:{tenant_id:cmd.tenant_id,world_id:'real',run_id:cmd.run_id,source_principal:source,context_id:randomUUID(),namespace:sha(cmd.tenant_id+'\0real'),artifact_id:artifact,command_digest:contextCommandDigest(cmd)},user_statement:{message_id:'e'.repeat(64),conversation_id:'f'.repeat(64),sequence:1,body:'用户原文，不是权利授予。',provenance:'eios:object:'+'e'.repeat(64)},formal_facts:[['Consumer','a'],['Goal','b'],['PlanStep','c'],['EffectControl','d']].map(([type,id])=>({type,id:id!.repeat(64),revision:1,provenance:'eios:object:'+id!.repeat(64)})),current_constraints:{action:'nexloop.service.request:1',allow_effect:true,budget_units:1,reserved_units:0,executor_principal:'synthetic-executor',valid_until:cmd.not_after},...(v2?{supply}:{})};
 const check=(change?:(value:any)=>void,raw?:string)=>{const copy=structuredClone(pack);change?.(copy);const input=raw??canonicalContextJSON(copy);return {input,proof:{artifact_ref:cmd.context_manifest_ref,sha256:sha(input),command_binding_digest:contextCommandDigest(cmd)}};};
 return {cmd,pack,check};
}

export function roleDeclaration(){
 const f=declaration();const roleId='3'.repeat(64),linkId='4'.repeat(64);
 f.cmd.role_ref='role:'+roleId+':mapping:'+linkId;
 const pack:any=f.pack;pack.trigger_statement={kind:'service_trigger',event_id:f.cmd.trigger_event_id,source_principal:pack.bindings.source_principal,body:pack.user_statement.body,provenance:'eios:role-trigger:'+f.cmd.trigger_event_id};delete pack.user_statement;pack.schema_version=CONTEXT_PROTOCOL_V3;pack.bindings.command_digest=contextCommandDigest(f.cmd);
 pack.role_binding={binding:{run_id:f.cmd.run_id,tenant_id:f.cmd.tenant_id,world:'real',consumer_id:'a'.repeat(64),link_id:linkId,role_id:roleId,step_id:'c'.repeat(64),link_revision:1,role_revision:1,step_revision:1,role_ref:f.cmd.role_ref,scope:'local-service',expires_at:new Date(Date.now()+30000).toISOString()},definition:{name:'delivery-role',responsibility:'真实定义的职责',ceiling_ref:'metadata-only',active:true,valid_from:new Date(Date.now()-30000).toISOString(),valid_until:new Date(Date.now()+60000).toISOString()},definition_provenance:'eios:object:'+roleId,mapping_provenance:'eios:object:'+linkId,grants_authority:false};
 return {cmd:f.cmd,pack,check:(change?:(value:any)=>void)=>{const value=structuredClone(pack);change?.(value);const input=canonicalContextJSON(value);return {input,proof:{artifact_ref:f.cmd.context_manifest_ref,sha256:sha(input),command_binding_digest:contextCommandDigest(f.cmd)}};}};
}

export function envelope(role=false,version=8){
 const f=role?roleDeclaration():declaration();const p:any=f.pack;
 const statement=p.user_statement;
 Object.assign(p,{schema_version:'nexloop.context-pack.v'+version,strategy_ref:'context-strategy:recent_plus_required@1',
  role:role?{role_binding:p.role_binding,role_policy:null}:null,current_event:role?p.trigger_statement:{kind:'consumer_message',message_id:statement.message_id,provenance:statement.provenance},
  user_statement:role?null:statement,goal:{goal_version_refs:['goal:'+'b'.repeat(64)+'@1'],control_snapshot:{control_revision:0,scopes:[],goals:[],objects:[],budgets:[]}},
  constraints:[],consumer_state:[],open_work:[],evidence:[],semantics:[],experience:[],budget_report:{estimator:'synthetic',input_token_budget:2048,output_reserve:128,framing_reserve:0,available:1920,used:0,sections:{},omitted:[]},insufficient:[]});
 delete p.role_binding;delete p.trigger_statement;
 return {cmd:f.cmd,pack:p,check:(change?:(p:any)=>void)=>{const copy=structuredClone(p);change?.(copy);const input=canonicalContextJSON(copy);return {input,proof:{artifact_ref:f.cmd.context_manifest_ref,sha256:sha(input),command_binding_digest:contextCommandDigest(f.cmd)}};}};
}
export function enterprise(action='nexloop.enterprise.deliver:37',delivery='nexloop.enterprise.deliver:1'){
 const f=declaration();return {action,offering_id:'3'.repeat(64),offering_revision:1,binding_id:'4'.repeat(64),binding_revision:1,provenance:'eios:object:'+'3'.repeat(64),properties:{...f.pack.supply!.properties,service_code:'enterprise.synthetic-delivery',title:'Synthetic delivery',delivery_action:delivery,content_kind:'enterprise-delivery',evidence_kind:'enterprise_receipt'}};
}
export function v5Declaration(){
 const f=roleDeclaration();const pack:any=f.pack;const ceilingId='5'.repeat(64),scopeId='6'.repeat(64);
 const facts=new Map(pack.formal_facts.map((x:any)=>[x.type,x.id]));
 const now=Date.now(),from=new Date(now-60000).toISOString(),until=new Date(now+3600000).toISOString();
 pack.schema_version='nexloop.context-pack.v5';pack.role_binding.definition.ceiling_ref=ceilingId;pack.role_binding.binding.scope=scopeId;
 pack.role_policy={binding:{run_id:f.cmd.run_id,tenant_id:f.cmd.tenant_id,world:'real',ceiling_id:ceilingId,ceiling_revision:1,scope_id:scopeId,scope_revision:1,budget:structuredClone(f.cmd.budget),effect_units:1,expires_at:new Date(now+30000).toISOString()},
  ceiling:{active:true,action_resources:['eios:action:nexloop.service.request:1'],consumer_ids:[pack.role_binding.binding.consumer_id],goal_ids:[facts.get('Goal')],step_ids:[facts.get('PlanStep')],budget:structuredClone(f.cmd.budget),effect_units:1,valid_from:from,valid_until:until},
  scope:{active:true,role_id:pack.role_binding.binding.role_id,consumer_id:pack.role_binding.binding.consumer_id,goal_ids:[facts.get('Goal')],step_ids:[facts.get('PlanStep')],valid_from:from,valid_until:until},
  ceiling_provenance:'eios:object:'+ceilingId,scope_provenance:'eios:object:'+scopeId,grants_authority:false};
 return f;
}
