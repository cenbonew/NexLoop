// 2026-11-08 (stage files_runtime, L4 NX-035-file-backup-runtime-wal.md): the Host closed a Run's storage only at purge (30 days),
// world deletion or exit, so every finished Run kept its SQLite open and a non-empty WAL blocked the file backup. A finished Run is
// now checkpointed (TRUNCATE) and closed by the inspect that sees it finished, a settled Run nobody asks about is closed by the idle
// sweep, a Run still executing is never closed, and a busy checkpoint never fails the answer.
import { mkdtemp, rm, stat } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { DatabaseSync } from 'node:sqlite';
import { afterEach, describe, expect, it } from 'vitest';
import { fauxAssistantMessage } from '../../../vendor/pi/packages/ai/dist/index.js';
import { PiRuntimeAdapter } from '../src/pi-runtime-adapter.ts';
import { command, deterministic, until } from './pi-runtime-support.ts';

const directories:string[]=[];const adapters:PiRuntimeAdapter[]=[];
async function root(){const path=await mkdtemp(join(tmpdir(),'nexloop-finished-'));directories.push(path);return path;}
function open(path:string,extra:Record<string,unknown>={}){
  const provider=deterministic();
  const adapter=new PiRuntimeAdapter({root:path,models:provider.models,model:{provider:'faux',modelId:'faux-1'},
    costPolicy:{kind:'deterministic_zero',currency:'USD'},assertOwner:()=>{},authorize:async()=>({ever_execution_authorized:false}),...extra});
  adapters.push(adapter);return {adapter,...provider};
}
const isOpen=(adapter:PiRuntimeAdapter,runId:string)=>(adapter as unknown as {runs:Map<string,unknown>}).runs.has(runId);
async function walBytes(path:string,runId:string){try{return (await stat(join(path,runId,'runtime.sqlite-wal'))).size;}catch(error){if((error as NodeJS.ErrnoException).code==='ENOENT')return 0;throw error;}}
afterEach(async()=>{
  for(const adapter of adapters.splice(0))await adapter.close();
  for(const directory of directories.splice(0))await rm(directory,{recursive:true,force:true});
});

describe('finished Run storage is checkpointed and closed',()=>{
  it('the inspect that sees a finished Run empties its WAL and closes it; another inspect reopens it with the same answer',async()=>{
    const path=await root();const {adapter}=open(path);const cmd=command();
    const accepted=await adapter.start(cmd,'synthetic input');await adapter.settled(cmd);
    expect(isOpen(adapter,cmd.run_id)).toBe(true);
    expect(await walBytes(path,cmd.run_id)).toBeGreaterThan(0);  // what kept the stage backup failing
    const first=await adapter.inspect(cmd);
    expect(first.runtime_outcome).toBe('succeeded');
    expect(isOpen(adapter,cmd.run_id)).toBe(false);
    expect(await walBytes(path,cmd.run_id)).toBe(0);
    const again=await adapter.inspect(cmd);
    // A reopened harness reports scheduling 'paused' (as after a Host restart); nothing reads it, every other field is the same.
    const {scheduling:firstScheduling,...firstRest}=first,{scheduling:againScheduling,...againRest}=again;
    expect(againRest).toEqual(firstRest);expect([firstScheduling,againScheduling].every(value=>['running','paused'].includes(value))).toBe(true);expect({run_id:again.run_id,request_id:again.request_id,conversation_id:again.conversation_id,submission_id:again.submission_id}).toEqual(accepted);
    expect(isOpen(adapter,cmd.run_id)).toBe(false);
  });

  it('a closed finished Run resumes from its storage (the same submission, nothing executed again)',async()=>{
    const path=await root();const {adapter}=open(path);const cmd=command();
    const accepted=await adapter.start(cmd,'synthetic input');await adapter.settled(cmd);
    await adapter.inspect(cmd);expect(isOpen(adapter,cmd.run_id)).toBe(false);
    expect(await adapter.resume(cmd,'synthetic input')).toEqual(accepted);
    await adapter.settled(cmd);
    expect((await adapter.inspect(cmd)).runtime_outcome).toBe('succeeded');
  });

  it('the idle sweep closes a settled Run nobody asks about',async()=>{
    const path=await root();const {adapter}=open(path,{finishedIdleCloseMs:20});const cmd=command();
    await adapter.start(cmd,'synthetic input');await adapter.settled(cmd);
    await until(async()=>!isOpen(adapter,cmd.run_id),5_000);
    expect(await walBytes(path,cmd.run_id)).toBe(0);
  });

  it('a Run still executing is never closed, neither by inspect nor by the sweep',async()=>{
    const path=await root();const {adapter,faux}=open(path,{finishedIdleCloseMs:5});const cmd=command();let reached=false;
    faux.setResponses([(_request,options)=>new Promise((_,reject)=>{reached=true;options!.signal!.addEventListener('abort',()=>reject(options!.signal!.reason),{once:true});})]);
    await adapter.start(cmd,'hanging input');await until(async()=>reached);
    await new Promise(resolve=>setTimeout(resolve,100));  // twenty sweep periods
    expect(isOpen(adapter,cmd.run_id)).toBe(true);
    expect((await adapter.inspect(cmd)).runtime_outcome).toBe('running');
    expect(isOpen(adapter,cmd.run_id)).toBe(true);
    await adapter.cancel(cmd);await adapter.settled(cmd);
    expect((await adapter.inspect(cmd)).runtime_outcome).toBe('cancelled');
    expect(isOpen(adapter,cmd.run_id)).toBe(false);
  });

  it('resume and inspect racing the sweep all succeed with the same receipt',async()=>{
    const path=await root();const {adapter}=open(path,{finishedIdleCloseMs:1});const cmd=command();
    const accepted=await adapter.start(cmd,'synthetic input');await adapter.settled(cmd);
    for(let round=0;round<20;round++){
      const [resumed,inspected]=await Promise.all([adapter.resume(cmd,'synthetic input'),adapter.inspect(cmd)]);
      expect(resumed).toEqual(accepted);expect(inspected.runtime_outcome).toBe('succeeded');
      await adapter.settled(cmd);
    }
  });

  it('a busy checkpoint leaves the Run open and the answer intact; the next sweep closes it',async()=>{
    const path=await root();const {adapter,faux}=open(path,{finishedIdleCloseMs:20});const cmd=command();
    faux.setResponses([fauxAssistantMessage('synthetic completion')]);
    await adapter.start(cmd,'synthetic input');await adapter.settled(cmd);
    // A reader holding a snapshot pins the WAL: TRUNCATE reports busy.
    const reader=new DatabaseSync(join(path,cmd.run_id,'runtime.sqlite'),{readOnly:true});
    reader.exec('begin');reader.prepare('select count(*) from submissions').get();
    try{
      const began=Date.now();
      expect((await adapter.inspect(cmd)).runtime_outcome).toBe('succeeded');
      expect(Date.now()-began).toBeLessThan(2_000);  // never waits out the busy timeout
      expect(isOpen(adapter,cmd.run_id)).toBe(true);
    }finally{reader.exec('commit');reader.close();}
    await until(async()=>!isOpen(adapter,cmd.run_id),5_000);
    expect(await walBytes(path,cmd.run_id)).toBe(0);
  });
});
