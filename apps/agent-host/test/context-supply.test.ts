/** Compiled parser/transport input declarations only; never grants EIOS authority. */
import {createHash,randomUUID} from 'node:crypto';
import {describe,it,expect} from 'vitest';
import {canonicalContextJSON,contextCommandDigest,validateContextInput,validateContextProviderInput,CONTEXT_PROTOCOL,CONTEXT_PROTOCOL_V2} from '../dist/context-input.js';
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
describe('strict offering context pack v2 and unchanged v1 compatibility',()=>{
 it('retains full canonical supply in actual provider input, extracts only user body for synthetic tool',()=>{
  const f=declaration();const {input,proof}=f.check();
  expect(validateContextInput(input,f.cmd,proof,CONTEXT_PROTOCOL_V2)).toEqual({body:f.pack.user_statement.body,run_id:f.cmd.run_id});
  const provider=validateContextProviderInput(input,f.cmd,proof,CONTEXT_PROTOCOL_V2);expect(provider).toBe(input);expect(JSON.parse(provider).supply).toEqual(f.pack.supply);
 });
 it('v1 remains strict compatibility but cannot satisfy explicit v2 config',()=>{
  const f=declaration(false);const {input,proof}=f.check();expect(validateContextInput(input,f.cmd,proof,CONTEXT_PROTOCOL)).toEqual({body:f.pack.user_statement.body,run_id:f.cmd.run_id});
  expect(()=>validateContextInput(input,f.cmd,proof,CONTEXT_PROTOCOL_V2)).toThrow('runtime_context_invalid');
 });
 it.each(['offering_id','offering_revision','binding_id','binding_revision','provenance','properties'])('requires exact supply field %s',key=>{
  const f=declaration();const {input,proof}=f.check(p=>delete p.supply[key]);expect(()=>validateContextInput(input,f.cmd,proof)).toThrow('runtime_context_invalid');
 });
 it.each(['service_code','title','delivery_action','content_kind','price_amount','currency','eligibility','allowed_guarantees','allowed_discounts','evidence_kind','active','valid_until'])('requires exact property %s',key=>{
  const f=declaration();const {input,proof}=f.check(p=>delete p.supply.properties[key]);expect(()=>validateContextInput(input,f.cmd,proof)).toThrow('runtime_context_invalid');
 });
 it.each(['negative','boolean','unsafe','nan','wrong_id','extra','extra_property','price','currency','action','rights','discount','eligibility','inactive','date','title'])('rejects malformed or over-granted %s supply',kind=>{
  const f=declaration();const mutated=structuredClone(f.pack);const mutate=(p:any)=>{
   const s=p.supply,prop=s.properties;
   if(kind==='negative')s.offering_revision=0;else if(kind==='boolean')s.binding_revision=true;else if(kind==='unsafe')s.binding_revision=Number.MAX_SAFE_INTEGER+1;else if(kind==='nan')s.offering_revision=null;
   else if(kind==='wrong_id')s.offering_id='uuid:'+randomUUID();else if(kind==='extra')s.approval=true;else if(kind==='extra_property')prop.approval=true;else if(kind==='price')prop.price_amount='10';else if(kind==='currency')prop.currency='USD';else if(kind==='action')prop.delivery_action='unsafe.send:1';else if(kind==='rights')prop.allowed_guarantees=['guaranteed'];else if(kind==='discount')prop.allowed_discounts=['50%'];else if(kind==='eligibility')prop.eligibility='everyone';else if(kind==='inactive')prop.active=false;else if(kind==='date')prop.valid_until='2026-02-31T00:00:00Z';else prop.title='x'.repeat(257);
  };mutate(mutated);const raw=kind==='unsafe'?JSON.stringify(mutated):canonicalContextJSON(mutated);const {input,proof}=f.check(undefined,raw);expect(()=>validateContextInput(input,f.cmd,proof)).toThrow('runtime_context_invalid');
 });
 it('rejects whole-input supply tampering under original current guard hash',()=>{
  const f=declaration();const original=f.check();const changed=f.check(p=>p.supply.properties.title='篡改供给');expect(()=>validateContextInput(changed.input,f.cmd,original.proof)).toThrow('runtime_context_invalid');
 });
 it('rejects duplicate JSON key and NaN encodings, whitespace and unknown protocol',()=>{
  const f=declaration();const base=f.check();for(const raw of [base.input.replace('"supply":','"supply":{},"supply":'),' '+base.input,base.input.replace('"offering_revision":1','"offering_revision":NaN'),base.input.replace(CONTEXT_PROTOCOL_V2,'unknown')]){
   const value=f.check(undefined,raw);expect(()=>validateContextInput(value.input,f.cmd,value.proof)).toThrow('runtime_context_invalid');
  }
 });
 it('bounds UTF8 bytes independently of codepoint count',()=>{
  const f=declaration();const raw=f.check().input+' '.repeat(65536);expect(()=>validateContextInput(raw,f.cmd,f.check(undefined,raw).proof)).toThrow('runtime_context_invalid');
 });
});
