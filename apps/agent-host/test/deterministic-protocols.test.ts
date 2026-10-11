/** NX-031 slice 3: one deterministic-test Host serves message (v2), plan-reevaluation (v6) and fallback-reply (v6) Runs, each by
 * the protocol its own start input declares. Test profile only; deepseek-flash refuses the key. */
import {afterEach,describe,it,expect} from 'vitest';
import {mkdtempSync,writeFileSync,chmodSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';import {join} from 'node:path';
import {RuntimeHost} from '../dist/runtime-host.js';import {readPrivateMaterial} from '../dist/private-material.js';
const roots:string[]=[];const hosts:RuntimeHost[]=[];
afterEach(async()=>{for(const h of hosts.splice(0))await h.close();for(const r of roots.splice(0))rmSync(r,{recursive:true});});
function setup(base:Record<string,unknown>,extra:Record<string,unknown>={}){
 const root=mkdtempSync(join(tmpdir(),'nexloop-protocols-'));chmodSync(root,0o700);roots.push(root);
 const file=(name:string,text:string)=>{const p=join(root,name);writeFileSync(p,text,{mode:0o600});return p;};
 const model=file('model',JSON.stringify({MODEL_CREDENTIALS_FILE:file('synthetic-model-key','synthetic-private-key'),stage:true}));
 const common={guard_ca_file:file('ca','synthetic-unused-ca'),guard_key_file:file('guard-key','a'.repeat(64)),guard_url:'https://127.0.0.1:65431/internal/v1/runtime/authorize'};
 const resolved=base.runtime_profile==='deepseek-flash'?{...base,model_configuration_file:model}:base;
 const config=file('configuration',JSON.stringify({...common,...resolved,...extra}));
 return ()=>{const h=new RuntimeHost(root,config,readPrivateMaterial,()=>{});hosts.push(h);return h;};
}
const multi={runtime_profile:'deterministic-test',effect_tools:true,plan_outcome_tool:true,deterministic_protocols:true};
describe('deterministic_protocols',()=>{
 it.each([{},{deterministic_plan_outcome:'action_intent'},{deterministic_latency:{median_ms:1500,p95_ms:4000,seed:31}},{maximum_active_runs:4}])('accepts the lane configuration %j',extra=>{
  expect(setup(multi,extra)()).toBeInstanceOf(RuntimeHost);
 });
 it.each([
  {deterministic_protocols:'yes'},{effect_tools:false},{plan_outcome_tool:undefined},{context_input_protocol:'nexloop.context-pack.v2'},
  {deterministic_message_from_input:true},{deterministic_reply_once:true},{deterministic_effect_message:'synthetic'},
  {deterministic_relationship_from_context:true},{deterministic_plan_outcome:'plan_update'},
 ])('refuses %j',extra=>{expect(()=>setup(multi,extra)()).toThrow('runtime configuration refused');});
 it('is refused for deepseek-flash (the same configuration without it constructs)',()=>{
  const deepseek={runtime_profile:'deepseek-flash',maximum_request_cost:'0.30491520',effect_tools:true,plan_outcome_tool:true,context_input_protocol:'nexloop.context-pack.v6'};
  expect(setup(deepseek)()).toBeInstanceOf(RuntimeHost);
  expect(()=>setup(deepseek,{deterministic_protocols:true})()).toThrow('runtime configuration refused');
 });
});
