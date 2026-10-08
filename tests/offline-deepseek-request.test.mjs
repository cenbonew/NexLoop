/** Actual frozen SDK request construction, with every network path refused.
 * fetch throws deliberately. An error stream is expected, never provider success.
 * This unit evidence does not authorize a Run or test a live supplier.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {createModels,Type} from '../apps/agent-host/node_modules/@earendil-works/pi-ai/dist/index.js';
import {deepseekProvider} from '../apps/agent-host/node_modules/@earendil-works/pi-ai/dist/providers/deepseek.js';
import {selectTrustedModel} from '../apps/agent-host/dist/trusted-model-profile.js';
const syntheticKey='offline-synthetic-key-never-a-real-credential';
const pack=readFileSync(new URL('./offline-context-pack.json',import.meta.url),'utf8').trim();
const declarations=names=>names.map(name=>({name,description:'Synthetic request construction, not an execution permit.',parameters:Type.Object({message:Type.String({minLength:1,maxLength:8192})},{additionalProperties:false})}));
async function observe(models,model,names,toolChoice){
 const previous=globalThis.fetch;let ambient=0;globalThis.fetch=async()=>{ambient++;throw Error('ambient network refused');};
 const observations=[];let payloadSeen=0;
 try{
  const stream=models.streamSimple(model,{messages:[{role:'user',content:pack,timestamp:1},{role:'system',content:'',toolsAdded:declarations(names),timestamp:2}]},{maxRetries:0,timeoutMs:1000,...(toolChoice?{toolChoice}:{}),
   onPayload:value=>{payloadSeen++;return value;},
   fetch:async(input,init)=>{
    const request=new Request(input,init);const body=JSON.parse(await request.text());
    // Never retain raw headers/payloads. Key is synthetic and only compared.
    observations.push({origin:new URL(request.url).origin,path:new URL(request.url).pathname,model:body.model,maxTokens:body.max_tokens,names:body.tools.map(t=>t.function.name),schemasClosed:body.tools.every(t=>t.function.parameters.additionalProperties===false),preserved:body.messages.some(m=>m.role==='user'&&m.content===pack),...(body.tool_choice?{toolChoice:body.tool_choice}:{}),syntheticAuth:request.headers.get('authorization')==='Bearer '+syntheticKey});
    throw Error('OFFLINE_FETCH_OBSERVED');
   }});
  const result=await stream.result();assert.equal(result.stopReason,'error');
  assert.equal(ambient,0,'actual network is forbidden');
  return {observations,payloadSeen};
 }finally{globalThis.fetch=previous;}
}
for(const names of [['nexloop.service.request','nexloop.service.find'],['nexloop_service_request','nexloop_service_find']]){
 test('trusted actual SDK constructs '+names[0]+' without pre-HTTP rejection',async()=>{
  const selection=selectTrustedModel({MODEL_PROVIDER:'deepseek',MODEL_ID:'deepseek-flash',MODEL_API_KEY:syntheticKey,MODEL_BASE_URL:'https://api.deepseek.com'},()=>{throw Error('no file reads allowed');},false);
  const model=selection.models.getModel('deepseek','deepseek-flash');assert(model);
  const result=await observe(selection.models,model,names);
  assert.equal(result.payloadSeen,1);assert.equal(result.observations.length,1);
  assert.deepEqual(result.observations[0],{origin:'https://api.deepseek.com',path:'/chat/completions',model:'deepseek-flash',maxTokens:4096,names,schemasClosed:true,preserved:true,syntheticAuth:true});
 });
}
test('actual SDK without auth fails before onPayload or fetch',async()=>{
 const models=createModels({authContext:{env:async()=>undefined,fileExists:async()=>false}});models.setProvider(deepseekProvider());
 const model=models.getModel('deepseek','deepseek-flash');assert(model);
 const result=await observe(models,model,['nexloop.service.request']);
 assert.equal(result.payloadSeen,0);assert.deepEqual(result.observations,[]);
});

test('actual SDK forced choice uses the same stable wire alias (no RuntimeAdapter choice API)',async()=>{
 const selection=selectTrustedModel({MODEL_PROVIDER:'deepseek',MODEL_ID:'deepseek-flash',MODEL_API_KEY:syntheticKey,MODEL_BASE_URL:'https://api.deepseek.com'},()=>{throw Error('no file reads allowed');},false);
 const {providerToolNames}=await import('../apps/agent-host/dist/provider-tool-names.js');
 const [wire]=providerToolNames(['nexloop.service.request'],true);
 const choice={type:'function',function:{name:wire}};
 const result=await observe(selection.models,selection.models.getModel('deepseek','deepseek-flash'),[wire],choice);
 assert.equal(result.observations.length,1);assert.deepEqual(result.observations[0].toolChoice,choice);
});
