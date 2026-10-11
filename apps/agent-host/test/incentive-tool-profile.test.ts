/** AT-043: the Agent's incentive tool is opt-in Host configuration (default off) and rides on the effect tools. */
import {afterEach,describe,it,expect} from 'vitest';
import {mkdtempSync,writeFileSync,chmodSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';import {join} from 'node:path';
import {RuntimeHost} from '../dist/runtime-host.js';import {readPrivateMaterial} from '../dist/private-material.js';
import {command} from './pi-runtime-support.ts';
const roots:string[]=[];const hosts:RuntimeHost[]=[];
afterEach(async()=>{for(const h of hosts.splice(0))await h.close();for(const r of roots.splice(0))rmSync(r,{recursive:true});});
function open(extra:Record<string,unknown>={}){
 const root=mkdtempSync(join(tmpdir(),'nexloop-incentive-tool-'));chmodSync(root,0o700);roots.push(root);
 const file=(name:string,text:string)=>{const p=join(root,name);writeFileSync(p,text,{mode:0o600});return p;};
 const config=file('configuration',JSON.stringify({guard_ca_file:file('ca','synthetic-unused-ca'),guard_key_file:file('guard-key','a'.repeat(64)),
  guard_url:'https://127.0.0.1:65431/internal/v1/runtime/authorize',runtime_profile:'deterministic-test',effect_tools:true,...extra}));
 const h=new RuntimeHost(root,config,readPrivateMaterial,()=>{});hosts.push(h);return h;
}
const toolNames=(h:RuntimeHost)=>{
 const adapter=(h as unknown as {adapter:{options:{toolsForRun?:(c:unknown)=>readonly {name:string}[]}}}).adapter;
 return (adapter.options.toolsForRun?.(command())??[]).map(tool=>tool.name);
};
describe('incentive tool configuration',()=>{
 it('is not offered unless the Host configuration turns it on',()=>{
  expect(toolNames(open())).not.toContain('nexloop.incentive.issue');
  expect(toolNames(open())).toContain('nexloop.service.request');
 });
 it('is offered with incentive_tool true',()=>{expect(toolNames(open({incentive_tool:true}))).toContain('nexloop.incentive.issue');});
 it.each([{incentive_tool:false},{incentive_tool:'yes'},{incentive_tool:true,effect_tools:false}])('rejects %j',extra=>{
  expect(()=>open(extra)).toThrow('runtime configuration refused');
 });
});
