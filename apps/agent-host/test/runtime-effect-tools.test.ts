/** Actual TLS transport and Tool registration boundaries only, no EIOS claim. */
import {createServer,type Server} from 'node:https';
import {mkdtempSync,readFileSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {execFileSync} from 'node:child_process';
import {once} from 'node:events';
import {afterEach,describe,expect,it} from 'vitest';
import {RuntimeEffectClient} from '../src/runtime-effect-tools.ts';
import {command} from './pi-runtime-support.ts';

const resources:{server:Server;directory:string}[]=[];
const intent='11111111-1111-4111-8111-111111111111';
const receipt={intent_id:intent,receipt_id:'22222222-2222-4222-8222-222222222222',state:'accepted',payload_digest:'a'.repeat(64),provider_payload_digest:'b'.repeat(64),scope:'effect_intent',business_action_success:false};
async function bridge(options:{status?:number;change?:(value:Record<string,unknown>)=>unknown;silent?:boolean}={}){
  const directory=mkdtempSync(join(tmpdir(),'nexloop-effect-tls-'));
  execFileSync('openssl',['req','-x509','-newkey','rsa:2048','-nodes','-keyout',join(directory,'key'),'out',join(directory,'cert')].map(value=>value==='out'?'-out':value).concat(['-days','1','-subj','/CN=127.0.0.1','-addext','subjectAltName=IP:127.0.0.1']),{stdio:'ignore'});
  const bodies:Record<string,unknown>[]=[];const cmd=command();let active='activation_'+'A'.repeat(24);
  const server=createServer({key:readFileSync(join(directory,'key')),cert:readFileSync(join(directory,'cert'))},(request,response)=>{
    const chunks:Buffer[]=[];request.on('data',chunk=>chunks.push(chunk));request.on('end',()=>{
      bodies.push(JSON.parse(Buffer.concat(chunks).toString('utf8')) as Record<string,unknown>);
      if(options.silent)return;
      const value=options.change?options.change({run_id:cmd.run_id,receipt}):{run_id:cmd.run_id,receipt};
      response.writeHead(options.status??200,{'Content-Type':'application/json'});response.end(JSON.stringify(value));
    });
  });server.listen(0,'127.0.0.1');await once(server,'listening');resources.push({server,directory});
  const port=(server.address() as {port:number}).port;
  const client=new RuntimeEffectClient({guardURL:new URL(`https://127.0.0.1:${port}/internal/v1/runtime/authorize`),keyPath:'transport-key',caPath:'ca',
    privateMaterial:path=>path==='ca'?readFileSync(join(directory,'cert')):Buffer.from('c'.repeat(64)),activationForRun:()=>active});
  return {client,cmd,bodies,setActivation:(value:string)=>{active=value;}};
}
afterEach(async()=>{for(const {server,directory} of resources.splice(0)){server.closeAllConnections();await new Promise<void>(resolve=>server.close(()=>resolve()));rmSync(directory,{recursive:true,force:true});}});

describe('fixed HTTPS Run-bound effect transport, no authorization stub',()=>{
  it('sends only opaque activation, full immutable command and parameters; refreshes activation on replay',async()=>{
    const {client,cmd,bodies,setActivation}=await bridge();
    expect(await client.call(cmd,'submit',{message:'one service'})).toEqual(receipt);
    setActivation('activation_'+'B'.repeat(24));expect(await client.call(cmd,'find',{intent_id:intent})).toEqual(receipt);
    expect(Object.keys(bodies[0]!).sort()).toEqual(['activation_ref','command','parameters']);
    expect(bodies[0]!.command).toEqual(cmd);expect(bodies[0]!.parameters).toEqual({message:'one service'});
    expect(bodies[1]!.activation_ref).toBe('activation_'+'B'.repeat(24));
    const tools=client.toolsForRun(cmd);expect(tools.map(tool=>tool.name)).toEqual(['nexloop.service.request','nexloop.service.find']);
    expect(tools.every(tool=>tool.replay==='safe')).toBe(true);
  });
  it.each(['run','secret','success','digest'] as const)('refuses malformed %s receipt without exposing upstream data',async kind=>{
    const sentinel='SYNTHETIC_SECRET_SENTINEL';
    const {client,cmd}=await bridge({change:body=>kind==='run'?{...body,run_id:'forged'}:{...body,receipt:{...receipt,
      ...(kind==='secret'?{secret:sentinel}:kind==='success'?{business_action_success:true}:{payload_digest:sentinel})}}});
    try{await client.call(cmd,'submit',{message:'service'});throw new Error('expected refusal');}
    catch(error){expect((error as Error).message).toBe('runtime_effect_unavailable');expect(String(error)).not.toContain(sentinel);}
  });
  it('preserves only fixed409 payload conflict, never raw diagnostic fields',async()=>{
    const {client,cmd}=await bridge({status:409,change:()=>({code:'intent_payload_conflict',debug:'SYNTHETIC_SECRET_SENTINEL'})});
    await expect(client.call(cmd,'submit',{message:'service'})).rejects.toMatchObject({code:'intent_payload_conflict'});
  });
  it('refuses invalid parameters and absent current activation before network',async()=>{
    const {client,cmd,bodies,setActivation}=await bridge();
    await expect(client.call(cmd,'submit',{message:'service',tenant_id:cmd.tenant_id})).rejects.toMatchObject({code:'runtime_effect_unavailable'});
    setActivation('');await expect(client.call(cmd,'find',{intent_id:intent})).rejects.toMatchObject({code:'runtime_effect_unavailable'});
    expect(bodies).toHaveLength(0);
  });
  it('refuses redirects and bounds a silent actualTLSresponse',async()=>{
    const first=await bridge({status:302});await expect(first.client.call(first.cmd,'submit',{message:'service'})).rejects.toMatchObject({code:'runtime_effect_unavailable'});
    expect(first.bodies).toHaveLength(1);
    const silent=await bridge({silent:true}),start=Date.now();
    await expect(silent.client.call(silent.cmd,'submit',{message:'service'})).rejects.toMatchObject({code:'runtime_effect_unavailable'});
    expect(Date.now()-start).toBeLessThan(2600);
  });
});
