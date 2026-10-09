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
