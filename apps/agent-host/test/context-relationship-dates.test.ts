/** Host v4 relationship zone dates: strict UTC calendar round-trip, never Date.parse alone. */
import {createHash,randomUUID} from 'node:crypto';
import {describe,it,expect} from 'vitest';
import {canonicalContextJSON,contextCommandDigest,validateContextInput,CONTEXT_PROTOCOL,CONTEXT_PROTOCOL_V2,CONTEXT_PROTOCOL_V4} from '../dist/context-input.js';
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
function relationship(change?:(row:any)=>void){
 const f=declaration();const pack:any=structuredClone(f.pack);pack.schema_version=CONTEXT_PROTOCOL_V4;
 const row:any={assessment_ref:'eios:object:RelationshipAssessment/'+'3'.repeat(64),revision:1,relation_type_ref:null,source_ref:'eios:object:Consumer/'+'4'.repeat(64),
  target_ref:'eios:object:Consumer/'+'5'.repeat(64),epistemic_kind:'hypothesis',resolution_state:'unresolved',conclusion:'synthetic hypothesis',
  valid_from:'2026-01-01T00:00:00Z',valid_to:null,source_message_ref:null,source_content_hash:null};
 change?.(row);pack.relationship_context={current_statements:[],evidence:[row]};
 const input=canonicalContextJSON(pack);
 return {input,cmd:f.cmd,proof:{artifact_ref:f.cmd.context_manifest_ref,sha256:sha(input),command_binding_digest:contextCommandDigest(f.cmd)}};
}
describe('v4 relationship zone dates',()=>{
 it('accepts an exact UTC instant',()=>{
  const r=relationship();expect(validateContextInput(r.input,r.cmd,r.proof,CONTEXT_PROTOCOL_V4).relationship_context.evidence).toHaveLength(1);
 });
 it.each([['rolled-over calendar date','2026-02-30T00:00:00Z'],['non-UTC offset','2026-01-01T08:00:00+08:00'],['missing zone','2026-01-01T00:00:00'],['date only','2026-01-01'],['free text','January 1, 2026']])('rejects %s',(_,value)=>{
  const r=relationship(row=>{row.valid_from=value;});expect(()=>validateContextInput(r.input,r.cmd,r.proof,CONTEXT_PROTOCOL_V4)).toThrow('runtime_context_invalid');
 });
 it('rejects a rolled-over valid_to as well',()=>{
  const r=relationship(row=>{row.valid_to='2026-13-01T00:00:00Z';});expect(()=>validateContextInput(r.input,r.cmd,r.proof,CONTEXT_PROTOCOL_V4)).toThrow('runtime_context_invalid');
 });
});
