/** Synthetic SSE conformance only: actual frozen SDK/Pi/SQLite, no EIOS write or supplier success. */
import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp,rm,readFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {randomUUID} from 'node:crypto';
import {DatabaseSync} from 'node:sqlite';
import {Type} from '../apps/agent-host/node_modules/@earendil-works/pi-ai/dist/index.js';
import {defineTool} from '../apps/agent-host/node_modules/@earendil-works/pi-durable/dist/index.js';
import {PiRuntimeAdapter} from '../apps/agent-host/dist/pi-runtime-adapter.js';
import {providerToolNames} from '../apps/agent-host/dist/provider-tool-names.js';
import {selectTrustedModel} from '../apps/agent-host/dist/trusted-model-profile.js';
const pack=(await readFile(new URL('./offline-context-pack.json',import.meta.url),'utf8')).trim();
const key='offline-synthetic-key-never-a-real-credential';
const canonical='nexloop.service.request';
const wire='nexloop_service_request';
const command=()=>({schema_version:'1.0',run_id:randomUUID(),tenant_id:randomUUID(),world_id:'real',mode:'real',request_id:'offline-'+randomUUID(),trigger_event_id:randomUUID(),role_ref:'role:synthetic',consumer_ref:'consumer:synthetic',goal_version_ref:'goal:synthetic',context_manifest_ref:'artifact:synthetic',runtime_profile:'deepseek-flash',credential_ref:'credential:synthetic',runtime_owner_epoch:1,budget:{maximum_model_turns:2,maximum_tool_calls:2,active_timeout_seconds:30,maximum_cost:'0.62',currency:'USD'},not_after:new Date(Date.now()+120000).toISOString()});
function response(name){
 const delta=name?{role:'assistant',tool_calls:[{index:0,id:'offline-call',type:'function',function:{name,arguments:'{"message":"synthetic wire execution"}'}}]}:{role:'assistant',content:'offline parse completion'};
 const chunks=[{id:'offline',model:'deepseek-flash',choices:[{index:0,delta,finish_reason:null}]},{id:'offline',model:'deepseek-flash',choices:[{index:0,delta:{},finish_reason:name?'tool_calls':'stop'}],usage:{prompt_tokens:10,completion_tokens:5,total_tokens:15}}];
 return new Response(chunks.map(c=>'data: '+JSON.stringify(c)+'\n\n').join('')+'data: [DONE]\n\n',{status:200,headers:{'content-type':'text/event-stream'}});
}
test('aliases are deterministic, bounded, collision-refusing and synthetic mode unchanged',()=>{
 assert.deepEqual(providerToolNames([canonical,'nexloop.service.find'],true),[wire,'nexloop_service_find']);
 assert.deepEqual(providerToolNames([canonical],false),[canonical]);
 for(const names of [[canonical,canonical],['nexloop.a.b','nexloop.a_b'],['nexloop.'+'x'.repeat(130)],['shell.exec']]) assert.throws(()=>providerToolNames(names,true),e=>e.code==='runtime_tool_refused');
});
for(const name of [wire,canonical,'unknown_tool']) test('actual SDK SSE/Pi '+name+' mapping and persisted history',async()=>{
 const root=await mkdtemp(join(tmpdir(),'nexloop-offline-alias-'));let adapter;let ambient=0,executed=0;const requests=[],operations=[];const previous=globalThis.fetch;
 globalThis.fetch=async()=>{ambient++;throw Error('NETWORK_FORBIDDEN');};
 try{
  const selected=selectTrustedModel({MODEL_PROVIDER:'deepseek',MODEL_ID:'deepseek-flash',MODEL_API_KEY:key,MODEL_BASE_URL:'https://api.deepseek.com'},()=>{throw Error('NO_FILE_READ');},false);
  const models=new Proxy(selected.models,{get(target,p){if(p==='streamSimple')return (model,ctx,opts)=>target.streamSimple(model,ctx,{...opts,maxRetries:0,timeoutMs:1000,fetch:async(input,init)=>{const request=new Request(input,init);assert.equal(request.headers.get('authorization'),'Bearer '+key);const body=JSON.parse(await request.text());requests.push(body);return response(requests.length===1?name:null);}});const value=Reflect.get(target,p,target);return typeof value==='function'?value.bind(target):value;}});
  const tool=defineTool({name:canonical,description:'Pure offline synthetic probe; no business writes.',parameters:Type.Object({message:Type.String()},{additionalProperties:false}),execute:async args=>{executed++;assert.equal(args.message,'synthetic wire execution');return {content:[{type:'text',text:'canonical execute closure'}]};}});
  adapter=new PiRuntimeAdapter({root,models,model:{provider:'deepseek',modelId:'deepseek-flash'},tools:[tool],costPolicy:{kind:'bounded_request',currency:'USD',maximum_request_cost:'0.31'},assertOwner:()=>{},authorize:async(_cmd,op)=>{operations.push(op);return {ever_execution_authorized:false};}});
  const cmd=command();const mapping=await adapter.start(cmd,pack);
  const deadline=Date.now()+10000;let inspection;
  do{inspection=await adapter.inspect(cmd);if(inspection.submission_status==='done')break;await new Promise(r=>setTimeout(r,10));}while(Date.now()<deadline);
  assert.equal(inspection.submission_status,'done');assert.equal(ambient,0);
  assert.equal(requests.length,2);for(const request of requests){assert.deepEqual(request.tools.map(t=>t.function.name),[wire]);assert(request.messages.some(m=>m.role==='user'&&m.content===pack));}
  assert.equal(requests[1].messages.find(m=>m.tool_calls)?.tool_calls[0].function.name,name);
  assert.equal(executed,name===wire?1:0);assert.equal(inspection.runtime_outcome,name===wire?'succeeded':'failed');
  if(name===wire)assert(operations.includes('tool'));
  assert.deepEqual(inspection.persistence,{journal_mode:'wal',synchronous:2});
  await adapter.close();
  const db=new DatabaseSync(join(root,cmd.run_id,'runtime.sqlite'),{readOnly:true});try{assert.equal(db.prepare('select count(*) n from submissions').get().n,1);assert(db.prepare('select record from entries').all().some(r=>r.record.includes(name)));}finally{db.close();}
  // Reopen with the same stable registry alias, no additional model request/submission.
  adapter=new PiRuntimeAdapter({root,models,model:{provider:'deepseek',modelId:'deepseek-flash'},tools:[tool],costPolicy:{kind:'bounded_request',currency:'USD',maximum_request_cost:'0.31'},assertOwner:()=>{},authorize:async()=>({ever_execution_authorized:false})});
  assert.deepEqual(await adapter.resume(cmd,pack),mapping);assert.equal(requests.length,2);
 }finally{await adapter?.close();globalThis.fetch=previous;await rm(root,{recursive:true,force:true});}
});
