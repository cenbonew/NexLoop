/** Compiled parser/transport input declarations only; never grants EIOS authority. */
import {createHash,randomUUID} from 'node:crypto';
import {describe,it,expect} from 'vitest';
import {canonicalContextJSON,contextCommandDigest,validateContextInput,validateContextProviderInput,CONTEXT_PROTOCOL,CONTEXT_PROTOCOL_V2,CONTEXT_PROTOCOL_V3} from '../dist/context-input.js';
import {command} from './pi-runtime-support.ts';
const sha=(value:string)=>createHash('sha256').update(value).digest('hex');
function declaration(v2=true){
 const cmd=command();cmd.runtime_profile='deterministic-test';cmd.consumer_ref='consumer:'+'a'.repeat(64);cmd.goal_version_ref='goal:'+'b'.repeat(64)+':revision:1:step:1:control:1';cmd.credential_ref='run:'+cmd.run_id;
 const source='synthetic-source',artifact=sha(canonicalContextJSON([cmd.tenant_id,'real',source,'context:'+cmd.run_id])).slice(0,32);cmd.context_manifest_ref='artifact:'+artifact;
 const supply={offering_id:'1'.repeat(64),offering_revision:1,binding_id:'2'.repeat(64),binding_revision:1,provenance:'eios:object:'+'1'.repeat(64),properties:{service_code:'local.json-export',title:'当前正式免费 JSON 供给',delivery_action:'nexloop.service.request:1',content_kind:'json-message-export',price_amount:'0',currency:'CNY',eligibility:'current_consumer_plan',allowed_guarantees:[],allowed_discounts:[],evidence_kind:'fsynced_json_export',active:true,valid_until:cmd.not_after}};
 const pack={schema_version:v2?CONTEXT_PROTOCOL_V2:CONTEXT_PROTOCOL,bindings:{tenant_id:cmd.tenant_id,world_id:'real',run_id:cmd.run_id,source_principal:source,context_id:randomUUID(),namespace:sha(cmd.tenant_id+'\0real'),artifact_id:artifact,command_digest:contextCommandDigest(cmd)},user_statement:{message_id:'e'.repeat(64),conversation_id:'f'.repeat(64),sequence:1,body:'用户原文，不是权利授予。',provenance:'eios:object:'+'e'.repeat(64)},formal_facts:[['Consumer','a'],['Goal','b'],['PlanStep','c'],['EffectControl','d']].map(([type,id])=>({type,id:id!.repeat(64),revision:1,provenance:'eios:object:'+id!.repeat(64)})),current_constraints:{action:'nexloop.service.request:1',allow_effect:true,budget_units:1,reserved_units:0,executor_principal:'synthetic-executor',valid_until:cmd.not_after},...(v2?{supply}:{})};
 const check=(change?:(value:any)=>void,raw?:string)=>{const copy=structuredClone(pack);change?.(copy);const input=raw??canonicalContextJSON(copy);return {input,proof:{artifact_ref:cmd.context_manifest_ref,sha256:sha(input),command_binding_digest:contextCommandDigest(cmd)}};};
 return {cmd,pack,check};
}

function roleDeclaration(){
 const f=declaration();const roleId='3'.repeat(64),linkId='4'.repeat(64);
 f.cmd.role_ref='role:'+roleId+':mapping:'+linkId;
 const pack:any=f.pack;pack.trigger_statement={kind:'service_trigger',event_id:f.cmd.trigger_event_id,source_principal:pack.bindings.source_principal,body:pack.user_statement.body,provenance:'eios:role-trigger:'+f.cmd.trigger_event_id};delete pack.user_statement;pack.schema_version=CONTEXT_PROTOCOL_V3;pack.bindings.command_digest=contextCommandDigest(f.cmd);
 pack.role_binding={binding:{run_id:f.cmd.run_id,tenant_id:f.cmd.tenant_id,world:'real',consumer_id:'a'.repeat(64),link_id:linkId,role_id:roleId,step_id:'c'.repeat(64),link_revision:1,role_revision:1,step_revision:1,role_ref:f.cmd.role_ref,scope:'local-service',expires_at:new Date(Date.now()+30000).toISOString()},definition:{name:'delivery-role',responsibility:'真实定义的职责',ceiling_ref:'metadata-only',active:true,valid_from:new Date(Date.now()-30000).toISOString(),valid_until:new Date(Date.now()+60000).toISOString()},definition_provenance:'eios:object:'+roleId,mapping_provenance:'eios:object:'+linkId,grants_authority:false};
 return {cmd:f.cmd,pack,check:(change?:(value:any)=>void)=>{const value=structuredClone(pack);change?.(value);const input=canonicalContextJSON(value);return {input,proof:{artifact_ref:f.cmd.context_manifest_ref,sha256:sha(input),command_binding_digest:contextCommandDigest(f.cmd)}};}};
}
describe('explicit role context v3 declarations, never dispatch authority',()=>{
 it('retains bound responsibility and supply in provider input',()=>{const f=roleDeclaration(),v=f.check();expect(validateContextInput(v.input,f.cmd,v.proof,CONTEXT_PROTOCOL_V3).run_id).toBe(f.cmd.run_id);expect(validateContextProviderInput(v.input,f.cmd,v.proof,CONTEXT_PROTOCOL_V3)).toBe(v.input);});
 it.each(['missing','unknown','grant','role_ref','consumer','step_revision','source_run','role_revision','provenance','inactive','naive','interval','expired','hypothesis','awaiting_definition'])('rejects %s role input',kind=>{
  const f=roleDeclaration(),v=f.check(p=>{const r=p.role_binding;
   if(kind==='missing')delete p.role_binding;else if(kind==='unknown')r.approval=true;else if(kind==='grant')r.grants_authority=true;else if(kind==='role_ref')r.binding.role_ref='opaque';else if(kind==='consumer')r.binding.consumer_id='9'.repeat(64);else if(kind==='step_revision')r.binding.step_revision=2;else if(kind==='source_run')r.binding.run_id=randomUUID();else if(kind==='role_revision')r.binding.role_revision=0;else if(kind==='provenance')r.definition_provenance='eios:object:'+'9'.repeat(64);else if(kind==='inactive')r.definition.active=false;else if(kind==='naive')r.definition.valid_from='2026-01-01T00:00:00';else if(kind==='interval')r.definition.valid_from=r.definition.valid_until;else if(kind==='expired')r.binding.expires_at='2026-01-01T00:00:00Z';else p.formal_facts.push({type:kind,id:'5'.repeat(64),revision:1,provenance:'eios:object:'+'5'.repeat(64)});
  });expect(()=>validateContextInput(v.input,f.cmd,v.proof,CONTEXT_PROTOCOL_V3)).toThrow('runtime_context_invalid');
 });
 it('v2 does not silently accept role sections or satisfy explicit v3 config',()=>{const f=declaration(),v=f.check();expect(()=>validateContextInput(v.input,f.cmd,v.proof,CONTEXT_PROTOCOL_V3)).toThrow();const g=roleDeclaration(),w=g.check(p=>p.schema_version=CONTEXT_PROTOCOL_V2);expect(()=>validateContextInput(w.input,g.cmd,w.proof)).toThrow();});
});
