// Actual frozen Pi Harness + private backend guard transport. No backend secrets.
import {createInterface} from 'node:readline';
import {createConnection} from 'node:net';
import {fstatSync,lstatSync} from 'node:fs';
import {join} from 'node:path';
import {PiRuntimeAdapter} from '../dist/pi-runtime-adapter.js';
import {createModels,fauxProvider,fauxAssistantMessage,fauxToolCall,Type} from '../../../vendor/pi/packages/ai/dist/index.js';
import {defineTool} from '../../../vendor/pi/packages/durable/dist/index.js';
const lines=createInterface({input:process.stdin});
const input=lines[Symbol.asyncIterator]();
const configuration=JSON.parse((await input.next()).value);
const descriptor=Number(process.env.NEXLOOP_TEST_OWNER_FD);
function assertOwner(){
  const held=fstatSync(descriptor),current=lstatSync(join(configuration.root,'.nexloop-owner.lock'));
  if(!held.isFile()||held.dev!==current.dev||held.ino!==current.ino||(held.mode&0o777)!==0o600)throw new Error('runtime_owner_lost');
}
async function authorize(command,operation){
  const socket=createConnection(configuration.socket);
  let response='';socket.setEncoding('utf8');
  await new Promise((resolve,reject)=>{
    socket.once('error',()=>reject(new Error('runtime_authorization_denied')));
    socket.once('connect',()=>socket.end(JSON.stringify({command,operation})+'\n'));
    socket.on('data',chunk=>{response+=chunk;if(response.length>65536)socket.destroy(new Error('invalid response'));});
    socket.once('end',resolve);
  });
  if(JSON.parse(response).authorized!==true){process.stdout.write(JSON.stringify({event:'authorization_denied',operation})+'\n');throw new Error('runtime_authorization_denied');}
  // This legacy direct-authority test transport never enrolls an activation;
  // creation is explicitly allowed only in its owned deterministic fixture.
  return {ever_execution_authorized:false};
}
const models=createModels(),faux=fauxProvider();models.setProvider(faux.provider);
let release;
const released=new Promise(resolve=>{release=resolve;});
void (async()=>{for await(const line of { [Symbol.asyncIterator]:()=>input }){if(JSON.parse(line).operation==='release')release();}})();
const tool=defineTool({name:'nexloop.probe',description:'Pure integration probe',parameters:Type.Object({}),execute:async()=>{
  process.stdout.write(JSON.stringify({event:'tool_executed'})+'\n');return {content:[{type:'text',text:'synthetic result'}]};
}});
if(configuration.mode==='blocked')faux.setResponses([async(_request,options)=>{
  process.stdout.write(JSON.stringify({event:'generation_started'})+'\n');
  await Promise.race([released,new Promise((_,reject)=>options.signal.addEventListener('abort',()=>reject(options.signal.reason),{once:true}))]);
  return fauxAssistantMessage(fauxToolCall('nexloop.probe',{}, {id:'synthetic-pg-tool'}),{stopReason:'toolUse'});
},fauxAssistantMessage('complete')]);
else faux.setResponses([fauxAssistantMessage('recovered deterministic completion')]);
const adapter=new PiRuntimeAdapter({root:configuration.root,models,model:{provider:'faux',modelId:'faux-1'},
  tools:[tool],costPolicy:{kind:'deterministic_zero',currency:'USD'},assertOwner,authorize});
const receipt=await adapter[configuration.resume?'resume':'start'](configuration.command,'synthetic PG-guarded input');
process.stdout.write(JSON.stringify({event:'accepted',receipt})+'\n');
if(configuration.mode==='complete'){
  const deadline=Date.now()+10000;
  while((await adapter.inspect(configuration.command)).submission_status!=='done'){
    if(Date.now()>deadline)throw new Error('runtime completion timeout');await new Promise(resolve=>setTimeout(resolve,10));
  }
  process.stdout.write(JSON.stringify({event:'completed',inspection:await adapter.inspect(configuration.command)})+'\n');
  await adapter.close();lines.close();process.stdin.destroy();
}else setInterval(()=>{},1000);
