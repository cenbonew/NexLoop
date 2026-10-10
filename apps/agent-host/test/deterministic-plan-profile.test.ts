/** NX-025: the explicit deterministic plan-reevaluation profile is test-only and narrowly configured. */
import {afterEach,describe,it,expect} from 'vitest';
import {mkdtempSync,writeFileSync,chmodSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';import {join} from 'node:path';
import {RuntimeHost} from '../dist/runtime-host.js';import {readPrivateMaterial} from '../dist/private-material.js';
const roots:string[]=[];const hosts:RuntimeHost[]=[];
afterEach(async()=>{for(const h of hosts.splice(0))await h.close();for(const r of roots.splice(0))rmSync(r,{recursive:true});});
function setup(extra:Record<string,unknown>={}){
 const root=mkdtempSync(join(tmpdir(),'nexloop-plan-profile-'));chmodSync(root,0o700);roots.push(root);
 const file=(name:string,text:string)=>{const p=join(root,name);writeFileSync(p,text,{mode:0o600});return p;};
 const config=file('configuration',JSON.stringify({guard_ca_file:file('ca','synthetic-unused-ca'),guard_key_file:file('guard-key','a'.repeat(64)),
  guard_url:'https://127.0.0.1:65431/internal/v1/runtime/authorize',runtime_profile:'deterministic-test',effect_tools:true,plan_outcome_tool:true,
  context_input_protocol:'nexloop.context-pack.v6',deterministic_plan_outcome:'no_action',...extra}));
 return {open(){const h=new RuntimeHost(root,config,readPrivateMaterial,()=>{});hosts.push(h);return h;}};
}
describe('deterministic plan-reevaluation profile',()=>{
 it.each(['no_action','action_intent'])('constructs only as an explicit v6 test profile (%s)',mode=>{expect(setup({deterministic_plan_outcome:mode}).open()).toBeInstanceOf(RuntimeHost);});
 it.each([{deterministic_plan_outcome:'plan_update'},{plan_outcome_tool:false},{effect_tools:false},{context_input_protocol:'nexloop.context-pack.v5'},
   {deterministic_message_from_input:true},{deterministic_effect_message:'synthetic output'}])('rejects %j',extra=>{
  expect(()=>setup(extra).open()).toThrow('runtime configuration refused');
 });
});
describe('deterministic fallback-reply profile',()=>{
 const reply=(extra:Record<string,unknown>={})=>setup({plan_outcome_tool:undefined,deterministic_plan_outcome:undefined,deterministic_reply_once:true,...extra});
 it('constructs only as an explicit v6 test profile',()=>{expect(reply().open()).toBeInstanceOf(RuntimeHost);});
 it.each([{deterministic_reply_once:'yes'},{effect_tools:false},{context_input_protocol:'nexloop.context-pack.v5'},{deterministic_message_from_input:true},{deterministic_plan_outcome:'no_action',plan_outcome_tool:true}])('rejects %j',extra=>{
  expect(()=>reply(extra).open()).toThrow('runtime configuration refused');
 });
});
