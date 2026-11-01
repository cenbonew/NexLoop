/** NX-024 run-outcome tool: actual TLS transport, exact contract shape and fixed error codes only (no EIOS claim). */
import {createServer,type Server} from 'node:https';
import {mkdtempSync,readFileSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {execFileSync} from 'node:child_process';
import {once} from 'node:events';
import {afterEach,describe,expect,it} from 'vitest';
import {RuntimeEffectClient,runOutcome} from '../src/runtime-effect-tools.ts';
import {command} from './pi-runtime-support.ts';

const resources:{server:Server;directory:string}[]=[];
const step={step_key:'confirm-payment',step_object_id:null,prerequisites:['续费意向已确认'],expected_result:'确认付款方式',
  stop_if:[{type_name:'Consumer',object_id:'1'.repeat(64),property:'renewed',equals:true}],reassess_at:'2026-11-01T02:00:00Z',
  budget:{maximum_model_turns:8,maximum_tool_calls:12,active_timeout_seconds:300},intent_ref:null};
const noAction={schema_version:'1.0',kind:'no_action',reasons:['当前无合理行动'],reassess_at:null,evidence_refs:[],intent_ref:null,plan_update:null};
const question={audience:'operator',question_kind:'schedule_change',text:'客户希望改到周五下午，是否可以？',options:['可以','不可以'],subject_ref:'consumer:synthetic-test-1',withdrawable:true};
const asks={...noAction,schema_version:'1.1',kind:'escalate',question};
const contract=JSON.parse(readFileSync(new URL('../../../packages/contracts/run-outcome.schema.json',import.meta.url),'utf8')) as {properties:Record<string,Record<string,unknown>>};
async function guard(reply:(body:Record<string,unknown>)=>{status:number;value:unknown}){
  const directory=mkdtempSync(join(tmpdir(),'nexloop-outcome-tls-'));
  execFileSync('openssl',['req','-x509','-newkey','rsa:2048','-nodes','-keyout',join(directory,'key'),'-out',join(directory,'cert'),'-days','1','-subj','/CN=127.0.0.1','-addext','subjectAltName=IP:127.0.0.1'],{stdio:'ignore'});
  const seen:{path:string;body:Record<string,unknown>}[]=[];
  const server=createServer({key:readFileSync(join(directory,'key')),cert:readFileSync(join(directory,'cert'))},(request,response)=>{
    const chunks:Buffer[]=[];request.on('data',chunk=>chunks.push(chunk));request.on('end',()=>{
      const body=JSON.parse(Buffer.concat(chunks).toString('utf8')) as Record<string,unknown>;seen.push({path:request.url??'',body});
      const {status,value}=reply(body);response.writeHead(status,{'Content-Type':'application/json'});response.end(JSON.stringify(value));
    });
  });server.listen(0,'127.0.0.1');await once(server,'listening');resources.push({server,directory});
  const port=(server.address() as {port:number}).port;
  const client=new RuntimeEffectClient({guardURL:new URL(`https://127.0.0.1:${port}/internal/v1/runtime/authorize`),keyPath:'transport-key',caPath:'ca',
    privateMaterial:path=>path==='ca'?readFileSync(join(directory,'cert')):Buffer.from('c'.repeat(64)),activationForRun:()=>'activation_'+'A'.repeat(24)});
  return {client,seen,cmd:command()};
}
afterEach(async()=>{for(const {server,directory} of resources.splice(0)){server.closeAllConnections();await new Promise<void>(resolve=>server.close(()=>resolve()));rmSync(directory,{recursive:true,force:true});}});

describe('plan run-outcome tool over the fixed HTTPS guard',()=>{
  it('posts activation, the full command and the exact outcome; returns only the projected result',async()=>{
    const {client,seen,cmd}=await guard(body=>({status:200,value:{run_id:(body.command as {run_id:string}).run_id,recorded:true,replay:false,kind:'plan_update',plan_version:1,new_version:2}}));
    const outcome={...noAction,kind:'plan_update',plan_update:{strategy:null,steps:[step]}};
    expect(await client.recordOutcome(cmd,outcome)).toEqual({run_id:cmd.run_id,recorded:true,replay:false,kind:'plan_update',plan_version:1,new_version:2});
    expect(seen[0]!.path).toBe('/internal/v1/runtime/outcomes/record');
    expect(Object.keys(seen[0]!.body).sort()).toEqual(['activation_ref','command','outcome']);expect(seen[0]!.body.outcome).toEqual(outcome);
    // The tool is registered only when the Host enables it for reevaluation Runs.
    expect(client.toolsForRun(cmd).map(tool=>tool.name)).toEqual(['nexloop.service.request','nexloop.service.find']);
    expect(client.toolsForRun(cmd,{planOutcome:true}).map(tool=>tool.name)).toEqual(['nexloop.plan.outcome','nexloop.service.request','nexloop.service.find']);
  });
  it.each([
    ['extra key',{...noAction,tenant_id:'t'}],['unknown kind',{...noAction,kind:'retry'}],['plan_update without steps',{...noAction,kind:'plan_update'}],
    ['steps on no_action',{...noAction,plan_update:{strategy:null,steps:[step]}}],['action_intent without intent',{...noAction,kind:'action_intent'}],
    ['waiting without time or intent',{...noAction,kind:'waiting_external'}],['naive time',{...noAction,reassess_at:'2026-11-01T02:00:00'}],
    ['bad step budget',{...noAction,kind:'plan_update',plan_update:{strategy:null,steps:[{...step,budget:{...step.budget,maximum_model_turns:65}}]}}],
    ['duplicate steps',{...noAction,kind:'plan_update',plan_update:{strategy:null,steps:[step,step]}}],
  ])('refuses %s before any request',async(_name,outcome)=>{
    expect(()=>runOutcome(outcome)).toThrow('invalid_run_outcome');
    const {client,seen,cmd}=await guard(()=>({status:200,value:{}}));
    await expect(client.recordOutcome(cmd,outcome)).rejects.toMatchObject({code:'invalid_run_outcome'});expect(seen).toHaveLength(0);
  });
  it('K4a: accepts 1.0 and 1.1, and posts a 1.1 question exactly as given',async()=>{
    expect(runOutcome(noAction)).toEqual(noAction);expect(runOutcome(asks)).toEqual(asks);
    expect(runOutcome({...asks,kind:'needs_information'})).toEqual({...asks,kind:'needs_information'});
    const {question:_,...without}=asks;expect(runOutcome(without)).toEqual(without);
    expect(runOutcome({...asks,question:{...question,options:[],subject_ref:null}}).question).toEqual({...question,options:[],subject_ref:null});
    const {client,seen,cmd}=await guard(body=>({status:200,value:{run_id:(body.command as {run_id:string}).run_id,recorded:true,replay:false,kind:'escalate',plan_version:1,new_version:null}}));
    expect((await client.recordOutcome(cmd,asks)).kind).toBe('escalate');expect(seen[0]!.body.outcome).toEqual(asks);
  });
  it.each([
    ['a question on 1.0',{...asks,schema_version:'1.0'}],['version 1.2',{...asks,schema_version:'1.2'}],
    ['a question on no_action',{...asks,kind:'no_action'}],['a question on plan_update',{...asks,kind:'plan_update',plan_update:{strategy:null,steps:[step]}}],
    ['a null question',{...asks,question:null}],['audience reviewer',{...asks,question:{...question,audience:'reviewer'}}],
    ['unknown question kind',{...asks,question:{...question,question_kind:'refund'}}],['empty text',{...asks,question:{...question,text:''}}],
    ['text over 300',{...asks,question:{...question,text:'问'.repeat(301)}}],['five options',{...asks,question:{...question,options:['a','b','c','d','e']}}],
    ['duplicate options',{...asks,question:{...question,options:['a','a']}}],['option over 60',{...asks,question:{...question,options:['x'.repeat(61)]}}],
    ['bad subject',{...asks,question:{...question,subject_ref:'no reference'}}],['withdrawable as text',{...asks,question:{...question,withdrawable:'yes'}}],
    ['extra question key',{...asks,question:{...question,answer_roles:['operator']}}],['missing withdrawable',{...asks,question:(({withdrawable:_w,...rest})=>rest)(question)}],
  ])('K4a: refuses %s before any request',async(_name,outcome)=>{
    expect(()=>runOutcome(outcome)).toThrow('invalid_run_outcome');
    const {client,seen,cmd}=await guard(()=>({status:200,value:{}}));
    await expect(client.recordOutcome(cmd,outcome)).rejects.toMatchObject({code:'invalid_run_outcome'});expect(seen).toHaveLength(0);
  });
  it('K4a: the tool parameters offer the contract versions and question, never wider',async()=>{
    const {client,cmd}=await guard(()=>({status:200,value:{}}));
    const tool=client.toolsForRun(cmd,{planOutcome:true,planQuestion:true}).find(item=>item.name==='nexloop.plan.outcome')! as unknown as {parameters:{properties:Record<string,any>;required:string[]}};
    const parameters=tool.parameters;
    expect(parameters.properties.schema_version.enum).toEqual(contract.properties.schema_version!.enum);  // S84 compact form: enum, not anyOf
    expect(parameters.required).not.toContain('question');
    const offered=parameters.properties.question,canonical=contract.properties.question as {properties:Record<string,any>;required:string[]};
    expect(offered.additionalProperties).toBe(false);expect([...offered.required].sort()).toEqual([...canonical.required].sort());
    expect(offered.properties.audience.enum).toEqual(canonical.properties.audience.enum);
    expect(offered.properties.question_kind.enum).toEqual(canonical.properties.question_kind.enum);
    expect([offered.properties.text.minLength,offered.properties.text.maxLength]).toEqual([1,300]);
    expect([offered.properties.options.maxItems,offered.properties.options.uniqueItems,offered.properties.options.items.maxLength]).toEqual([4,true,60]);
    expect(offered.properties.subject_ref.type).toEqual(['string','null']);expect(offered.properties.subject_ref.pattern).toBe(canonical.properties.subject_ref.anyOf[1].pattern);
  });
  it('K4 H1: by default the tool offers exactly 1.0, says nothing of questions and refuses a 1.1 body before any request',async()=>{
    const {client,seen,cmd}=await guard(()=>({status:200,value:{}}));
    const tool=client.toolsForRun(cmd,{planOutcome:true}).find(item=>item.name==='nexloop.plan.outcome')! as unknown as {description:string;parameters:{properties:Record<string,any>};execute:(args:unknown)=>Promise<{isError?:boolean;content:{text:string}[]}>};
    expect(tool.parameters.properties.schema_version.const).toBe('1.0');expect(tool.parameters.properties.question).toBeUndefined();
    expect(tool.description).not.toMatch(/question|1\.1/);
    const refused=await tool.execute(asks);
    expect(refused.isError).toBe(true);expect(refused.content[0].text).toContain('invalid_run_outcome');expect(seen).toHaveLength(0);
  });
  it('keeps only the fixed codes of a superseded plan and of refusals, never upstream text',async()=>{
    const sentinel='SYNTHETIC_SECRET_SENTINEL';
    let {client,cmd}=await guard(()=>({status:409,value:{code:'plan_version_not_current'}}));
    await expect(client.recordOutcome(cmd,noAction)).rejects.toMatchObject({code:'plan_version_not_current'});
    ({client,cmd}=await guard(()=>({status:403,value:{code:'run_outcome_unavailable',debug:sentinel}})));
    try{await client.recordOutcome(cmd,noAction);throw new Error('expected refusal');}
    catch(error){expect((error as {code:string}).code).toBe('run_outcome_unavailable');expect(String(error)).not.toContain(sentinel);}
    ({client,cmd}=await guard(body=>({status:200,value:{run_id:(body.command as {run_id:string}).run_id,recorded:true,replay:false,kind:'escalate',plan_version:1,new_version:null}})));
    await expect(client.recordOutcome(cmd,noAction)).rejects.toMatchObject({code:'run_outcome_unavailable'});  // projected kind must match
  });
});
