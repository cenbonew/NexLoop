/** NX-031 AT-060 (A2): the Run's model time from its durable Pi task records: generation spans minus the tool spans they own. */
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { DatabaseSync } from 'node:sqlite';
import { afterEach, describe, expect, it } from 'vitest';
import { Type, fauxAssistantMessage, fauxToolCall } from '../../../vendor/pi/packages/ai/dist/index.js';
import { defineTool } from '../../../vendor/pi/packages/durable/dist/index.js';
import { PiRuntimeAdapter } from '../src/pi-runtime-adapter.ts';
import { modelTiming } from '../src/model-timing.ts';
import { command, deterministic, until } from './pi-runtime-support.ts';

const directories:string[]=[];const adapters:PiRuntimeAdapter[]=[];
afterEach(async()=>{for(const a of adapters.splice(0))await a.close();for(const d of directories.splice(0))await rm(d,{recursive:true,force:true});});
const sleep=(ms:number)=>new Promise(resolve=>setTimeout(resolve,ms));

describe('modelTiming',()=>{
  it('subtracts the tool spans a generation owns and sums several generations',()=>{
    const tasks=[
      {id:1,kind:'pi.generation',startedAt:1000,endedAt:1900},{id:2,kind:'pi.tool',owner:1,startedAt:1300,endedAt:1800},
      {id:3,kind:'pi.generation',startedAt:1900,endedAt:2150},
      {id:4,kind:'pi.generation',startedAt:2150,endedAt:3000},{id:5,kind:'pi.tool',owner:4,startedAt:2200,endedAt:2400},{id:6,kind:'pi.tool',owner:4,startedAt:2400,endedAt:2900},
    ];
    expect(modelTiming(tasks)).toEqual({calls:3,total_ms:400+250+150,max_ms:400});
  });
  it('leaves out live or unstamped generations and never goes negative',()=>{
    expect(modelTiming([{id:1,kind:'pi.generation',startedAt:10},{id:2,kind:'pi.generation'},{id:3,kind:'pi.tool',owner:9,startedAt:0,endedAt:50}]))
      .toEqual({calls:0,total_ms:0,max_ms:0});
    expect(modelTiming([{id:1,kind:'pi.generation',startedAt:0,endedAt:10},{id:2,kind:'pi.tool',owner:1,startedAt:0,endedAt:30}])).toEqual({calls:1,total_ms:0,max_ms:0});
  });
});

describe('inspect reports model_timing from the durable task records',()=>{
  it('matches the persisted spans and survives a Host restart',async()=>{
    const path=await mkdtemp(join(tmpdir(),'nexloop-model-timing-'));directories.push(path);
    const open=()=>{const provider=deterministic();const adapter=new PiRuntimeAdapter({root:path,models:provider.models,model:{provider:'faux',modelId:'faux-1'},
      costPolicy:{kind:'deterministic_zero',currency:'USD'},assertOwner:()=>{},authorize:async()=>({ever_execution_authorized:false}),tools:[tool]});adapters.push(adapter);return {adapter,...provider};};
    const tool=defineTool({name:'nexloop.probe',description:'Deterministic probe that takes 120 ms',parameters:Type.Object({}),
      execute:async()=>{await sleep(120);return {content:[{type:'text' as const,text:'synthetic tool result'}]};}});
    const {adapter,faux}=open();
    faux.setResponses([async()=>{await sleep(60);return fauxAssistantMessage(fauxToolCall('nexloop.probe',{},{id:'probe-1'}),{stopReason:'toolUse'});},
      async()=>{await sleep(80);return fauxAssistantMessage('complete');}]);
    const cmd=command();
    await adapter.start(cmd,'synthetic input');
    await until(async()=>(await adapter.inspect(cmd)).runtime_outcome==='succeeded');
    const timing=(await adapter.inspect(cmd)).model_timing!;
    const db=new DatabaseSync(join(path,cmd.run_id,'runtime.sqlite'),{readOnly:true});
    const rows=db.prepare('select record from tasks').all().map(row=>JSON.parse(String(row.record)));db.close();
    expect(timing).toEqual(modelTiming(rows));
    expect(timing.calls).toBe(rows.filter(r=>String(r.kind).includes('generation')).length);
    expect(timing.calls).toBeGreaterThanOrEqual(2);
    expect(timing.total_ms).toBeGreaterThanOrEqual(130);  // the two scripted model delays
    const tools=rows.filter(r=>String(r.kind).includes('tool'));
    const generations=rows.filter(r=>String(r.kind).includes('generation')).reduce((sum,r)=>sum+r.endedAt-r.startedAt,0);
    expect(timing.total_ms).toBe(generations-tools.reduce((sum,r)=>sum+r.endedAt-r.startedAt,0));  // the 120 ms tool is not model time
    await adapter.close();adapters.splice(adapters.indexOf(adapter),1);
    const reopened=open().adapter;
    expect((await reopened.inspect(cmd)).model_timing).toEqual(timing);
  });
});
