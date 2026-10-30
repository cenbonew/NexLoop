/** Outreach slice 5: the proactive message tool is opt-in Host configuration (default off) and rides on the plan outcome tool. */
import {afterEach,describe,it,expect} from 'vitest';
import {mkdtempSync,writeFileSync,chmodSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';import {join} from 'node:path';
import {RuntimeHost} from '../dist/runtime-host.js';import {readPrivateMaterial} from '../dist/private-material.js';
import {command} from './pi-runtime-support.ts';
const roots:string[]=[];const hosts:RuntimeHost[]=[];
afterEach(async()=>{for(const h of hosts.splice(0))await h.close();for(const r of roots.splice(0))rmSync(r,{recursive:true});});
function open(extra:Record<string,unknown>={}){
 const root=mkdtempSync(join(tmpdir(),'nexloop-outreach-tool-'));chmodSync(root,0o700);roots.push(root);
 const file=(name:string,text:string)=>{const p=join(root,name);writeFileSync(p,text,{mode:0o600});return p;};
 const config=file('configuration',JSON.stringify({guard_ca_file:file('ca','synthetic-unused-ca'),guard_key_file:file('guard-key','a'.repeat(64)),
  guard_url:'https://127.0.0.1:65431/internal/v1/runtime/authorize',runtime_profile:'deterministic-test',effect_tools:true,...extra}));
 const h=new RuntimeHost(root,config,readPrivateMaterial,()=>{});hosts.push(h);return h;
}
const PLAN_INPUT=JSON.stringify({schema_version:'nexloop.context-pack.v6',user_statement:null,current_event:{kind:'service_trigger'}});
const toolNames=(h:RuntimeHost,{input=PLAN_INPUT,request}:{input?:string|null;request?:(runId:string)=>string}={})=>{
 const c=command();c.request_id=request?request(c.run_id):'plan-reevaluate-'+c.run_id;
 if(input!==null)(h as unknown as {activations:Map<string,{ref:string;input:string}>}).activations.set(c.run_id,{ref:'activation_synthetic',input});
 const adapter=(h as unknown as {adapter:{options:{toolsForRun?:(c:unknown)=>readonly {name:string}[]}}}).adapter;
 return (adapter.options.toolsForRun?.(c)??[]).map(tool=>tool.name);
};
describe('outreach tool configuration',()=>{
 it('is not offered unless the Host configuration turns it on',()=>{
  expect(toolNames(open({plan_outcome_tool:true}))).not.toContain('nexloop.outreach.send');
 });
 it('is offered with outreach_tool and plan_outcome_tool',()=>{
  expect(toolNames(open({plan_outcome_tool:true,outreach_tool:true}))).toContain('nexloop.outreach.send');
 });
 it.each([{outreach_tool:true},{outreach_tool:false,plan_outcome_tool:true},{outreach_tool:'yes',plan_outcome_tool:true},
  {outreach_tool:true,plan_outcome_tool:true,effect_tools:false}])('rejects %j',extra=>{
  expect(()=>open(extra)).toThrow('runtime configuration refused');
 });
});

/** 10-30 stage S6 and L6 S47: the plan outcome and outreach tools go only to a Run positively known to be a plan reevaluation Run. */
describe('plan outcome tool only for plan reevaluation Runs',()=>{
 const PLAN=['nexloop.plan.outcome','nexloop.outreach.send'];
 it('is offered to a plan reevaluation Run',()=>{
  const names=toolNames(open({plan_outcome_tool:true,outreach_tool:true}));
  for(const name of PLAN)expect(names).toContain(name);
 });
 it.each([
  ['a message Run',{input:JSON.stringify({schema_version:'nexloop.context-pack.v6',user_statement:{message_id:'m'},current_event:{kind:'consumer_message'}})}],
  ['no activation',{input:null}],
  ['an unreadable input',{input:'not json'}],
  ['another request id (a Role or fallback Run)',{request:()=>'synthetic-role-request-0001'}],
  ['another Run\'s plan request id',{request:()=>'plan-reevaluate-00000000-0000-4000-8000-000000000000'}],
  ['an older pack version',{input:JSON.stringify({schema_version:'nexloop.context-pack.v3',user_statement:null,current_event:{kind:'service_trigger'}})}],
  ['a consumer event',{input:JSON.stringify({schema_version:'nexloop.context-pack.v6',user_statement:null,current_event:{kind:'consumer_message'}})}],
 ] as const)('is withheld on %s',(_,options)=>{
  const names=toolNames(open({plan_outcome_tool:true,outreach_tool:true}),options as never);
  for(const name of PLAN)expect(names).not.toContain(name);
  expect(names).toContain('nexloop.service.request');
 });
});
