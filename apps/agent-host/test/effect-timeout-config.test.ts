/** ADR-029b: the Host's per-request effect limit is configurable (effect_timeout_ms): absent = 2 s (ADR-022 §4), an integer
 * 1000..5000 otherwise, only with effect tools; anything else refuses the Host's start. The limit in use bounds each request and
 * is written, with the time spent, in the one diagnosis line of an unavailable effect request (numbers and codes only). */
import {afterEach,describe,it,expect,vi} from 'vitest';
import {createServer,type Server} from 'node:https';
import {once} from 'node:events';
import {execFileSync} from 'node:child_process';
import {mkdtempSync,readFileSync,rmSync,writeFileSync,chmodSync} from 'node:fs';
import {tmpdir} from 'node:os';import {join} from 'node:path';
import {RuntimeEffectClient,EFFECT_TIMEOUT_MS,EFFECT_TIMEOUT_MAX_MS,effectTimeoutMs} from '../src/runtime-effect-tools.js';
import {RuntimeHost} from '../dist/runtime-host.js';import {readPrivateMaterial} from '../dist/private-material.js';
import {command} from './pi-runtime-support.ts';

const roots:string[]=[];const hosts:RuntimeHost[]=[];
afterEach(async()=>{for(const h of hosts.splice(0))await h.close();for(const r of roots.splice(0))rmSync(r,{recursive:true});vi.restoreAllMocks();});
function open(extra:Record<string,unknown>={}){
 const root=mkdtempSync(join(tmpdir(),'nexloop-effect-timeout-'));chmodSync(root,0o700);roots.push(root);
 const file=(name:string,text:string)=>{const p=join(root,name);writeFileSync(p,text,{mode:0o600});return p;};
 const config=file('configuration',JSON.stringify({guard_ca_file:file('ca','synthetic-unused-ca'),guard_key_file:file('guard-key','a'.repeat(64)),
  guard_url:'https://127.0.0.1:65431/internal/v1/runtime/authorize',runtime_profile:'deterministic-test',effect_tools:true,...extra}));
 const h=new RuntimeHost(root,config,readPrivateMaterial,()=>{});hosts.push(h);return h;
}
type Step='hang'|'reset'|'503'|'409'|'403'|'ok'|'bad';
const resources:{server:Server;directory:string}[]=[];
afterEach(async()=>{for(const {server,directory} of resources.splice(0)){server.closeAllConnections();await new Promise<void>(resolve=>server.close(()=>resolve()));rmSync(directory,{recursive:true,force:true});}});
const intent='11111111-1111-4111-8111-111111111111';
const receipt={intent_id:intent,receipt_id:'22222222-2222-4222-8222-222222222222',state:'accepted',payload_digest:'a'.repeat(64),provider_payload_digest:'b'.repeat(64),scope:'effect_intent',business_action_success:false};

async function scripted(steps:Step[],extra:{effectTimeoutMs?:number}={}){
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
    privateMaterial:path=>path==='ca'?readFileSync(join(directory,'cert')):Buffer.from('c'.repeat(64)),activationForRun:()=>'activation_'+'A'.repeat(24),...extra});
  return {client,cmd,bodies};
}


describe('effect limit configuration',()=>{
  it.each([{effect_timeout_ms:999},{effect_timeout_ms:5001},{effect_timeout_ms:2500.5},{effect_timeout_ms:'3000'},{effect_timeout_ms:null},
    {effect_timeout_ms:3000,effect_tools:false}])('refuses to start with %j',extra=>{
    expect(()=>open(extra)).toThrow('runtime configuration refused');
  });
  it.each([1000,3000,5000])('starts with %i',value=>{expect(()=>open({effect_timeout_ms:value})).not.toThrow();});
  it('default 2 s, bounds 1 s and 5 s',()=>{
    expect([EFFECT_TIMEOUT_MS,EFFECT_TIMEOUT_MAX_MS,effectTimeoutMs(undefined),effectTimeoutMs(4000)]).toEqual([2000,5000,2000,4000]);
    expect(()=>effectTimeoutMs(5001)).toThrow();expect(()=>new RuntimeEffectClient({guardURL:new URL('https://127.0.0.1:1234/internal/v1/runtime/authorize'),
      keyPath:'k',caPath:'c',privateMaterial:()=>Buffer.from(''),activationForRun:()=>undefined,effectTimeoutMs:6000})).toThrow();
  });
});

describe('effect limit in use',()=>{
  it.each([[undefined,2000],[1000,1000],[3000,3000]] as const)('configured %s: each request bounded by %i ms, written in the diagnosis',async(configured,limit)=>{
    const lines:string[]=[];vi.spyOn(process.stderr,'write').mockImplementation(chunk=>{lines.push(String(chunk));return true;});
    const {client,cmd,bodies}=await scripted(['hang'],configured===undefined?{}:{effectTimeoutMs:configured});
    expect(client.timeoutMs).toBe(limit);
    const began=Date.now();
    await expect(client.call(cmd,'submit',{message:'one governed request'})).rejects.toMatchObject({code:'runtime_effect_unavailable'});
    const elapsed=Date.now()-began;
    expect(bodies.length).toBe(2);expect(elapsed).toBeGreaterThanOrEqual(2*limit-50);expect(elapsed).toBeLessThan(2*limit+1500);
    const diagnosis=lines.map(line=>JSON.parse(line)).filter(row=>row.event==='runtime_effect_unavailable');
    expect(diagnosis).toEqual([{event:'runtime_effect_unavailable',run_id:cmd.run_id,operation:'submit',effect_timeout_ms:limit,elapsed_ms:expect.any(Number)}]);
    expect(JSON.stringify(diagnosis)).not.toContain('one governed request');
  },20000);
});
