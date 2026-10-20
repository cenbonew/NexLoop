/** NX-041 S6b: in a simulation Host the effect tools record stand-in intents through the guard's stand-in route and answer with a
 * simulated outcome (never a fact); a real Host's tools are unchanged and never reach the stand-in route. Actual TLS, fake guard. */
import {createServer,type Server} from 'node:https';
import {mkdtempSync,readFileSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {execFileSync} from 'node:child_process';
import {once} from 'node:events';
import {randomUUID} from 'node:crypto';
import {afterEach,describe,expect,it} from 'vitest';
import {RuntimeEffectClient} from '../src/runtime-effect-tools.ts';
import {command} from './pi-runtime-support.ts';

const resources:{server:Server;directory:string}[]=[];
afterEach(async()=>{for(const {server,directory} of resources.splice(0)){server.closeAllConnections();await new Promise<void>(resolve=>server.close(()=>resolve()));rmSync(directory,{recursive:true,force:true});}});
const SIM='sim:'+randomUUID();
const intent='33333333-3333-4333-8333-333333333333';
type Tool={name:string;description:string;execute:(args:unknown)=>Promise<{isError?:boolean;content:{text:string}[]}>};

async function guard(simulation:boolean|undefined,answer?:(path:string,body:Record<string,unknown>)=>[number,unknown]){
  const directory=mkdtempSync(join(tmpdir(),'nexloop-sim-tools-'));
  execFileSync('openssl',['req','-x509','-newkey','rsa:2048','-nodes','-keyout',join(directory,'key'),'-out',join(directory,'cert'),'-days','1','-subj','/CN=127.0.0.1','-addext','subjectAltName=IP:127.0.0.1'],{stdio:'ignore'});
  const calls:{path:string;body:Record<string,unknown>}[]=[];
  const cmd=command();if(simulation){cmd.world_id=SIM;cmd.mode='simulation';}
  const receipt={intent_id:intent,world:cmd.world_id,state:'accepted',stand_in_id:'service.request.http',stand_in_version:'1.0.0',simulated:true,output_kind:'simulated_output'};
  const server=createServer({key:readFileSync(join(directory,'key')),cert:readFileSync(join(directory,'cert'))},(request,response)=>{
    const chunks:Buffer[]=[];request.on('data',chunk=>chunks.push(chunk));request.on('end',()=>{
      const body=JSON.parse(Buffer.concat(chunks).toString('utf8')) as Record<string,unknown>;calls.push({path:request.url!,body});
      const [status,value]=answer?answer(request.url!,body):[200,{run_id:cmd.run_id,receipt}];
      response.writeHead(status,{'Content-Type':'application/json'});response.end(JSON.stringify(value));
    });
  });server.listen(0,'127.0.0.1');await once(server,'listening');resources.push({server,directory});
  const port=(server.address() as {port:number}).port;
  const client=new RuntimeEffectClient({guardURL:new URL(`https://127.0.0.1:${port}/internal/v1/runtime/authorize`),keyPath:'k',caPath:'ca',
    privateMaterial:path=>path==='ca'?readFileSync(join(directory,'cert')):Buffer.from('c'.repeat(64)),activationForRun:()=>'activation_'+'A'.repeat(24),
    ...(simulation===undefined?{}:{simulation})});
  return {client,cmd,calls,receipt};
}
const tool=(tools:readonly unknown[],name:string)=>tools.find(t=>(t as Tool).name===name) as Tool;

describe('NX-041 S6b simulation Host tools',()=>{
  it('the reply, incentive and outreach tools only record stand-in intents and answer as simulated output',async()=>{
    const {client,cmd,calls}=await guard(true);
    const tools=client.toolsForRun(cmd,{incentive:true,outreach:true,planOutcome:true,enterprise:{action:'nexloop.enterprise.deliver',offering_id:'o',offering_revision:1,service_code:'s',title:'t',price_amount:'1'}});
    const names=tools.map(t=>(t as Tool).name);
    expect(names).toEqual(['nexloop.plan.outcome','nexloop.incentive.issue','nexloop.outreach.send','nexloop.service.request','nexloop.service.find']);  // no enterprise delivery (no stand-in)
    expect(names.filter(n=>n!=='nexloop.plan.outcome').every(n=>/^Simulation world:/.test(tool(tools,n).description))).toBe(true);
    const offer={incentive_offering_id:'d'.repeat(64),offering_revision:1,binding_id:'e'.repeat(64),amount_minor:'200',currency:'CNY',notification_text:''};
    for(const [name,args,action] of [['nexloop.service.request',{message:'您好'},'nexloop.service.request'],['nexloop.incentive.issue',offer,'nexloop.incentive.issue'],
      ['nexloop.outreach.send',{purpose:'commitment_followup',body:'明天见'},'nexloop.outreach.send']] as const){
      const out=await tool(tools,name).execute(args);
      expect(out.isError).toBeUndefined();
      const shown=JSON.parse(out.content[0]!.text);
      expect(shown).toMatchObject({simulated:true,output_kind:'simulated_output',request_id:intent,status:'accepted',stand_in_version:'1.0.0'});
      expect(shown.note).toMatch(/^Simulated: .*not a fact/);
      expect(Object.keys(shown).sort()).toEqual(['note','output_kind','request_id','simulated','stand_in_version','status']);
      const last=calls.at(-1)!;
      expect(last.path).toBe('/internal/v1/runtime/stand-ins/submit');
      expect(Object.keys(last.body).sort()).toEqual(['action','activation_ref','command','parameters']);
      expect(last.body.action).toBe(action);expect(last.body.parameters).toEqual(args);expect('sim_clock' in (last.body.parameters as object)).toBe(false);
    }
    const found=JSON.parse((await tool(tools,'nexloop.service.find').execute({intent_id:intent.toUpperCase()})).content[0]!.text);
    expect(found.simulated).toBe(true);expect(calls.at(-1)!.path).toBe('/internal/v1/runtime/stand-ins/find');expect(calls.at(-1)!.body.intent_id).toBe(intent);
    expect(calls.every(c=>c.path.startsWith('/internal/v1/runtime/stand-ins/'))).toBe(true);  // never the real effect, incentive or outreach route
  });
  it('a receipt not marked simulated, of another world or Run, or with extra fields is refused; fixed codes pass through',async()=>{
    for(const change of [{simulated:false},{output_kind:'fact'},{world:'real'},{extra:'SYNTHETIC_SECRET_SENTINEL'}]){
      const h=await guard(true,(_path,body)=>[200,{run_id:(body.command as {run_id:string}).run_id,receipt:{intent_id:intent,world:SIM,state:'accepted',stand_in_id:'s',stand_in_version:'1.0.0',simulated:true,output_kind:'simulated_output',...change}}]);
      const out=await tool(h.client.toolsForRun(h.cmd),'nexloop.service.request').execute({message:'x'}).catch(error=>error);
      expect(String((out as Error).message)).toBe('runtime_effect_unavailable');expect(String(out)).not.toContain('SYNTHETIC_SECRET_SENTINEL');
    }
    const conflict=await guard(true,()=>[409,{code:'intent_payload_conflict'}]);
    const out=await tool(conflict.client.toolsForRun(conflict.cmd),'nexloop.service.request').execute({message:'x'});
    expect(out.isError).toBe(true);expect(JSON.parse(out.content[0]!.text)).toEqual({code:'intent_payload_conflict',simulated:true});
  });
  it('a real Host (the default) keeps its tools exactly and never calls the stand-in route',async()=>{
    for(const simulation of [undefined,false]){
      const real=await guard(simulation,(_path,body)=>[200,{run_id:(body.command as {run_id:string}).run_id,receipt:{intent_id:intent,receipt_id:'22222222-2222-4222-8222-222222222222',state:'accepted',
        payload_digest:'a'.repeat(64),provider_payload_digest:'b'.repeat(64),scope:'effect_intent',business_action_success:false}}]);
      const tools=real.client.toolsForRun(real.cmd);
      expect(tools.map(t=>(t as Tool).name)).toEqual(['nexloop.service.request','nexloop.service.find']);
      expect(tool(tools,'nexloop.service.request').description).toMatch(/^Send your reply to the customer/);
      await tool(tools,'nexloop.service.request').execute({message:'one service'});
      expect(real.calls.map(c=>c.path)).toEqual(['/internal/v1/runtime/effects/submit']);
      await expect(real.client.standIn(real.cmd,{action:'nexloop.service.request',parameters:{message:'x'}})).rejects.toMatchObject({code:'runtime_effect_unavailable'});
      expect(real.calls).toHaveLength(1);
    }
  });
});
