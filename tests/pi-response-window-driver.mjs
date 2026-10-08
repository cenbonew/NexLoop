/** Exact frozen afterResponse fault observer; no credential or fake grant input. */
import {createInterface} from 'node:readline';
import {request as httpsRequest} from 'node:https';
import {constants,openSync,readFileSync,closeSync,fstatSync,lstatSync} from 'node:fs';
import {join} from 'node:path';
import {PiRuntimeAdapter} from '../apps/agent-host/dist/pi-runtime-adapter.js';
import {RuntimeEffectClient} from '../apps/agent-host/dist/runtime-effect-tools.js';
import {createModels,fauxProvider,fauxAssistantMessage,fauxToolCall} from '../apps/agent-host/node_modules/@earendil-works/pi-ai/dist/index.js';
const lines=createInterface({input:process.stdin}),iterator=lines[Symbol.asyncIterator]();
const cfg=JSON.parse((await iterator.next()).value);
const owner=Number(process.env.NEXLOOP_TEST_OWNER_FD);
function assertOwner(){
  const held=fstatSync(owner),actual=lstatSync(join(cfg.root,'.nexloop-owner.lock'));
  if(!held.isFile()||!actual.isFile()||actual.isSymbolicLink()||held.dev!==actual.dev||held.ino!==actual.ino||(held.mode&0o777)!==0o600)throw new Error('owner_unavailable');
}
function privateMaterial(path,maximum){
  const fd=openSync(path,constants.O_RDONLY|constants.O_NOFOLLOW|constants.O_NONBLOCK);
  try{const st=fstatSync(fd);if(!st.isFile()||st.uid!==process.getuid()||(st.mode&0o777)!==0o600||st.nlink!==1||st.size>maximum)throw new Error('material_unavailable');return readFileSync(fd);}finally{closeSync(fd);}
}
const guard=new URL(cfg.guard_url);
if(guard.protocol!=='https:'||guard.hostname!=='127.0.0.1'||guard.pathname!=='/internal/v1/runtime/authorize'||!guard.port||guard.username||guard.password||guard.search||guard.hash)throw new Error('guard_unavailable');
async function authorize(command,operation){
  assertOwner();const key=privateMaterial(cfg.guard_key_file,64).toString('utf8');
  if(!/^[0-9a-f]{64}$/.test(key))throw new Error('guard_unavailable');
  const body=Buffer.from(JSON.stringify({activation_ref:cfg.activation_ref,command,operation,...(['start','resume'].includes(operation)?{input:cfg.input}:{})}));
  return await new Promise((resolve,reject)=>{
    const req=httpsRequest(guard,{method:'POST',agent:false,ca:privateMaterial(cfg.guard_ca_file,32768),minVersion:'TLSv1.2',signal:AbortSignal.timeout(2000),headers:{Authorization:'Bearer '+key,'Content-Type':'application/json','Content-Length':body.length}},res=>{
      const chunks=[];let n=0;res.on('data',chunk=>{n+=chunk.length;if(n>32768){req.destroy();reject(new Error('guard_unavailable'));}else chunks.push(chunk);});
      res.on('error',()=>reject(new Error('guard_unavailable')));
      res.on('end',()=>{try{const result=JSON.parse(Buffer.concat(chunks));if(res.statusCode!==200||result.authorized!==true||result.run_id!==command.run_id||typeof result.ever_execution_authorized!=='boolean'||(['model','tool'].includes(operation)&&result.ever_execution_authorized!==true))throw new Error();resolve({ever_execution_authorized:result.ever_execution_authorized});}catch{reject(new Error('guard_unavailable'));}});
    });req.on('error',()=>reject(new Error('guard_unavailable')));req.end(body);
  });
}
const models=createModels(),faux=fauxProvider();models.setProvider(faux.provider);
const lostId='model-returned-uncommitted',rebuiltId='model-regenerated-after-kill';
const input={message:'one governed runtime service'};
if(cfg.resume)faux.setResponses([
  fauxAssistantMessage(fauxToolCall('nexloop.service.request',input,{id:rebuiltId}),{stopReason:'toolUse'}),
  fauxAssistantMessage('Recovered deterministic result'),
]);
else faux.setResponses([
  fauxAssistantMessage(fauxToolCall('nexloop.service.request',input,{id:'service-request-first'}),{stopReason:'toolUse'}),
  fauxAssistantMessage(fauxToolCall('nexloop.service.request',input,{id:lostId}),{stopReason:'toolUse'}),
  fauxAssistantMessage('Must not commit before test kills process'),
]);
const effects=new RuntimeEffectClient({guardURL:guard,caPath:cfg.guard_ca_file,keyPath:cfg.guard_key_file,privateMaterial,activationForRun:runId=>runId===cfg.command.run_id?cfg.activation_ref:undefined});
let receiptReady;const ready=new Promise(resolve=>{receiptReady=resolve;});
const adapter=new PiRuntimeAdapter({root:cfg.root,models,model:{provider:'faux',modelId:'faux-1'},costPolicy:{kind:'deterministic_zero',currency:'USD'},assertOwner,authorize,
  toolsForRun:command=>effects.toolsForRun(command),
  observeModelResponse:async event=>{
    if(event.tool_call_ids.includes(lostId)){
      await ready;
      // Frozen hook follows complete streamResponse() terminal return and
      // precedes startToolRound AssistantEntry+ToolTask atomic SQLite commit.
      process.stdout.write(JSON.stringify({event:'model_response_returned_before_sqlite_commit',...event})+'\n');
      await new Promise(()=>{});
    }
  }});
try{
  const receipt=await adapter[cfg.resume?'resume':'start'](cfg.command,cfg.input);
  process.stdout.write(JSON.stringify({event:'accepted',receipt})+'\n');receiptReady();
  if(cfg.resume){
    const deadline=Date.now()+10000;
    let inspection;
    while((inspection=await adapter.inspect(cfg.command)).active_tasks!==0){if(Date.now()>deadline)throw new Error('completion_unavailable');await new Promise(resolve=>setTimeout(resolve,20));}
    process.stdout.write(JSON.stringify({event:'completed',inspection})+'\n');
    await adapter.close();lines.close();process.stdin.destroy();
  }else setInterval(()=>{},1000);
}catch{process.stderr.write('Runtime response-window driver unavailable\n');process.exitCode=1;lines.close();process.stdin.destroy();}
