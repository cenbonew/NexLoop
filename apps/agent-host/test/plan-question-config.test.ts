/** K4h: run-outcome 1.1 questions are opt-in Host configuration (plan_question_tool, default off), and only a plan Run whose own
 *  pack declares context-pack v7 is offered them; every other Run gets the 1.0 outcome tool, which refuses a 1.1 body before any request. */
import {afterEach,describe,it,expect} from 'vitest';
import {mkdtempSync,writeFileSync,chmodSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';import {join} from 'node:path';
import {RuntimeHost} from '../dist/runtime-host.js';import {readPrivateMaterial} from '../dist/private-material.js';
import {command} from './pi-runtime-support.ts';
const roots:string[]=[];const hosts:RuntimeHost[]=[];
afterEach(async()=>{for(const h of hosts.splice(0))await h.close();for(const r of roots.splice(0))rmSync(r,{recursive:true});});
const V7='nexloop.context-pack.v7',V6='nexloop.context-pack.v6';
const BASE={effect_tools:true,plan_outcome_tool:true,context_input_protocol:V7,deterministic_plan_outcome:'no_action'};
function open(extra:Record<string,unknown>={}){
 const root=mkdtempSync(join(tmpdir(),'nexloop-plan-question-'));chmodSync(root,0o700);roots.push(root);
 const file=(name:string,text:string)=>{const p=join(root,name);writeFileSync(p,text,{mode:0o600});return p;};
 const config=file('configuration',JSON.stringify({guard_ca_file:file('ca','synthetic-unused-ca'),guard_key_file:file('guard-key','a'.repeat(64)),
  guard_url:'https://127.0.0.1:65431/internal/v1/runtime/authorize',runtime_profile:'deterministic-test',...BASE,...extra}));
 const h=new RuntimeHost(root,config,readPrivateMaterial,()=>{});hosts.push(h);return h;
}
const pack=(version:string,event='service_trigger',statement:unknown=null)=>JSON.stringify({schema_version:version,user_statement:statement,current_event:{kind:event}});
type Tool={name:string;description:string;parameters:{properties:Record<string,{const?:string;enum?:string[]}>};execute:(args:unknown)=>Promise<{isError?:boolean;content:{text:string}[]}>};
const outcomeTool=(h:RuntimeHost,input:string,request?:(runId:string)=>string)=>{
 const c=command();c.request_id=request?request(c.run_id):'plan-reevaluate-'+c.run_id;
 (h as unknown as {activations:Map<string,{ref:string;input:string}>}).activations.set(c.run_id,{ref:'activation_synthetic',input});
 const adapter=(h as unknown as {adapter:{options:{toolsForRun?:(c:unknown)=>readonly Tool[]}}}).adapter;
 return (adapter.options.toolsForRun?.(c)??[]).find(tool=>tool.name==='nexloop.plan.outcome');
};
const asks=(tool:Tool|undefined)=>tool?.parameters.properties.question!==undefined;
const ASK={schema_version:'1.1',kind:'escalate',reasons:['需要人确认'],reassess_at:null,evidence_refs:[],intent_ref:null,plan_update:null,
 question:{audience:'operator',question_kind:'schedule_change',text:'可以改到周五吗？',options:['可以','不可以'],subject_ref:null,withdrawable:true}};
describe('plan_question_tool configuration',()=>{
 it('is off by default: a v7 plan Run gets the 1.0 outcome tool, which refuses a question before any request',async()=>{
  const tool=outcomeTool(open(),pack(V7))!;
  expect(tool).toBeDefined();expect(asks(tool)).toBe(false);expect(tool.parameters.properties.schema_version!.const).toBe('1.0');
  expect(tool.description).not.toMatch(/question|1\.1/);
  const refused=await tool.execute(ASK);
  expect(refused.isError).toBe(true);expect(refused.content[0]!.text).toContain('invalid_run_outcome');
 });
 it('when on, offers the question to a plan Run whose pack declares v7',()=>{
  const tool=outcomeTool(open({plan_question_tool:true}),pack(V7));
  expect(asks(tool)).toBe(true);expect(tool!.parameters.properties.schema_version!.enum).toEqual(['1.0','1.1']);
 });
 it.each([
  ['a v6 plan Run',pack(V6)],
  ['a message Run',pack(V7,'consumer_message',{message_id:'m'})],
 ])('when on, still withholds it from %s',async(_,input)=>{
  const tool=outcomeTool(open({plan_question_tool:true}),input);
  expect(asks(tool)).toBe(false);
  if(tool)expect((await tool.execute(ASK)).isError).toBe(true);
 });
 it('when on, withholds it from a Run that is not this plan\'s reevaluation',()=>{
  expect(outcomeTool(open({plan_question_tool:true}),pack(V7),()=>'synthetic-role-request-0001')).toBeUndefined();
 });
 it.each([{plan_question_tool:false},{plan_question_tool:'yes'},{plan_question_tool:true,plan_outcome_tool:false,deterministic_plan_outcome:undefined},
  {plan_question_tool:true,context_input_protocol:V6},{plan_question_tool:true,effect_tools:false}])('rejects %j',extra=>{
  expect(()=>open(extra)).toThrow('runtime configuration refused');
 });
});
