/** NX-031 D5: injected model latency is a deterministic-test-only, bounded, reproducible sleep that never changes a response. */
import {afterEach,describe,it,expect} from 'vitest';
import {mkdtempSync,writeFileSync,chmodSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';import {join} from 'node:path';
import {fauxAssistantMessage,type Context} from '@earendil-works/pi-ai';
import {RuntimeHost} from '../dist/runtime-host.js';import {readPrivateMaterial} from '../dist/private-material.js';
import {delayed,latencySampler,parseDeterministicLatency} from '../dist/deterministic-latency.js';
const roots:string[]=[];const hosts:RuntimeHost[]=[];
afterEach(async()=>{for(const h of hosts.splice(0))await h.close();for(const r of roots.splice(0))rmSync(r,{recursive:true});});
const LATENCY={median_ms:1500,p95_ms:4000,seed:20261011};
function setup(base:Record<string,unknown>,extra:Record<string,unknown>={}){
 const root=mkdtempSync(join(tmpdir(),'nexloop-latency-'));chmodSync(root,0o700);roots.push(root);
 const file=(name:string,text:string)=>{const p=join(root,name);writeFileSync(p,text,{mode:0o600});return p;};
 const model=file('model',JSON.stringify({MODEL_CREDENTIALS_FILE:file('synthetic-model-key','synthetic-private-key'),stage:true}));
 const common={guard_ca_file:file('ca','synthetic-unused-ca'),guard_key_file:file('guard-key','a'.repeat(64)),guard_url:'https://127.0.0.1:65431/internal/v1/runtime/authorize'};
 const resolved=base.runtime_profile==='deepseek-flash'?{...base,model_configuration_file:model}:base;
 const config=file('configuration',JSON.stringify({...common,...resolved,...extra}));
 return ()=>{const h=new RuntimeHost(root,config,readPrivateMaterial,()=>{});hosts.push(h);return h;};
}
const message={runtime_profile:'deterministic-test',effect_tools:true,deterministic_message_from_input:true,context_input_protocol:'nexloop.context-pack.v2'};
const deepseek={runtime_profile:'deepseek-flash',maximum_request_cost:'0.30491520',effect_tools:true,context_input_protocol:'nexloop.context-pack.v2'};
describe('deterministic_latency configuration',()=>{
 it('is accepted with a deterministic-test per-context protocol',()=>{
  expect(setup(message,{deterministic_latency:LATENCY})()).toBeInstanceOf(RuntimeHost);
  expect(setup({runtime_profile:'deterministic-test',effect_tools:true,deterministic_reply_once:true,context_input_protocol:'nexloop.context-pack.v6'},{deterministic_latency:LATENCY})()).toBeInstanceOf(RuntimeHost);
 });
 it('is refused for the deepseek-flash profile (the same configuration without it constructs)',()=>{
  expect(setup(deepseek)()).toBeInstanceOf(RuntimeHost);
  expect(()=>setup(deepseek,{deterministic_latency:LATENCY})()).toThrow('runtime configuration refused');
 });
 it.each([
  {deterministic_latency:{...LATENCY,extra:1}},{deterministic_latency:{median_ms:1500,p95_ms:4000}},{deterministic_latency:[1500,4000,1]},
  {deterministic_latency:{...LATENCY,median_ms:0}},{deterministic_latency:{...LATENCY,median_ms:10001,p95_ms:20000}},
  {deterministic_latency:{...LATENCY,p95_ms:30001}},{deterministic_latency:{...LATENCY,p95_ms:1000}},{deterministic_latency:{...LATENCY,median_ms:1.5}},
  {deterministic_latency:{...LATENCY,seed:-1}},{deterministic_latency:{...LATENCY,seed:2**32}},{deterministic_latency:{...LATENCY,seed:'1'}},
 ])('refuses malformed or out-of-bound values %j',extra=>{expect(()=>setup(message,extra)()).toThrow('runtime configuration refused');});
 it('is refused without a per-context scripted protocol',()=>{
  expect(()=>setup({runtime_profile:'deterministic-test'},{deterministic_latency:LATENCY})()).toThrow('runtime configuration refused');
  expect(()=>setup({runtime_profile:'deterministic-test',effect_tools:true,deterministic_effect_message:'synthetic'},{deterministic_latency:LATENCY})()).toThrow('runtime configuration refused');
 });
 it('parses only for deterministic-test',()=>{
  expect(parseDeterministicLatency(undefined,'deepseek-flash')).toBeUndefined();
  expect(parseDeterministicLatency(LATENCY,'deterministic-test')).toEqual(LATENCY);
  expect(()=>parseDeterministicLatency(LATENCY,'deepseek-flash')).toThrow('runtime configuration refused');
 });
});
describe('latency samples',()=>{
 const draw=(config:typeof LATENCY,n:number)=>{const s=latencySampler(config);return Array.from({length:n},()=>s());};
 it('are reproducible from the seed',()=>{
  expect(draw(LATENCY,500)).toEqual(draw(LATENCY,500));
  expect(draw({...LATENCY,seed:LATENCY.seed+1},50)).not.toEqual(draw(LATENCY,50));
 });
 it('follow the configured median and p95 and stay within the bound',()=>{
  const values=draw(LATENCY,20000).sort((a,b)=>a-b);
  const median=values[10000]!,p95=values[19000]!;
  expect(Math.abs(median-1500)/1500).toBeLessThan(0.05);expect(Math.abs(p95-4000)/4000).toBeLessThan(0.05);
  const wide=draw({median_ms:10000,p95_ms:30000,seed:7},20000);
  expect(Math.max(...wide)).toBeLessThanOrEqual(30000);expect(Math.min(...wide)).toBeGreaterThan(0);
 });
 it('only delay the scripted response, never change it',async()=>{
  const response=(context:Context)=>fauxAssistantMessage('echo:'+context.messages.length);
  const wrapped=delayed(response,()=>60);const context={messages:[{role:'user',content:'x',timestamp:0}]} as Context;
  const started=performance.now();const message=await wrapped(context);
  expect(performance.now()-started).toBeGreaterThanOrEqual(55);
  expect({...message,timestamp:0}).toEqual({...response(context),timestamp:0});
 });
});
