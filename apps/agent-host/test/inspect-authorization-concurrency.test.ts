/** NX-031 (lane-history-fix): an inspect's guard authorization runs before the Host's single serial queue, so inspects of
 * different Runs no longer wait for each other's guard round trips; the storage checks stay inside the queue. */
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { afterEach, describe, expect, it } from 'vitest';
import { fauxAssistantMessage } from '../../../vendor/pi/packages/ai/dist/index.js';
import { PiRuntimeAdapter } from '../src/pi-runtime-adapter.ts';
import { command, deterministic, until } from './pi-runtime-support.ts';

const directories:string[]=[];const adapters:PiRuntimeAdapter[]=[];
afterEach(async()=>{for(const adapter of adapters.splice(0))await adapter.close();for(const directory of directories.splice(0))await rm(directory,{recursive:true,force:true});});

describe('inspect authorization outside the serial queue',()=>{
  it('concurrent inspects of different Runs overlap their guard waits',async()=>{
    const path=await mkdtemp(join(tmpdir(),'nexloop-inspect-concurrency-'));directories.push(path);
    const provider=deterministic();provider.faux.setResponses([fauxAssistantMessage('one'),fauxAssistantMessage('two')]);
    let slow=false,active=0,peak=0;
    const adapter=new PiRuntimeAdapter({root:path,models:provider.models,model:{provider:'faux',modelId:'faux-1'},costPolicy:{kind:'deterministic_zero',currency:'USD'},
      assertOwner:()=>{},authorize:async(_command,operation)=>{
        if(operation==='inspect'&&slow){active++;peak=Math.max(peak,active);await new Promise(resolve=>setTimeout(resolve,300));active--;}
        return {ever_execution_authorized:operation==='model'};
      }});
    adapters.push(adapter);
    const first=command(),second=command();
    await adapter.start(first,'input one');await adapter.start(second,'input two');
    await until(async()=>(await adapter.inspect(first)).runtime_outcome!=='running'&&(await adapter.inspect(second)).runtime_outcome!=='running');
    slow=true;
    const started=Date.now();
    const results=await Promise.all([adapter.inspect(first),adapter.inspect(second),adapter.inspect(first),adapter.inspect(second)]);
    const elapsed=Date.now()-started;
    expect(results.map(r=>r.runtime_outcome)).toEqual(['succeeded','succeeded','succeeded','succeeded']);
    expect(peak).toBe(4);
    expect(elapsed).toBeLessThan(900);  // serialized guard waits would take at least 1200 ms
  });
  it('a closed adapter still refuses an inspect that was authorized before it closed',async()=>{
    const path=await mkdtemp(join(tmpdir(),'nexloop-inspect-closed-'));directories.push(path);
    const provider=deterministic();provider.faux.setResponses([fauxAssistantMessage('done')]);
    let release:()=>void=()=>{};const gate=new Promise<void>(resolve=>{release=resolve;});let hold=false;
    const adapter=new PiRuntimeAdapter({root:path,models:provider.models,model:{provider:'faux',modelId:'faux-1'},costPolicy:{kind:'deterministic_zero',currency:'USD'},
      assertOwner:()=>{},authorize:async(_command,operation)=>{if(operation==='inspect'&&hold)await gate;return {ever_execution_authorized:operation==='model'};}});
    const cmd=command();await adapter.start(cmd,'input');
    hold=true;const pending=adapter.inspect(cmd);
    const closing=adapter.close();release();
    await expect(pending).rejects.toMatchObject({code:'runtime_closed'});
    await closing;
  });
});
