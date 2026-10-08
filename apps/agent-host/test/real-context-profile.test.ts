/** Actual compiled constructor/files only; no provider call or EIOS positive stub. */
import {afterEach,describe,it,expect} from 'vitest';
import {mkdtempSync,writeFileSync,chmodSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';import {join} from 'node:path';
import {RuntimeHost} from '../dist/runtime-host.js';import {readPrivateMaterial} from '../dist/private-material.js';
import {command} from './pi-runtime-support.ts';
const roots:string[]=[];const hosts:RuntimeHost[]=[];
afterEach(async()=>{for(const h of hosts.splice(0))await h.close();for(const r of roots.splice(0))rmSync(r,{recursive:true});});
function setup(extra:Record<string,unknown>={}){
 const root=mkdtempSync(join(tmpdir(),'nexloop-real-context-'));chmodSync(root,0o700);roots.push(root);
 const file=(name:string,text:string)=>{const p=join(root,name);writeFileSync(p,text,{mode:0o600});return p;};
 const key=file('synthetic-model-key','synthetic-private-key');const model=file('model',JSON.stringify({MODEL_CREDENTIALS_FILE:key,stage:true}));
 const config=file('configuration',JSON.stringify({guard_ca_file:file('ca','synthetic-unused-ca'),guard_key_file:file('guard-key','a'.repeat(64)),
 guard_url:'https://127.0.0.1:65431/internal/v1/runtime/authorize',runtime_profile:'deepseek-flash',model_configuration_file:model,
 maximum_request_cost:'0.30491520',effect_tools:true,context_input_protocol:'nexloop.context-pack.v1',...extra}));
 return {root,config,open(){const h=new RuntimeHost(root,config,readPrivateMaterial,()=>{});hosts.push(h);return h;}};
}
describe('trusted explicit context with real provider configuration',()=>{
 it('constructs a genuine DeepSeek registry while profile mismatch rejects before guard/provider',async()=>{
  const f=setup();const h=f.open();const cmd=command();cmd.runtime_profile='deterministic-test';
  await expect(h.dispatch('start',{activation_ref:'activation_'+'a'.repeat(24),command:cmd,input:'not interpreted as context'})).rejects.toThrow('invalid_runtime_request');
  expect(JSON.stringify(h)).not.toContain('synthetic-private-key');
 });
 it.each([{context_input_protocol:'unknown'},{effect_tools:false},{deterministic_message_from_input:true},{deterministic_effect_message:'synthetic output'}])('rejects malformed/disabled/synthetic configuration %j',extra=>{
  const f=setup(extra);expect(()=>f.open()).toThrow('runtime configuration refused');
 });
});
