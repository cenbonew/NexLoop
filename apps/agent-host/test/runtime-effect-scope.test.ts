/** Actual TLS transport and Tool registration boundaries only, no EIOS claim. */
import {createServer,type Server} from 'node:https';
import {mkdtempSync,readFileSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {execFileSync} from 'node:child_process';
import {once} from 'node:events';
import {afterEach,describe,expect,it} from 'vitest';
import {RuntimeEffectClient} from '../dist/runtime-effect-tools.js';
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


const allowedScope={service_code:'local.json-export',deliverable:'固定私有目录内可核验的 JSON 文本导出文件',price_amount:'0',currency:'CNY',guarantees:[],discounts:[],limitations:['不提供目录外保证或折扣','不代表第三方渠道送达、付款或问题解决'],evidence_kind:'fsynced_json_export'};
const scope={offering_id:'d'.repeat(64),offering_revision:1,requested_guarantees:[],requested_discounts:[]};
describe('compiled typed request scope actual TLS transport, not PG authority',()=>{
 it('retains formal message and sends scope only as separate top-level envelope',async()=>{
  const {client,cmd,bodies}=await bridge();expect(await client.call(cmd,'submit',{message:'formal statement',request_scope:scope})).toEqual(receipt);
  expect(Object.keys(bodies[0]!).sort()).toEqual(['activation_ref','command','parameters','request_scope']);expect(bodies[0]!.parameters).toEqual({message:'formal statement'});expect(bodies[0]!.request_scope).toEqual(scope);
  const tools=client.toolsForRun(cmd);expect(tools[0]!.name).toBe('nexloop.service.request');expect((tools[0]!.parameters as any).properties.request_scope.additionalProperties).toBe(false);
 });
 it.each(['unsafe','fraction','zero','id','extra','missing','long','empty','duplicate','count','wrongtype','nul','surrogate'])('rejects malformed %s scope before actual network',async kind=>{
  const {client,cmd,bodies}=await bridge();const s:any=structuredClone(scope);
  if(kind==='unsafe')s.offering_revision=Number.MAX_SAFE_INTEGER+1;else if(kind==='fraction')s.offering_revision=1.2;else if(kind==='zero')s.offering_revision=0;else if(kind==='id')s.offering_id='a'.repeat(63);else if(kind==='extra')s.allow=true;else if(kind==='missing')delete s.requested_discounts;else if(kind==='long')s.requested_guarantees=['x'.repeat(129)];else if(kind==='empty')s.requested_guarantees=[''];else if(kind==='duplicate')s.requested_discounts=['same','same'];else if(kind==='count')s.requested_discounts=Array.from({length:17},(_,i)=>String(i));else if(kind==='wrongtype')s.requested_discounts=[true];else if(kind==='nul')s.requested_discounts=['\0'];else s.requested_discounts=['\ud800'];
  await expect(client.call(cmd,'submit',{message:'statement',request_scope:s})).rejects.toMatchObject({code:'runtime_effect_unavailable'});expect(bodies).toHaveLength(0);
 });
 it('find cannot carry a request scope',async()=>{const {client,cmd,bodies}=await bridge();await expect(client.call(cmd,'find',{intent_id:intent,request_scope:scope})).rejects.toMatchObject({code:'runtime_effect_unavailable'});expect(bodies).toHaveLength(0);});
 it('unknown catalog terms only receive a rejection, never a fulfillment receipt',async()=>{
  const {client,cmd,bodies}=await bridge({status:403,change:()=>({code:'outside_catalog_terms',scope:allowedScope})});
  await expect(client.call(cmd,'submit',{message:'statement',request_scope:{...scope,requested_guarantees:['guaranteed success']}})).rejects.toMatchObject({code:'outside_catalog_terms'});expect((bodies[0]!.request_scope as any).requested_guarantees).toEqual(['guaranteed success']);
 });
 it('registered tool returns only an error scope explanation and no receipt',async()=>{const {client,cmd}=await bridge({status:403,change:()=>({code:'outside_catalog_terms',scope:allowedScope})});const tool=client.toolsForRun(cmd)[0]!;const result=await (tool.execute as any)({message:'statement',request_scope:{...scope,requested_discounts:['half price']}});expect(result.isError).toBe(true);const payload=JSON.parse(result.content[0].text);expect(payload).toEqual({code:'outside_catalog_terms',scope:allowedScope});expect(payload.receipt).toBeUndefined();});
 it('refuses modified scope or added fulfilled/debug fields',async()=>{const {client,cmd}=await bridge({status:403,change:()=>({code:'outside_catalog_terms',scope:allowedScope,receipt:{...receipt,state:'fulfilled'},debug:'SYNTHETIC_SECRET_SENTINEL'})});await expect(client.call(cmd,'submit',{message:'statement',request_scope:scope})).rejects.toMatchObject({code:'runtime_effect_unavailable'});});
});
