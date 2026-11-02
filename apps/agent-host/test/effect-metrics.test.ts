/** NX-064 (ADR-029c): the Host counts its effect requests per operation (calls, ambiguous, unavailable, and every HTTP request's
 * elapsed time in the guard's histogram buckets) for the metrics endpoint; and the incentive tool keeps the configured effect limit
 * (effect_timeout_ms) and, after an ambiguous answer, sends the same body exactly once more. Numbers only, never a Run id or text. */
import {afterEach,describe,it,expect} from 'vitest';
import {createServer,type Server} from 'node:https';
import {once} from 'node:events';
import {execFileSync} from 'node:child_process';
import {mkdtempSync,readFileSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';import {join} from 'node:path';
import {RuntimeEffectClient} from '../src/runtime-effect-tools.js';
import {EFFECT_METRIC_OPERATIONS,effectMetrics,histogramBucket} from '../src/effect-metrics.js';
import {ENTERPRISE_ACTION} from '../src/context-input.js';
import {command} from './pi-runtime-support.ts';

type Step='hang'|'503'|'409'|'ok';
const resources:{server:Server;directory:string}[]=[];
afterEach(async()=>{for(const {server,directory} of resources.splice(0)){server.closeAllConnections();await new Promise<void>(resolve=>server.close(()=>resolve()));rmSync(directory,{recursive:true,force:true});}});
const intent='11111111-1111-4111-8111-111111111111';
const receipt={intent_id:intent,receipt_id:'22222222-2222-4222-8222-222222222222',state:'accepted',payload_digest:'a'.repeat(64),provider_payload_digest:'b'.repeat(64),scope:'effect_intent',business_action_success:false};
const offer={incentive_offering_id:'c'.repeat(64),offering_revision:1,binding_id:'d'.repeat(64),amount_minor:'300',currency:'CNY',notification_text:''};

async function scripted(steps:Step[],effectTimeoutMs=1000){
  const directory=mkdtempSync(join(tmpdir(),'nexloop-effect-metrics-'));
  execFileSync('openssl',['req','-x509','-newkey','rsa:2048','-nodes','-keyout',join(directory,'key'),'-out',join(directory,'cert'),'-days','1','-subj','/CN=127.0.0.1','-addext','subjectAltName=IP:127.0.0.1'],{stdio:'ignore'});
  const cmd=command();const bodies:string[]=[];const paths:string[]=[];
  const server=createServer({key:readFileSync(join(directory,'key')),cert:readFileSync(join(directory,'cert'))},(request,response)=>{
    const chunks:Buffer[]=[];request.on('data',chunk=>chunks.push(chunk));request.on('end',()=>{
      bodies.push(Buffer.concat(chunks).toString('utf8'));paths.push(request.url??'');
      const step=steps[Math.min(bodies.length,steps.length)-1];
      if(step==='hang')return;
      const incentive=(request.url??'').includes('/incentives/');
      const status={'503':503,'409':409,ok:200}[step];
      const value=step==='ok'?(incentive?{run_id:cmd.run_id,intent_id:'incentive-run:'+cmd.run_id+':'+offer.incentive_offering_id,state:'accepted',replayed:bodies.length>1,origin:'agent',business_action_success:false}:{run_id:cmd.run_id,receipt})
        :step==='409'?{code:incentive?'incentive_payload_conflict':'intent_payload_conflict'}:{code:'effect_intent_unavailable'};
      response.writeHead(status,{'Content-Type':'application/json'});response.end(JSON.stringify(value));
    });
  });server.listen(0,'127.0.0.1');await once(server,'listening');resources.push({server,directory});
  const port=(server.address() as {port:number}).port;
  const client=new RuntimeEffectClient({guardURL:new URL(`https://127.0.0.1:${port}/internal/v1/runtime/authorize`),keyPath:'transport-key',caPath:'ca',
    privateMaterial:path=>path==='ca'?readFileSync(join(directory,'cert')):Buffer.from('c'.repeat(64)),activationForRun:()=>'activation_'+'A'.repeat(24),effectTimeoutMs});
  return {client,cmd,bodies,paths};
}
/** The change of the cumulative counters of one operation across a call. */
async function delta(operation:string,act:()=>Promise<unknown>){
  const before=effectMetrics();
  await act().catch(()=>undefined);
  const after=effectMetrics();
  const out:Record<string,number>={};
  for(const [key,value] of Object.entries(after))if(key.startsWith(operation+'_')&&value!==(before[key]??0))out[key.slice(operation.length+1)]=value-(before[key]??0);
  return {out,total:{calls:after.effect_calls!-before.effect_calls!,ambiguous:after.effect_ambiguous!-before.effect_ambiguous!,unavailable:after.effect_unavailable!-before.effect_unavailable!}};
}
const buckets=(out:Record<string,number>)=>Object.entries(out).filter(([key])=>key.startsWith('h_')).reduce((sum,[,value])=>sum+value,0);

describe('histogram buckets',()=>{
  it('match the guard buckets (observability.histogram_bucket)',()=>{
    // Python: histogram_bucket(0.5)=0, (1)=0, (1.05)=1, (2)=8, (100)=49, (1102)=74, (2000)=80, (10**9)=116
    expect([0.5,1,1.05,2,100,1102,2000,1e9].map(histogramBucket)).toEqual([0,0,1,8,49,74,80,116]);
  });
});

describe('effect request counters',()=>{
  it('every operation is always present, with numbers only',()=>{
    const value=effectMetrics();
    for(const operation of EFFECT_METRIC_OPERATIONS)for(const key of ['calls','ambiguous','unavailable'])expect(typeof value[`${operation}_${key}`]).toBe('number');
    expect(Object.values(value).every(v=>Number.isSafeInteger(v)&&v>=0)).toBe(true);
    expect(Object.keys(value).every(key=>/^[a-z_]+(_h_\d{1,3})?$/.test(key))).toBe(true);
  });
  it('an answered submit: one call, one request in a bucket',async()=>{
    const {client,cmd}=await scripted(['ok']);
    const {out,total}=await delta('effect_submit',()=>client.call(cmd,'submit',{message:'one governed request'}));
    expect({calls:out.calls,ambiguous:out.ambiguous,unavailable:out.unavailable,requests:buckets(out)}).toEqual({calls:1,ambiguous:undefined,unavailable:undefined,requests:1});
    expect(total).toEqual({calls:1,ambiguous:0,unavailable:0});
  });
  it('an ambiguous first request answered by the second: ambiguous, not unavailable, two requests',async()=>{
    const {client,cmd,bodies}=await scripted(['hang','ok']);
    const {out}=await delta('effect_submit',()=>client.call(cmd,'submit',{message:'one governed request'}));
    expect([out.calls,out.ambiguous,out.unavailable,buckets(out)]).toEqual([1,1,undefined,2]);
    expect(bodies[0]).toBe(bodies[1]);
    // The timed-out request is in the bucket of the limit (1000 ms ~ bucket 73).
    expect(Object.keys(out).some(key=>/^h_7[2-5]$/.test(key))).toBe(true);
  },10000);
  it('still ambiguous: unavailable, counted under its own operation',async()=>{
    const {client,cmd}=await scripted(['hang','hang']);
    const {out}=await delta('enterprise_submit',()=>client.call(cmd,'submit',{summary:'one order'},ENTERPRISE_ACTION));
    expect([out.calls,out.ambiguous,out.unavailable,buckets(out)]).toEqual([1,1,1,2]);
  },10000);
  it('find and a conflict: a conflict is neither ambiguous nor unavailable',async()=>{
    const {client,cmd}=await scripted(['409']);
    const {out}=await delta('effect_submit',()=>client.call(cmd,'submit',{message:'other'}));
    expect([out.calls,out.ambiguous,out.unavailable]).toEqual([1,undefined,undefined]);
    const found=await scripted(['ok']);
    expect((await delta('effect_find',()=>found.client.call(found.cmd,'find',{intent_id:intent}))).out.calls).toBe(1);
  });
});

describe('incentive tool: configured limit and one more request with the same body',()=>{
  it('uses effect_timeout_ms, sends the same body once more after a timeout, and is answered',async()=>{
    const {client,cmd,bodies,paths}=await scripted(['hang','ok'],1000);
    const began=Date.now();
    const {out}=await delta('incentive',async()=>{const result=await client.issueIncentive(cmd,offer);expect(result.replayed).toBe(true);});
    const elapsed=Date.now()-began;
    expect(bodies.length).toBe(2);expect(bodies[0]).toBe(bodies[1]);expect(paths).toEqual(['/internal/v1/runtime/incentives/issue','/internal/v1/runtime/incentives/issue']);
    expect(elapsed).toBeGreaterThanOrEqual(950);expect(elapsed).toBeLessThan(1900);  // the configured 1 s, not the former 2 s
    expect([out.calls,out.ambiguous,out.unavailable]).toEqual([1,1,undefined]);
  },10000);
  it('still ambiguous after the second request: incentive_unavailable, no third request',async()=>{
    const {client,cmd,bodies}=await scripted(['hang'],1000);
    let code='';
    const {out}=await delta('incentive',()=>client.issueIncentive(cmd,offer).catch(error=>{code=error.code;throw error;}));
    expect(code).toBe('incentive_unavailable');
    expect(bodies.length).toBe(2);expect(bodies[0]).toBe(bodies[1]);
    expect([out.calls,out.ambiguous,out.unavailable]).toEqual([1,1,1]);
  },15000);
  it('a 503 is ambiguous too; a conflict is final at once',async()=>{
    const retried=await scripted(['503','ok']);
    await expect(retried.client.issueIncentive(retried.cmd,offer)).resolves.toMatchObject({state:'accepted'});
    expect(retried.bodies.length).toBe(2);
    const conflict=await scripted(['409','ok']);
    await expect(conflict.client.issueIncentive(conflict.cmd,offer)).rejects.toMatchObject({code:'incentive_payload_conflict'});
    expect(conflict.bodies.length).toBe(1);
  });
  it('the default limit is 2 s',async()=>{
    const {client}=await scripted(['ok'],2000);
    expect(client.timeoutMs).toBe(2000);
  });
});
