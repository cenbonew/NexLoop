/** ADR-028: after an ambiguous effect answer (timeout, connection error, 503) the Host sends exactly one more request with the
 * same body; a definite answer (409 conflict, 403, malformed 200) is never repeated; no repeat when the Run's time limit would
 * pass. Actual TLS transport against a scripted guard (no EIOS authority). */
import {afterEach,describe,it,expect} from 'vitest';
import {createServer,type Server} from 'node:https';
import {once} from 'node:events';
import {execFileSync} from 'node:child_process';
import {mkdtempSync,readFileSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';import {join} from 'node:path';
import {RuntimeEffectClient,EFFECT_TIMEOUT_MS} from '../src/runtime-effect-tools.js';
import {command} from './pi-runtime-support.ts';

type Step='hang'|'reset'|'503'|'409'|'403'|'ok'|'bad';
const resources:{server:Server;directory:string}[]=[];
afterEach(async()=>{for(const {server,directory} of resources.splice(0)){server.closeAllConnections();await new Promise<void>(resolve=>server.close(()=>resolve()));rmSync(directory,{recursive:true,force:true});}});
const intent='11111111-1111-4111-8111-111111111111';
const receipt={intent_id:intent,receipt_id:'22222222-2222-4222-8222-222222222222',state:'accepted',payload_digest:'a'.repeat(64),provider_payload_digest:'b'.repeat(64),scope:'effect_intent',business_action_success:false};

async function scripted(steps:Step[]){
  const directory=mkdtempSync(join(tmpdir(),'nexloop-effect-retry-'));
  execFileSync('openssl',['req','-x509','-newkey','rsa:2048','-nodes','-keyout',join(directory,'key'),'-out',join(directory,'cert'),'-days','1','-subj','/CN=127.0.0.1','-addext','subjectAltName=IP:127.0.0.1'],{stdio:'ignore'});
  const cmd=command();const bodies:string[]=[];
  const server=createServer({key:readFileSync(join(directory,'key')),cert:readFileSync(join(directory,'cert'))},(request,response)=>{
    const chunks:Buffer[]=[];request.on('data',chunk=>chunks.push(chunk));request.on('end',()=>{
      bodies.push(Buffer.concat(chunks).toString('utf8'));
      const step=steps[Math.min(bodies.length,steps.length)-1];
      if(step==='hang')return;
      if(step==='reset'){request.socket.destroy();return;}
      const status={'503':503,'409':409,'403':403,ok:200,bad:200}[step];
      const value=step==='ok'?{run_id:cmd.run_id,receipt}:step==='bad'?{run_id:'other',receipt}:step==='409'?{code:'intent_payload_conflict'}:step==='403'?{code:'effect_intent_unavailable'}:{code:'effect_intent_unavailable'};
      response.writeHead(status,{'Content-Type':'application/json'});response.end(JSON.stringify(value));
    });
  });server.listen(0,'127.0.0.1');await once(server,'listening');resources.push({server,directory});
  const port=(server.address() as {port:number}).port;
  const client=new RuntimeEffectClient({guardURL:new URL(`https://127.0.0.1:${port}/internal/v1/runtime/authorize`),keyPath:'transport-key',caPath:'ca',
    privateMaterial:path=>path==='ca'?readFileSync(join(directory,'cert')):Buffer.from('c'.repeat(64)),activationForRun:()=>'activation_'+'A'.repeat(24)});
  return {client,cmd,bodies};
}

describe('effect client: one same-body request after an ambiguous answer',()=>{
  it.each(['hang','reset','503'] as const)('%s then success: two requests with identical bodies',async first=>{
    const {client,cmd,bodies}=await scripted([first,'ok']);
    await expect(client.call(cmd,'submit',{message:'one governed request'})).resolves.toEqual(receipt);
    expect(bodies.length).toBe(2);expect(bodies[1]).toBe(bodies[0]);
  },15000);
  it('still ambiguous after the second request: unavailable, never a third',async()=>{
    const {client,cmd,bodies}=await scripted(['503']);
    await expect(client.call(cmd,'submit',{message:'one governed request'})).rejects.toMatchObject({code:'runtime_effect_unavailable'});
    expect(bodies.length).toBe(2);
  },15000);
  it('find is asked at most twice',async()=>{
    const {client,cmd,bodies}=await scripted(['hang','ok']);
    await expect(client.call(cmd,'find',{intent_id:intent})).resolves.toEqual(receipt);
    expect(bodies.length).toBe(2);
  },15000);
  it.each([['409','intent_payload_conflict'],['403','runtime_effect_unavailable'],['bad','runtime_effect_unavailable']] as const)('a definite %s answer is not repeated',async(first,code)=>{
    const {client,cmd,bodies}=await scripted([first,'ok']);
    await expect(client.call(cmd,'submit',{message:'one governed request'})).rejects.toMatchObject({code});
    expect(bodies.length).toBe(1);
  },15000);
  it('no second request when the Run time limit would pass',async()=>{
    const {client,cmd,bodies}=await scripted(['503','ok']);
    cmd.not_after=new Date(Date.now()+EFFECT_TIMEOUT_MS/2).toISOString();
    await expect(client.call(cmd,'submit',{message:'one governed request'})).rejects.toMatchObject({code:'runtime_effect_unavailable'});
    expect(bodies.length).toBe(1);
  },15000);
});
