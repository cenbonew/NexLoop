// 2026-11-08 (stage files_runtime): a long-lived Host process holding one finished Run, for the file backup end-to-end test
// (tests/test_file_backup_finished_runs.py). stdin lines: 'inspect' runs one inspect (the Run is closed by it); stdout: 'settled',
// then 'inspected <outcome>' after each inspect. The process stays alive until stdin ends, as a Host does between requests.
import { createInterface } from 'node:readline';
import { PiRuntimeAdapter } from '../dist/pi-runtime-adapter.js';
import { deterministic } from './pi-runtime-support.ts';
const lines=createInterface({input:process.stdin});
const iterator=lines[Symbol.asyncIterator]();
const configuration=JSON.parse((await iterator.next()).value);
const {models}=deterministic();
const adapter=new PiRuntimeAdapter({root:configuration.root,models,model:{provider:'faux',modelId:'faux-1'},
  costPolicy:{kind:'deterministic_zero',currency:'USD'},assertOwner:()=>{},authorize:async()=>({ever_execution_authorized:false})});
await adapter.start(configuration.command,'backup input');
await adapter.settled(configuration.command);
process.stdout.write('settled\n');
for(;;){
  const next=await iterator.next();if(next.done)break;
  if(next.value==='inspect')process.stdout.write(`inspected ${(await adapter.inspect(configuration.command)).runtime_outcome}\n`);
}
await adapter.close();
