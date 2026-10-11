/** NX-031 (dispatcher 2026-10-11): an unanswered guard is runtime_authorization_unavailable and is retried a bounded number of
 * times, each call keeping the 2 s limit (ADR-022 §4); only an explicit guard answer is runtime_authorization_denied. Actual
 * RuntimeHost guard client over TLS against a scripted guard. */
import {afterEach,describe,it,expect} from 'vitest';
import {createServer,type Server} from 'node:https';
import {once} from 'node:events';
import {execFileSync} from 'node:child_process';
import {mkdtempSync,writeFileSync,chmodSync,readFileSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';import {join} from 'node:path';
import {RuntimeHost,GUARD_ATTEMPTS,GUARD_TIMEOUT_MS} from '../src/runtime-host.js';
import {readPrivateMaterial} from '../src/private-material.js';
import {command} from './pi-runtime-support.ts';

type Step='hang'|'reset'|'503'|'403'|'400'|'ok'|'bad';
const resources:{server:Server;host:RuntimeHost;directory:string}[]=[];
afterEach(async()=>{for(const {server,host,directory} of resources.splice(0)){await host.close();server.closeAllConnections();await new Promise<void>(resolve=>server.close(()=>resolve()));rmSync(directory,{recursive:true,force:true});}});

async function scripted(steps:Step[]){
  const directory=mkdtempSync(join(tmpdir(),'nexloop-guard-retry-'));chmodSync(directory,0o700);
  execFileSync('openssl',['req','-x509','-newkey','rsa:2048','-nodes','-keyout',join(directory,'key'),'-out',join(directory,'cert'),'-days','1','-subj','/CN=127.0.0.1','-addext','subjectAltName=IP:127.0.0.1'],{stdio:'ignore'});
  const cmd=command();const calls:{at:number;operation:unknown}[]=[];
  const server=createServer({key:readFileSync(join(directory,'key')),cert:readFileSync(join(directory,'cert'))},(request,response)=>{
    const chunks:Buffer[]=[];request.on('data',chunk=>chunks.push(chunk));request.on('end',()=>{
      calls.push({at:Date.now(),operation:(JSON.parse(Buffer.concat(chunks).toString('utf8')) as {operation:unknown}).operation});
      const step=steps[Math.min(calls.length,steps.length)-1];
      if(step==='hang')return;
      if(step==='reset'){request.socket.destroy();return;}
      const status=step==='ok'||step==='bad'?200:Number(step);
      response.writeHead(status,{'Content-Type':'application/json'});
      response.end(JSON.stringify(step==='ok'?{authorized:true,run_id:cmd.run_id,ever_execution_authorized:true}:step==='bad'?{authorized:true,run_id:'other'}:{authorized:false}));
    });
  });server.listen(0,'127.0.0.1');await once(server,'listening');
  const port=(server.address() as {port:number}).port;
  const file=(name:string,value:string|Buffer)=>{const path=join(directory,name);writeFileSync(path,value,{mode:0o600});return path;};
  const runtime=join(directory,'runtime');execFileSync('mkdir',['-m','700',runtime]);
  const config=file('runtime-config',JSON.stringify({guard_ca_file:file('guard-ca',readFileSync(join(directory,'cert'))),guard_key_file:file('guard-key','a'.repeat(64)),
    guard_url:`https://127.0.0.1:${port}/internal/v1/runtime/authorize`,runtime_profile:'deterministic-test'}));
  const host=new RuntimeHost(runtime,config,readPrivateMaterial,()=>{});resources.push({server,host,directory});
  const authorize=(host as unknown as {authorizeAdmission:(c:typeof cmd,o:string,i?:string,r?:string)=>Promise<{ever_execution_authorized:boolean}>}).authorizeAdmission;
  return {calls,cmd,authorize:(operation='tool')=>authorize(cmd,operation,undefined,'activation_'+'a'.repeat(36))};
}

describe('Host guard authorization: unavailable is not denied',()=>{
  it('a timeout becomes unavailable and is retried; each attempt keeps the 2 s limit',async()=>{
    const {calls,authorize}=await scripted(['hang','ok']);
    const started=Date.now();
    await expect(authorize()).resolves.toEqual({ever_execution_authorized:true});
    expect(calls.length).toBe(2);
    expect(calls[1]!.at-calls[0]!.at).toBeGreaterThanOrEqual(GUARD_TIMEOUT_MS-50);
    expect(calls[1]!.at-calls[0]!.at).toBeLessThan(GUARD_TIMEOUT_MS+1500);
    expect(Date.now()-started).toBeLessThan(2*GUARD_TIMEOUT_MS+1500);
  },15000);
  it('a connection error and a 503 are retried and can succeed',async()=>{
    const {calls,authorize}=await scripted(['reset','503','ok']);
    await expect(authorize()).resolves.toEqual({ever_execution_authorized:true});
    expect(calls.length).toBe(3);
  },15000);
  it('retries are bounded: exhausted attempts fail as runtime_authorization_unavailable, never denied',async()=>{
    const {calls,authorize}=await scripted(['503']);
    await expect(authorize()).rejects.toMatchObject({code:'runtime_authorization_unavailable'});
    expect(calls.length).toBe(GUARD_ATTEMPTS);
    const hanging=await scripted(['hang']);
    await expect(hanging.authorize()).rejects.toMatchObject({code:'runtime_authorization_unavailable'});
    expect(hanging.calls.length).toBe(GUARD_ATTEMPTS);
  },30000);
  it('an explicit guard answer stays runtime_authorization_denied and is not retried',async()=>{
    for(const step of ['403','400','bad'] as const){
      const {calls,authorize}=await scripted([step,'ok']);
      await expect(authorize()).rejects.toMatchObject({code:'runtime_authorization_denied'});
      expect(calls.length).toBe(1);
    }
  },15000);
  it('no retry once the command time limit would pass',async()=>{
    const {calls,cmd,authorize}=await scripted(['503','ok']);
    cmd.not_after=new Date(Date.now()+50).toISOString();
    await expect(authorize()).rejects.toMatchObject({code:'runtime_authorization_unavailable'});
    expect(calls.length).toBe(1);
  },15000);
});
