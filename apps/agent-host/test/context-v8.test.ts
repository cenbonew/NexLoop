import {describe,it,expect} from 'vitest';
import {CONTEXT_PROTOCOL_V6,CONTEXT_PROTOCOL_V7,CONTEXT_PROTOCOL_V8,validateContextInput,validateContextProviderInput} from '../src/context-input.js';
import {enterpriseSupply,serviceAction,planRun} from '../src/runtime-host.js';
import {readFileSync} from 'node:fs';
import type {ContextPackV8} from '../../../packages/contracts/generated/contracts.js';
import {envelope,enterprise,v5Declaration} from './context-v8-support.ts';

describe('v8 exact resources, frozen legacy semantics',()=>{
 it.each([1,2,37,9999])('keeps exact service version %i in original provider input',n=>{
  const f=envelope(),v=f.check(p=>{p.current_constraints.action='nexloop.service.request:'+n;});
  expect(validateContextInput(v.input,f.cmd,v.proof,CONTEXT_PROTOCOL_V8).run_id).toBe(f.cmd.run_id);
  expect(validateContextProviderInput(v.input,f.cmd,v.proof,CONTEXT_PROTOCOL_V8)).toBe(v.input);
  expect(serviceAction(v.input)).toBe('nexloop.service.request:'+n);
 });
 it.each([1,2,37,9999])('matches enterprise offering by stable name while preserving bound version %i',n=>{
  const f=envelope(),v=f.check(p=>{p.effect_supplies=[enterprise('nexloop.enterprise.deliver:'+n)];});
  const out=validateContextInput(v.input,f.cmd,v.proof,CONTEXT_PROTOCOL_V8);
  expect(out.effect_supplies?.[0]?.action).toBe('nexloop.enterprise.deliver:'+n);
  expect(enterpriseSupply(v.input)?.action).toBe('nexloop.enterprise.deliver:'+n);
  expect(enterpriseSupply(v.input)?.exact_version).toBe(true);
 });
 it('accepts v8 Role and message shapes, keeps v7 Role/v6 compatibility',()=>{
  for(const [role,version] of [[true,8],[false,8],[true,7],[true,6],[false,6]] as const){
   const f=envelope(role,version),v=f.check();expect(validateContextInput(v.input,f.cmd,v.proof,CONTEXT_PROTOCOL_V8).run_id).toBe(f.cmd.run_id);
  }
  const f=envelope(true,7),v=f.check();expect(validateContextInput(v.input,f.cmd,v.proof,CONTEXT_PROTOCOL_V7).run_id).toBe(f.cmd.run_id);
 });
 it('does not route an enterprise primary resource through the service tool',()=>{
  const f=envelope(),v=f.check(p=>{p.current_constraints.action='nexloop.enterprise.deliver:37';p.supply.properties.delivery_action='nexloop.enterprise.deliver:1';});
  expect(validateContextInput(v.input,f.cmd,v.proof,CONTEXT_PROTOCOL_V8).run_id).toBe(f.cmd.run_id);
  expect(serviceAction(v.input)).toBeNull();
 });
 it.each(['0','01','10000','-1','1.0','1e2','1\n','','latest'])('rejects invalid version %j',version=>{
  for(const field of ['constraint','supply','effect','delivery']){
   const f=envelope(),v=f.check(p=>{p.effect_supplies=[enterprise()];
    if(field==='constraint')p.current_constraints.action='nexloop.service.request:'+version;
    if(field==='supply')p.supply.properties.delivery_action='nexloop.service.request:'+version;
    if(field==='effect')p.effect_supplies[0].action='nexloop.enterprise.deliver:'+version;
    if(field==='delivery')p.effect_supplies[0].properties.delivery_action='nexloop.enterprise.deliver:'+version;
   });expect(()=>validateContextInput(v.input,f.cmd,v.proof)).toThrow('runtime_context_invalid');
  }
 });
 it.each(['tenant','world','command','provenance','budget','read','family','effect-family','duplicate','role','statement'])('retains %s refusal',kind=>{
  const f=envelope(),v=f.check(p=>{
   if(kind==='tenant')p.bindings.tenant_id='11111111-1111-4111-8111-111111111111';
   if(kind==='world')p.bindings.world_id='simulation';
   if(kind==='command')p.bindings.command_digest='0'.repeat(64);
   if(kind==='provenance')p.supply.provenance='eios:object:'+'0'.repeat(64);
   if(kind==='budget')p.current_constraints.reserved_units=2;
   if(kind==='read')p.evidence=[{subsection:'conversation',ref:'message:x',revision:'1',content:{},content_hash:'0'.repeat(64),evidence_kind:'conversation',access_decision_ref:'unverified',relevance_permille:1,at:'',tags:[]}];
   if(kind==='family')p.supply.properties.delivery_action='nexloop.enterprise.deliver:1';
   if(kind==='effect-family')p.effect_supplies=[enterprise('nexloop.enterprise.deliver:37','nexloop.service.request:1')];
   if(kind==='duplicate')p.effect_supplies=[enterprise(),enterprise('nexloop.enterprise.deliver:38')];
   if(kind==='role')p.role={role_binding:null,role_policy:null};
   if(kind==='statement')p.current_event.message_id='0'.repeat(64);
  });expect(()=>validateContextInput(v.input,f.cmd,v.proof)).toThrow('runtime_context_invalid');
 });
 it('refuses v8 under legacy configuration and versioned actions disguised as legacy',()=>{
  const f=envelope(),v=f.check();for(const protocol of [CONTEXT_PROTOCOL_V6,CONTEXT_PROTOCOL_V7])expect(()=>validateContextInput(v.input,f.cmd,v.proof,protocol)).toThrow();
  for(const version of [6,7]){const g=envelope(true,version),w=g.check(p=>p.current_constraints.action='nexloop.service.request:2');expect(()=>validateContextInput(w.input,g.cmd,w.proof)).toThrow();}
 });
 it('recognizes only bound plan request plus Role trigger as plan Run',()=>{
  const f=envelope(true),v=f.check();expect(planRun({...f.cmd,request_id:'plan-reevaluate-'+f.cmd.run_id},v.input)).toBe(true);
  expect(planRun(f.cmd,v.input)).toBe(false);const g=envelope(),w=g.check();expect(planRun({...g.cmd,request_id:'plan-reevaluate-'+g.cmd.run_id},w.input)).toBe(false);
 });
});


describe('v8 consumes the published contract and retains Role policy checks',()=>{
 it('reads the existing generated type and public example, rebinding only synthetic Run data',()=>{
  const example:ContextPackV8=JSON.parse(readFileSync(new URL('../../../packages/contracts/examples/context-pack-v8.valid.json',import.meta.url),'utf8'));
  const f=envelope(),v=f.check(p=>{
   Object.assign(p,{strategy_ref:example.strategy_ref,supply:example.supply,current_constraints:example.current_constraints});
   p.current_constraints.valid_until=f.cmd.not_after;p.supply.properties.valid_until=f.cmd.not_after;
  });
  expect(validateContextInput(v.input,f.cmd,v.proof,CONTEXT_PROTOCOL_V8).run_id).toBe(f.cmd.run_id);
  expect(serviceAction(v.input)).toBe('nexloop.service.request:2');
 });
 it.each(['valid','scope','ceiling','binding','provenance'])('checks v8 Role policy %s with an exact successor action',kind=>{
  const base=v5Declaration(),f=envelope(true);Object.assign(f.cmd,base.cmd);
  Object.assign(f.pack,{current_event:base.pack.trigger_statement,bindings:base.pack.bindings,role:{role_binding:base.pack.role_binding,role_policy:base.pack.role_policy}});
  const v=f.check(p=>{
   p.current_constraints.action='nexloop.service.request:37';p.role.role_policy.ceiling.action_resources=['eios:action:nexloop.service.request:37'];
   if(kind==='scope')p.role.role_policy.scope.consumer_id='9'.repeat(64);
   if(kind==='ceiling')p.role.role_policy.ceiling.active=false;
   if(kind==='binding')p.role.role_policy.binding.run_id='11111111-1111-4111-8111-111111111111';
   if(kind==='provenance')p.role.role_policy.scope_provenance='eios:object:'+'9'.repeat(64);
  });
  if(kind==='valid')expect(validateContextInput(v.input,f.cmd,v.proof,CONTEXT_PROTOCOL_V8).run_id).toBe(f.cmd.run_id);
  else expect(()=>validateContextInput(v.input,f.cmd,v.proof,CONTEXT_PROTOCOL_V8)).toThrow('runtime_context_invalid');
 });
});

describe('legacy enterprise tool-surface projection compatibility',()=>{
 it.each([6,7])('keeps the v%i enterprise display projection without revalidating the full offering',version=>{
  const input=JSON.stringify({schema_version:'nexloop.context-pack.v'+version,effect_supplies:[{action:'nexloop.enterprise.deliver:1',offering_id:'o'.repeat(64),offering_revision:1,
   properties:{service_code:'enterprise.synthetic',title:'Synthetic delivery',price_amount:'100.00'}}]});
  expect(enterpriseSupply(input)).toEqual({action:'nexloop.enterprise.deliver:1',offering_id:'o'.repeat(64),offering_revision:1,service_code:'enterprise.synthetic',title:'Synthetic delivery',price_amount:'100.00'});
 });
 it('still refuses a v8 display projection with missing or mismatched delivery_action',()=>{
  for(const delivery of [undefined,'nexloop.service.request:1']){
   const offering=enterprise();offering.properties.delivery_action=delivery as string;
   expect(enterpriseSupply(JSON.stringify({schema_version:CONTEXT_PROTOCOL_V8,effect_supplies:[offering]}))).toBeUndefined();
  }
 });
});
