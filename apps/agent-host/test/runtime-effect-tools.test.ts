/** Actual TLS transport and Tool registration boundaries only, no EIOS claim. */
import {createServer,type Server} from 'node:https';
import {mkdtempSync,readFileSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {execFileSync} from 'node:child_process';
import {once} from 'node:events';
import {afterEach,describe,expect,it} from 'vitest';
import {ENTERPRISE_RESULT_GUIDE,RuntimeEffectClient} from '../src/runtime-effect-tools.ts';
import {command} from './pi-runtime-support.ts';

const resources:{server:Server;directory:string}[]=[];
const intent='11111111-1111-4111-8111-111111111111';
const receipt={intent_id:intent,receipt_id:'22222222-2222-4222-8222-222222222222',state:'accepted',payload_digest:'a'.repeat(64),provider_payload_digest:'b'.repeat(64),scope:'effect_intent',business_action_success:false};
async function bridge(options:{status?:number|((call:number)=>number);change?:(value:Record<string,unknown>)=>unknown;silent?:boolean}={}){
  const directory=mkdtempSync(join(tmpdir(),'nexloop-effect-tls-'));
  execFileSync('openssl',['req','-x509','-newkey','rsa:2048','-nodes','-keyout',join(directory,'key'),'out',join(directory,'cert')].map(value=>value==='out'?'-out':value).concat(['-days','1','-subj','/CN=127.0.0.1','-addext','subjectAltName=IP:127.0.0.1']),{stdio:'ignore'});
  const bodies:Record<string,unknown>[]=[];const cmd=command();let active='activation_'+'A'.repeat(24);
  const server=createServer({key:readFileSync(join(directory,'key')),cert:readFileSync(join(directory,'cert'))},(request,response)=>{
    const chunks:Buffer[]=[];request.on('data',chunk=>chunks.push(chunk));request.on('end',()=>{
      bodies.push(JSON.parse(Buffer.concat(chunks).toString('utf8')) as Record<string,unknown>);
      if(options.silent)return;
      const status=typeof options.status==='function'?options.status(bodies.length):options.status??200;
      const value=status===409?{code:'intent_payload_conflict'}:options.change?options.change({run_id:cmd.run_id,receipt}):{run_id:cmd.run_id,receipt};
      response.writeHead(typeof options.status==='function'?options.status(bodies.length):options.status??200,{'Content-Type':'application/json'});response.end(JSON.stringify(value));
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
    // 10/16: the reply tool says it is how the customer gets the reply, and that plain text is never delivered.
    expect((tools[0] as unknown as {description:string}).description).toMatch(/^Send your reply to the customer.*never delivered/);
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
  it('a second, different reply in the same Run is answered as already sent, not as a tool error (10/17 stage smoke)',async()=>{
    const {client,cmd,bodies}=await bridge({status:call=>call===1?200:409});
    const tool=client.toolsForRun(cmd).find(item=>item.name==='nexloop.service.request') as unknown as {execute:(args:unknown)=>Promise<{isError?:boolean;content:{text:string}[]}>};
    const first=await tool.execute({message:'the reply'});
    expect(first.isError).toBeUndefined();expect(JSON.parse(first.content[0]!.text)).toEqual(receipt);
    const second=await tool.execute({message:'one more thing'});
    expect(second.isError).toBeUndefined();
    expect(JSON.parse(second.content[0]!.text)).toMatchObject({code:'reply_already_submitted',intent_id:intent});
    expect(bodies).toHaveLength(2);
    const description=(client.toolsForRun(cmd)[0] as unknown as {description:string}).description;
    expect(description).toMatch(/exactly once per Run/);
  });
  it('in a message Run the reply tool ends the Run with its receipt; find never does; other Runs keep their loop (10/18)',async()=>{
    type Run={execute:(args:unknown)=>Promise<{isError?:boolean;control?:{terminate?:true};content:{text:string}[]}>};
    const {client,cmd}=await bridge({status:call=>call<=2?200:409});
    const tools=client.toolsForRun(cmd,{replyEndsRun:true});
    const reply=tools.find(t=>t.name==='nexloop.service.request') as unknown as Run,find=tools.find(t=>t.name==='nexloop.service.find') as unknown as Run;
    expect((await reply.execute({message:'the reply'})).control).toEqual({terminate:true});
    expect((await find.execute({intent_id:intent})).control).toBeUndefined();
    const again=await reply.execute({message:'one more'});  // 409: already sent; still the end
    expect(JSON.parse(again.content[0]!.text).code).toBe('reply_already_submitted');expect(again.control).toEqual({terminate:true});
    const other=await bridge();
    const plain=other.client.toolsForRun(other.cmd).find(t=>t.name==='nexloop.service.request') as unknown as Run;
    expect((await plain.execute({message:'the reply'})).control).toBeUndefined();
  });
  it('refuses invalid parameters and absent current activation before network',async()=>{
    const {client,cmd,bodies,setActivation}=await bridge();
    await expect(client.call(cmd,'submit',{message:'service',tenant_id:cmd.tenant_id})).rejects.toMatchObject({code:'invalid_effect_request'});
    setActivation('');await expect(client.call(cmd,'find',{intent_id:intent})).rejects.toMatchObject({code:'runtime_effect_unavailable'});
    expect(bodies).toHaveLength(0);
  });
  it('refuses redirects and bounds a silent actualTLSresponse',async()=>{
    const first=await bridge({status:302});await expect(first.client.call(first.cmd,'submit',{message:'service'})).rejects.toMatchObject({code:'runtime_effect_unavailable'});
    expect(first.bodies).toHaveLength(1);
    const silent=await bridge({silent:true}),start=Date.now();
    await expect(silent.client.call(silent.cmd,'submit',{message:'service'})).rejects.toMatchObject({code:'runtime_effect_unavailable'});
    // ADR-028: a silent guard is ambiguous; exactly one more request with the same body, each bounded by 2 s.
    expect(silent.bodies).toHaveLength(2);expect(silent.bodies[1]).toEqual(silent.bodies[0]);
    expect(Date.now()-start).toBeLessThan(2*2000+600);
  },10000);
  it('AT-043: offers the incentive tool only when asked, sends the exact parameters and accepts only a fixed receipt',async()=>{
    const offer={incentive_offering_id:'d'.repeat(64),offering_revision:1,binding_id:'e'.repeat(64),amount_minor:'200',currency:'CNY',notification_text:''};
    const accepted=(body:Record<string,unknown>)=>({run_id:body.run_id,intent_id:'incentive-run:'+String(body.run_id)+':'+'d'.repeat(64),state:'accepted',replayed:false,origin:'plan',business_action_success:false});
    const {client,cmd,bodies}=await bridge({change:accepted});
    expect(client.toolsForRun(cmd).map(tool=>tool.name)).not.toContain('nexloop.incentive.issue');
    expect(client.toolsForRun(cmd,{incentive:true}).map(tool=>tool.name)).toContain('nexloop.incentive.issue');
    const out=await client.issueIncentive(cmd,offer);
    expect(out.state).toBe('accepted');expect(out.origin).toBe('plan');
    expect(Object.keys(bodies[0]!).sort()).toEqual(['activation_ref','command','parameters']);expect(bodies[0]!.parameters).toEqual(offer);
    // Malformed arguments never reach the network; a forged receipt is refused.
    await expect(client.issueIncentive(cmd,{...offer,amount_minor:'1.5'})).rejects.toMatchObject({code:'invalid_incentive_request'});
    expect(bodies).toHaveLength(1);
    const forged=await bridge({change:body=>({...accepted(body),business_action_success:true})});
    await expect(forged.client.issueIncentive(forged.cmd,offer)).rejects.toMatchObject({code:'incentive_unavailable'});
    const outside=await bridge({status:403,change:()=>({code:'outside_catalog_terms'})});
    await expect(outside.client.issueIncentive(outside.cmd,offer)).rejects.toMatchObject({code:'outside_catalog_terms'});
  });
});

describe('outreach slice 5: the proactive message tool',()=>{
  const ask={purpose:'commitment_followup',body:'您好，您预约的服务明天上午开始。'};
  const accepted=(body:Record<string,unknown>)=>({run_id:body.run_id,intent_id:'outreach:'+'f'.repeat(64),state:'accepted',replayed:false,business_action_success:false});
  it('is offered only to a plan reevaluation Run when asked, sends exactly purpose and body, accepts only a fixed receipt',async()=>{
    const {client,cmd,bodies}=await bridge({change:accepted});
    expect(client.toolsForRun(cmd).map(tool=>tool.name)).not.toContain('nexloop.outreach.send');
    expect(client.toolsForRun(cmd,{outreach:true}).map(tool=>tool.name)).not.toContain('nexloop.outreach.send');  // not without the plan outcome tool
    expect(client.toolsForRun(cmd,{outreach:true,planOutcome:true}).map(tool=>tool.name)).toContain('nexloop.outreach.send');
    const out=await client.sendOutreach(cmd,ask);
    expect(out.state).toBe('accepted');expect(out.intent_id).toBe('outreach:'+'f'.repeat(64));
    expect(Object.keys(bodies[0]!).sort()).toEqual(['activation_ref','command','parameters']);expect(bodies[0]!.parameters).toEqual(ask);
    // The model never chooses the consumer, the origin, the channel or the topic; malformed arguments never reach the network.
    for(const bad of [{...ask,consumer_id:'a'.repeat(64)},{...ask,origin_ref:'owner:x'},{...ask,channel:'sms'},{...ask,body:''},{...ask,purpose:'Bad Purpose'}])
      await expect(client.sendOutreach(cmd,bad)).rejects.toMatchObject({code:'invalid_outreach_request'});
    expect(bodies).toHaveLength(1);
    const forged=await bridge({change:body=>({...accepted(body),business_action_success:true})});
    await expect(forged.client.sendOutreach(forged.cmd,ask)).rejects.toMatchObject({code:'outreach_unavailable'});
    const unlisted=await bridge({status:403,change:()=>({code:'purpose_not_allowed'})});
    await expect(unlisted.client.sendOutreach(unlisted.cmd,ask)).rejects.toMatchObject({code:'purpose_not_allowed'});
  });
});

describe('ADR-030 enterprise delivery tool (NX-037 gap B)',()=>{
  const supply={action:'nexloop.enterprise.deliver:1' as const,offering_id:'d'.repeat(64),offering_revision:1,service_code:'enterprise.synthetic-delivery',title:'合成企业交付',price_amount:'12.50'};
  it('is offered only for a Run whose context names the enterprise offering',async()=>{
    const {client,cmd}=await bridge();
    expect(client.toolsForRun(cmd).map(tool=>tool.name)).toEqual(['nexloop.service.request','nexloop.service.find']);
    expect(client.toolsForRun(cmd,{enterprise:supply}).map(tool=>tool.name)).toEqual(['nexloop.enterprise.deliver','nexloop.enterprise.find','nexloop.service.request','nexloop.service.find']);
  });
  it('sends the Action and exactly its parameters, with this Run\'s own consumer',async()=>{
    const {client,cmd,bodies}=await bridge();
    expect(await client.call(cmd,'submit',{summary:'交付一次服务',details:'周三上午'},'nexloop.enterprise.deliver:1')).toEqual(receipt);
    expect(Object.keys(bodies[0]!).sort()).toEqual(['action','activation_ref','command','parameters']);
    expect(bodies[0]!.action).toBe('nexloop.enterprise.deliver');
    expect(bodies[0]!.parameters).toEqual({summary:'交付一次服务',details:'周三上午',consumer_ref:cmd.consumer_ref});
    expect(await client.call(cmd,'find',{intent_id:intent},'nexloop.enterprise.deliver:1')).toEqual(receipt);
    expect(bodies[1]!.action).toBe('nexloop.enterprise.deliver');
    // The reply tool's request is unchanged (no action key).
    await client.call(cmd,'submit',{message:'one service'});
    expect(Object.keys(bodies[2]!).sort()).toEqual(['activation_ref','command','parameters']);
  });
  type Run={execute:(args:unknown)=>Promise<{isError?:boolean;content:{text:string}[]}>;description:string};
  const tool=(client:RuntimeEffectClient,cmd:ReturnType<typeof command>,name:string)=>client.toolsForRun(cmd,{enterprise:supply}).find(item=>item.name===name) as unknown as Run;
  it.each([['fulfilled','order_placed'],['confirmed','order_placed'],['observed_fulfilled','order_placed'],['accepted','sending'],['dispatching','sending'],['unknown','checking'],['failed','not_placed']] as const)(
    '10/17 calibration: a %s receipt is shown as request_status %s with the enterprise result pending, never the raw receipt',async(state,status)=>{
    const {client,cmd,bodies}=await bridge({change:body=>({...body,receipt:{...receipt,state}})});
    for(const [name,args] of [['nexloop.enterprise.deliver',{summary:'上门检查一次'}],['nexloop.enterprise.find',{request_id:intent}]] as const){
      const result=await tool(client,cmd,name).execute(args);
      expect(result.isError).toBeUndefined();
      const shown=JSON.parse(result.content[0]!.text);
      expect(Object.keys(shown).sort()).toEqual(['enterprise_result','next_step','request_id','request_status']);
      expect(shown).toMatchObject({request_id:intent,request_status:status,enterprise_result:'pending_enterprise'});
      // No internal field reaches the model: not the success flag, digests, receipt id or raw state.
      expect(result.content[0]!.text).not.toMatch(/business_action_success|payload_digest|receipt_id|scope|effect_intent/);
      expect(shown.next_step).toMatch(/Do not request it again/);
    }
    expect(bodies[1]!.intent_id).toBe(intent);  // find still reads the same intent
  });
  it('tells the model the result fields, that pending is not a failure, no second order, and the customer\'s language',async()=>{
    const {client,cmd}=await bridge();
    for(const name of ['nexloop.enterprise.deliver','nexloop.enterprise.find']){
      const description=tool(client,cmd,name).description;
      expect(description).toContain(ENTERPRISE_RESULT_GUIDE);
      expect(description).toMatch(/not a failure/);
      expect(description).toMatch(/Within this Run, once the result is back, do not request the same order again/);
      expect(description).not.toMatch(/place the order again yourself/);  // 10/18: no longer read as "never order again"
      expect(description).toMatch(/never these field names/);
    }
    expect(tool(client,cmd,'nexloop.enterprise.deliver').description).toMatch(/summary and details in the customer's own language/);
  });
  it('10/18: a refused delivery parameter comes back to the model as invalid_effect_request (a tool result), not an exception',async()=>{
    type Run={execute:(args:unknown)=>Promise<{isError?:boolean;content:{text:string}[]}>};
    const {client,cmd,bodies}=await bridge();
    const deliver=client.toolsForRun(cmd,{enterprise:supply}).find(t=>t.name==='nexloop.enterprise.deliver') as unknown as Run;
    for(const args of [{summary:'x',consumer_ref:cmd.consumer_ref.replace('consumer:','')},{summary:'x'.repeat(201)}]){
      const result=await deliver.execute(args);
      expect(result.isError).toBe(true);expect(JSON.parse(result.content[0]!.text)).toEqual({code:'invalid_effect_request'});
    }
    expect(bodies).toEqual([]);
  });
  it('10/18: a find of an intent this Run did not create (guard 403 effect_intent_unavailable) is a coded result, not an exception',async()=>{
    type Run={execute:(args:unknown)=>Promise<{isError?:boolean;content:{text:string}[]}>;description:string};
    const {client,cmd}=await bridge({status:403,change:()=>({code:'effect_intent_unavailable'})});
    const tools=client.toolsForRun(cmd,{enterprise:supply});
    const find=tools.find(t=>t.name==='nexloop.enterprise.find') as unknown as Run;
    const result=await find.execute({request_id:intent});
    expect(result.isError).toBe(true);expect(JSON.parse(result.content[0]!.text)).toEqual({code:'effect_intent_unavailable'});
    const reply=tools.find(t=>t.name==='nexloop.service.request') as unknown as Run;
    expect(JSON.parse((await reply.execute({message:'x'})).content[0]!.text)).toEqual({code:'effect_intent_unavailable'});
    expect(find.description).toMatch(/only a request_id that nexloop.enterprise.deliver returned in this Run/);
  });
  it('10/18 (trial 4f5d795c): a new order in a new message is placed even when the history shows an earlier one',async()=>{
    const {client,cmd}=await bridge();
    const tools=client.toolsForRun(cmd,{enterprise:supply});
    const deliver=(tools.find(t=>t.name==='nexloop.enterprise.deliver') as unknown as {description:string}).description;
    const find=(tools.find(t=>t.name==='nexloop.enterprise.find') as unknown as {description:string}).description;
    expect(deliver).toMatch(/that is a new order: place it now with this tool, even if the conversation history shows an earlier order/);
    expect(deliver).toMatch(/an earlier order is never a reason to skip this one/);
    expect(find).toMatch(/Not being able to read an earlier order never stops a new one/);
  });
  it('10/18 (Run eede1a41): the enterprise offering in a reply\'s request_scope is refused before the network, with what to do',async()=>{
    type Run={execute:(args:unknown)=>Promise<{isError?:boolean;content:{text:string}[]}>;description:string};
    const {client,cmd,bodies}=await bridge();
    const reply=client.toolsForRun(cmd,{enterprise:supply}).find(t=>t.name==='nexloop.service.request') as unknown as Run;
    const scope={offering_id:supply.offering_id,offering_revision:1,requested_guarantees:[],requested_discounts:[]};
    const refused=await reply.execute({message:'已下单',request_scope:scope});
    expect(refused.isError).toBe(true);expect(JSON.parse(refused.content[0]!.text).code).toBe('request_scope_wrong_service');
    expect(bodies).toEqual([]);  // nothing reached the guard
    expect((await reply.execute({message:'已下单'})).isError).toBeUndefined();  // the reply without scope goes out
    expect(reply.description).toMatch(/never put the enterprise offering or a request_id in it/);
  });
  it('refuses another consumer, unknown keys and oversized text before the network',async()=>{
    const {client,cmd,bodies}=await bridge();
    for(const args of [{summary:'x',consumer_ref:'consumer:'+'f'.repeat(64)},{summary:'x',tenant_id:cmd.tenant_id},{summary:''},{summary:'x'.repeat(201)},{summary:'x',details:'y'.repeat(2001)},{details:'only'}])
      await expect(client.call(cmd,'submit',args,'nexloop.enterprise.deliver:1')).rejects.toThrow('invalid_effect_request');
    expect(bodies).toEqual([]);
  });
});
