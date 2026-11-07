/** ADR-045 (stage only): the test-Consumer model fault. The guard's admission answer carries synthetic_model_fault (decided in SQL
 * from the Consumer's governed persona). The Host takes it only as true and only on a start or resume; a Host without the stage key
 * refuses the admission (the Run does not start); the stage key is true only and never on a simulation Host. With the fault, the
 * Run's model call fails before it is registered: no guard 'model' request, no provider call, and the Run fails with
 * synthetic_model_fault (the S6 attribution reads that failure, tests/test_stage_capabilities.py). */
import {afterEach,describe,it,expect} from 'vitest';
import {createServer,type Server} from 'node:https';
import {once} from 'node:events';
import {execFileSync} from 'node:child_process';
import {mkdtempSync,writeFileSync,chmodSync,readFileSync,rmSync} from 'node:fs';
import {mkdtemp,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';import {join} from 'node:path';
import {fauxAssistantMessage} from '../../../vendor/pi/packages/ai/dist/index.js';
import {RuntimeHost} from '../src/runtime-host.js';
import {PiRuntimeAdapter} from '../src/pi-runtime-adapter.ts';
import {readPrivateMaterial} from '../src/private-material.js';
import {command,deterministic,until} from './pi-runtime-support.ts';

const resources:(()=>Promise<void>|void)[]=[];
afterEach(async()=>{for(const step of resources.splice(0).reverse())await step();});

async function hostWith(answer:(operation:unknown)=>Record<string,unknown>,extra:Record<string,unknown>={}){
  const directory=mkdtempSync(join(tmpdir(),'nexloop-synthetic-fault-'));chmodSync(directory,0o700);
  execFileSync('openssl',['req','-x509','-newkey','rsa:2048','-nodes','-keyout',join(directory,'key'),'-out',join(directory,'cert'),'-days','1','-subj','/CN=127.0.0.1','-addext','subjectAltName=IP:127.0.0.1'],{stdio:'ignore'});
  const cmd={...command(),runtime_profile:'deterministic-test'};const operations:unknown[]=[];
  const server:Server=createServer({key:readFileSync(join(directory,'key')),cert:readFileSync(join(directory,'cert'))},(request,response)=>{
    const chunks:Buffer[]=[];request.on('data',chunk=>chunks.push(chunk));request.on('end',()=>{
      const operation=(JSON.parse(Buffer.concat(chunks).toString('utf8')) as {operation:unknown}).operation;operations.push(operation);
      response.writeHead(200,{'Content-Type':'application/json'});
      response.end(JSON.stringify({authorized:true,run_id:cmd.run_id,ever_execution_authorized:true,...answer(operation)}));
    });
  });server.listen(0,'127.0.0.1');await once(server,'listening');
  const port=(server.address() as {port:number}).port;
  const file=(name:string,value:string|Buffer)=>{const path=join(directory,name);writeFileSync(path,value,{mode:0o600});return path;};
  const runtime=join(directory,'runtime');execFileSync('mkdir',['-m','700',runtime]);
  const config=file('runtime-config',JSON.stringify({guard_ca_file:file('guard-ca',readFileSync(join(directory,'cert'))),guard_key_file:file('guard-key','a'.repeat(64)),
    guard_url:`https://127.0.0.1:${port}/internal/v1/runtime/authorize`,runtime_profile:'deterministic-test',...extra}));
  const make=()=>new RuntimeHost(runtime,config,readPrivateMaterial,()=>{});
  resources.push(async()=>{server.closeAllConnections();await new Promise<void>(resolve=>server.close(()=>resolve()));rmSync(directory,{recursive:true,force:true});});
  return {cmd,operations,make};
}

type Admission=(c:ReturnType<typeof command>,o:string,i?:string,r?:string)=>Promise<{ever_execution_authorized:boolean;synthetic_model_fault?:boolean}>;
const admission=(host:RuntimeHost)=>(host as unknown as {authorizeAdmission:Admission}).authorizeAdmission;
const ref='activation_'+'a'.repeat(36);

describe('ADR-045: the guard admission carries the fault',()=>{
  it('takes synthetic_model_fault only as true and only on a start or resume',async()=>{
    const ok=await hostWith(()=>({synthetic_model_fault:true}),{synthetic_model_fault:true});const host=ok.make();resources.push(()=>host.close());
    await expect(admission(host)(ok.cmd,'start','input',ref)).resolves.toEqual({ever_execution_authorized:true,synthetic_model_fault:true});
    await expect(admission(host)(ok.cmd,'resume','input',ref)).resolves.toEqual({ever_execution_authorized:true,synthetic_model_fault:true});
    await expect(admission(host)(ok.cmd,'tool',undefined,ref)).rejects.toMatchObject({code:'runtime_authorization_denied'});  // never on another operation
    const odd=await hostWith(()=>({synthetic_model_fault:'yes'}),{synthetic_model_fault:true});const other=odd.make();resources.push(()=>other.close());
    await expect(admission(other)(odd.cmd,'start','input',ref)).rejects.toMatchObject({code:'runtime_authorization_denied'});
    const plain=await hostWith(()=>({}));const quiet=plain.make();resources.push(()=>quiet.close());
    await expect(admission(quiet)(plain.cmd,'start','input',ref)).resolves.toEqual({ever_execution_authorized:true});  // unchanged without it
  },20000);
  it('the stage key is true only and never on a simulation Host',async()=>{
    for(const extra of [{synthetic_model_fault:'yes'},{synthetic_model_fault:false},{synthetic_model_fault:true,world_group:'simulation'}]){
      const setup=await hostWith(()=>({}),extra);
      expect(()=>setup.make()).toThrow('runtime configuration refused');
    }
  },20000);
  it('a Host without the stage key refuses a fault admission: the Run does not start',async()=>{
    const setup=await hostWith(()=>({synthetic_model_fault:true}));const host=setup.make();resources.push(()=>host.close());
    await expect(host.dispatch('start',{activation_ref:ref,command:setup.cmd,input:'customer message'})).rejects.toMatchObject({code:'invalid_runtime_request'});
    expect(setup.operations).toEqual(['start']);
  },20000);
});

describe('ADR-045: the faulted Run',()=>{
  it('fails before its model call is registered: no model authorization, no provider call, failure synthetic_model_fault',async()=>{
    const root=await mkdtemp(join(tmpdir(),'nexloop-synthetic-fault-run-'));resources.push(()=>rm(root,{recursive:true,force:true}));
    const provider=deterministic({});const operations:string[]=[];
    provider.faux.setResponses([fauxAssistantMessage('a reply the model never gets to send')]);
    const adapter=new PiRuntimeAdapter({root,models:provider.models,model:{provider:'faux',modelId:'faux-1'},tools:[],
      costPolicy:{kind:'deterministic_zero',currency:'USD'},assertOwner:()=>{},modelFault:()=>true,
      authorize:async(_command,operation)=>{operations.push(operation);return {ever_execution_authorized:false};}});
    resources.push(()=>adapter.close());
    const cmd=command();
    await adapter.start(cmd,'customer message');
    await until(async()=>(await adapter.inspect(cmd)).active_tasks===0,20000);
    const got=await adapter.inspect(cmd);
    expect(got.runtime_outcome).toBe('failed');
    expect([got.failure_code,got.failure_detail]).toEqual(['runtime_task_failed','synthetic_model_fault']);
    expect(operations.filter(operation=>operation==='model')).toEqual([]);
    expect(provider.faux.state.callCount).toBe(0);
  },30000);
  it('without the fault the same Run calls the model (control)',async()=>{
    const root=await mkdtemp(join(tmpdir(),'nexloop-synthetic-fault-control-'));resources.push(()=>rm(root,{recursive:true,force:true}));
    const provider=deterministic({});const operations:string[]=[];
    provider.faux.setResponses([fauxAssistantMessage('done')]);
    const adapter=new PiRuntimeAdapter({root,models:provider.models,model:{provider:'faux',modelId:'faux-1'},tools:[],
      costPolicy:{kind:'deterministic_zero',currency:'USD'},assertOwner:()=>{},modelFault:()=>false,
      authorize:async(_command,operation)=>{operations.push(operation);return {ever_execution_authorized:false};}});
    resources.push(()=>adapter.close());
    const cmd=command();
    await adapter.start(cmd,'customer message');
    await until(async()=>(await adapter.inspect(cmd)).active_tasks===0,20000);
    expect((await adapter.inspect(cmd)).runtime_outcome).not.toBe('failed');
    expect(operations).toContain('model');
  },30000);
});
