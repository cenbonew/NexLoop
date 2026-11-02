/** NX-064 (L6 condition 2): what the enterprise alert reads. A Run whose real nexloop.enterprise.deliver tool meets a guard that
 * never answers (the request and its one resend time out) fails with failure_code runtime_tool_error, failure_tool
 * 'nexloop.enterprise.deliver' and failure_detail 'runtime_effect_unavailable' in its inspection - the values the runtime dispatcher
 * copies into runtime.jobs.result and migration effect_request_metrics (0405) matches literally. tests/test_effect_request_metrics_pg.py
 * pins the same three literals on the Python and SQL side. */
import {mkdtemp,rm} from 'node:fs/promises';
import {mkdtempSync,readFileSync,rmSync} from 'node:fs';
import {createServer,type Server} from 'node:https';
import {once} from 'node:events';
import {execFileSync} from 'node:child_process';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {afterEach,describe,expect,it} from 'vitest';
import {Type,fauxAssistantMessage,fauxToolCall} from '../../../vendor/pi/packages/ai/dist/index.js';
import {defineTool} from '../../../vendor/pi/packages/durable/dist/index.js';
import {PiRuntimeAdapter} from '../src/pi-runtime-adapter.ts';
import {RuntimeEffectClient} from '../src/runtime-effect-tools.ts';
import {command,deterministic,until} from './pi-runtime-support.ts';

const cleanup:(()=>Promise<void>|void)[]=[];
afterEach(async()=>{for(const step of cleanup.splice(0).reverse())await step();});
const supply={action:'nexloop.enterprise.deliver:1',offering_id:'d'.repeat(64),offering_revision:1,binding_id:'e'.repeat(64),binding_revision:1,provenance:'eios:object:'+'d'.repeat(64),
  properties:{service_code:'enterprise.synthetic-delivery',title:'Synthetic enterprise offering',delivery_action:'nexloop.enterprise.deliver:1',content_kind:'enterprise-delivery',
    price_amount:'12.50',currency:'CNY',eligibility:'current_consumer_plan',allowed_guarantees:[],allowed_discounts:[],evidence_kind:'enterprise_receipt',active:true,valid_until:'2026-12-31T00:00:00Z'}};

async function silentGuard(){
  const directory=mkdtempSync(join(tmpdir(),'nexloop-silent-guard-'));
  execFileSync('openssl',['req','-x509','-newkey','rsa:2048','-nodes','-keyout',join(directory,'key'),'-out',join(directory,'cert'),'-days','1','-subj','/CN=127.0.0.1','-addext','subjectAltName=IP:127.0.0.1'],{stdio:'ignore'});
  let requests=0;
  const server:Server=createServer({key:readFileSync(join(directory,'key')),cert:readFileSync(join(directory,'cert'))},request=>{request.resume();request.on('end',()=>{requests+=1;});});
  server.listen(0,'127.0.0.1');await once(server,'listening');
  cleanup.push(async()=>{server.closeAllConnections();await new Promise<void>(resolve=>server.close(()=>resolve()));rmSync(directory,{recursive:true,force:true});});
  return {port:(server.address() as {port:number}).port,cert:join(directory,'cert'),requests:()=>requests};
}

describe('an enterprise delivery that stays unavailable',()=>{
  it('fails the Run with the tool name and runtime_effect_unavailable the alert matches',async()=>{
    const guard=await silentGuard();
    const client=new RuntimeEffectClient({guardURL:new URL(`https://127.0.0.1:${guard.port}/internal/v1/runtime/authorize`),keyPath:'k',caPath:'ca',
      privateMaterial:path=>path==='ca'?readFileSync(guard.cert):Buffer.from('c'.repeat(64)),activationForRun:()=>'activation_'+'A'.repeat(24),effectTimeoutMs:1000});
    const root=await mkdtemp(join(tmpdir(),'nexloop-enterprise-unavailable-'));cleanup.push(()=>rm(root,{recursive:true,force:true}));
    const provider=deterministic({});
    const adapter=new PiRuntimeAdapter({root,models:provider.models,model:{provider:'faux',modelId:'faux-1'},
      toolsForRun:cmd=>client.toolsForRun(cmd,{enterprise:supply as never,replyEndsRun:true}),
      costPolicy:{kind:'deterministic_zero',currency:'USD'},assertOwner:()=>{},authorize:async()=>({ever_execution_authorized:false})});
    cleanup.push(()=>adapter.close());
    provider.faux.setResponses([fauxAssistantMessage(fauxToolCall('nexloop.enterprise.deliver',{summary:'one synthetic order'},{id:'call-0'}),{stopReason:'toolUse'}),
      fauxAssistantMessage('done')]);
    const cmd=command();
    await adapter.start(cmd,'customer message');
    await until(async()=>(await adapter.inspect(cmd)).active_tasks===0,20000);
    const got=await adapter.inspect(cmd);
    expect(guard.requests()).toBe(2);  // the request and its one resend, same body
    expect(got.runtime_outcome).toBe('failed');
    expect([got.failure_code,got.failure_tool,got.failure_detail]).toEqual(['runtime_tool_error','nexloop.enterprise.deliver','runtime_effect_unavailable']);
  },30000);
  it('a thrown error without a runtime code is runtime_tool_unavailable (no text reaches the record)',async()=>{
    const root=await mkdtemp(join(tmpdir(),'nexloop-thrown-text-'));cleanup.push(()=>rm(root,{recursive:true,force:true}));
    const provider=deterministic({});
    const thrower=defineTool({name:'nexloop.probe',description:'probe',parameters:Type.Object({}),execute:async()=>{throw new Error('Synthetic failure with text');}});
    const adapter=new PiRuntimeAdapter({root,models:provider.models,model:{provider:'faux',modelId:'faux-1'},tools:[thrower] as never,
      costPolicy:{kind:'deterministic_zero',currency:'USD'},assertOwner:()=>{},authorize:async()=>({ever_execution_authorized:false})});
    cleanup.push(()=>adapter.close());
    provider.faux.setResponses([fauxAssistantMessage(fauxToolCall('nexloop.probe',{},{id:'call-0'}),{stopReason:'toolUse'}),fauxAssistantMessage('done')]);
    const cmd=command();await adapter.start(cmd,'customer message');
    await until(async()=>(await adapter.inspect(cmd)).active_tasks===0);
    const got=await adapter.inspect(cmd);
    expect([got.failure_code,got.failure_tool,got.failure_detail]).toEqual(['runtime_tool_error','nexloop.probe','runtime_tool_unavailable']);
  });
});
