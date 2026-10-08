import { PiRuntimeAdapter } from '../dist/pi-runtime-adapter.js';
import { deterministic } from './pi-runtime-support.ts';
let input='';for await(const chunk of process.stdin)input+=chunk;
const configuration=JSON.parse(input);
const {models,faux}=deterministic();
faux.setResponses([(_request,options)=>new Promise((_,reject)=>{
  process.stdout.write('generation_started\n');
  options!.signal!.addEventListener('abort',()=>reject(options!.signal!.reason),{once:true});
})]);
const adapter=new PiRuntimeAdapter({root:configuration.root,models,model:{provider:'faux',modelId:'faux-1'},
  costPolicy:{kind:'deterministic_zero',currency:'USD'},assertOwner:()=>{},authorize:async()=>({ever_execution_authorized:false})});
await adapter.start(configuration.command,'recover input');
setInterval(()=>{},1000);
