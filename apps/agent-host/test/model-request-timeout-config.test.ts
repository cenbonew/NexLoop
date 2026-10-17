/** 10/18: model_request_timeout_ms is an integer 5000..120000 and only for the real model profile (a deterministic profile has no request to bound). */
import {afterEach,describe,it,expect} from 'vitest';
import {mkdtempSync,writeFileSync,chmodSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';import {join} from 'node:path';
import {RuntimeHost} from '../dist/runtime-host.js';import {readPrivateMaterial} from '../dist/private-material.js';
const roots:string[]=[];const hosts:RuntimeHost[]=[];
afterEach(async()=>{for(const h of hosts.splice(0))await h.close();for(const r of roots.splice(0))rmSync(r,{recursive:true});});
function open(extra:Record<string,unknown>={}){
 const root=mkdtempSync(join(tmpdir(),'nexloop-model-timeout-'));chmodSync(root,0o700);roots.push(root);
 const file=(name:string,text:string)=>{const p=join(root,name);writeFileSync(p,text,{mode:0o600});return p;};
 const config=file('configuration',JSON.stringify({guard_ca_file:file('ca','synthetic-unused-ca'),guard_key_file:file('guard-key','a'.repeat(64)),
  guard_url:'https://127.0.0.1:65431/internal/v1/runtime/authorize',runtime_profile:'deterministic-test',effect_tools:true,...extra}));
 const h=new RuntimeHost(root,config,readPrivateMaterial,()=>{});hosts.push(h);return h;
}
describe('model request timeout configuration',()=>{
 it.each([{model_request_timeout_ms:40000},{model_request_timeout_ms:4999,runtime_profile:'deepseek-flash'},{model_request_timeout_ms:120001,runtime_profile:'deepseek-flash'},
  {model_request_timeout_ms:30000.5,runtime_profile:'deepseek-flash'},{model_request_timeout_ms:'40000',runtime_profile:'deepseek-flash'},
  {model_connect_timeout_ms:4000},{model_connect_timeout_ms:999,runtime_profile:'deepseek-flash'},{model_connect_timeout_ms:10001,runtime_profile:'deepseek-flash'}])('rejects %j',extra=>{
  expect(()=>open(extra)).toThrow('runtime configuration refused');
 });
});
