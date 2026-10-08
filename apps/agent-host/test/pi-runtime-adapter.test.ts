import { mkdtemp, rm, readdir, readFile, mkdir, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { DatabaseSync } from 'node:sqlite';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { fileURLToPath } from 'node:url';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { Type, fauxAssistantMessage, fauxToolCall } from '../../../vendor/pi/packages/ai/dist/index.js';
import { defineTool } from '../../../vendor/pi/packages/durable/dist/index.js';
import { PiRuntimeAdapter } from '../src/pi-runtime-adapter.ts';
import { command, deterministic, until } from './pi-runtime-support.ts';

const directories:string[]=[];
const adapters:PiRuntimeAdapter[]=[];
async function root(){const path=await mkdtemp(join(tmpdir(),'nexloop-pi-adapter-'));directories.push(path);return path;}
function open(path:string,extra:Record<string,unknown>={},providerOptions:Parameters<typeof deterministic>[0]={}) {
  const provider=deterministic(providerOptions);
  const adapter=new PiRuntimeAdapter({root:path,models:provider.models,model:{provider:'faux',modelId:'faux-1'},
    costPolicy:{kind:'deterministic_zero',currency:'USD'},assertOwner:()=>{},authorize:async()=>({ever_execution_authorized:false}),...extra});
  adapters.push(adapter);return {adapter,...provider};
}
function counts(path:string,runId:string) {
  const db=new DatabaseSync(join(path,runId,'runtime.sqlite'),{readOnly:true});
  try{return {conversations:db.prepare('select count(*) as n from conversations').get()?.n,
    submissions:db.prepare('select count(*) as n from submissions').get()?.n,
    conversation_id:db.prepare('select id from conversations').get()?.id,submission_id:db.prepare('select id from submissions').get()?.id,
    tasks:db.prepare('select id,kind,record from tasks').all(),entries:db.prepare('select record from entries').all()};}
  finally{db.close();}
}
afterEach(async()=>{
  vi.restoreAllMocks();
  for(const adapter of adapters.splice(0))await adapter.close();
  for(const directory of directories.splice(0))await rm(directory,{recursive:true,force:true});
});

describe('actual frozen Pi Harness RuntimeAdapter with SQLite FULL',()=>{
  it('runs real Generation and Tool, deduplicates identical request and rejects changed payload',async()=>{
    const path=await root();let calls=0;
    const tool=defineTool({name:'nexloop.probe',description:'Pure deterministic integration probe',parameters:Type.Object({}),
      execute:async()=>{calls++;return {content:[{type:'text' as const,text:'synthetic tool result'}]};}});
    const {adapter,faux}=open(path,{tools:[tool]});
    faux.setResponses([fauxAssistantMessage(fauxToolCall('nexloop.probe',{}, {id:'synthetic-tool-call'}),{stopReason:'toolUse'}),fauxAssistantMessage('complete')]);
    const cmd=command();
    const accepted=await adapter.start(cmd,'synthetic input');
    expect(await adapter.start(cmd,'synthetic input')).toEqual(accepted);
    await expect(adapter.start(cmd,'different input')).rejects.toMatchObject({code:'runtime_request_conflict'});
    await until(async()=>(await adapter.inspect(cmd)).submission_status==='done');
    expect((await adapter.inspect(cmd)).persistence).toEqual({journal_mode:'wal',synchronous:2});
    expect((await adapter.inspect(cmd)).runtime_outcome).toBe('succeeded');
    expect(calls).toBe(1);
    const persisted=counts(path,cmd.run_id);
    expect(persisted.conversations).toBe(1);expect(persisted.submissions).toBe(1);
    expect(persisted.tasks.some(row=>String(row.kind).includes('generation'))).toBe(true);
    expect(persisted.tasks.some(row=>String(row.kind).includes('tool'))).toBe(true);
    expect(persisted.entries.some(row=>String(row.record).includes('pi.tool-result'))).toBe(true);
    await adapter.close();
    const reopened=open(path).adapter;
    expect(await reopened.resume(cmd,'synthetic input')).toEqual(accepted);
    expect((await reopened.inspect(cmd)).submission_status).toBe('done');
    expect(counts(path,cmd.run_id).submissions).toBe(1);
  });

  it.each(['isError','diagnostic'] as const)('preserves returned Tool %s failure despite a completed answer',async kind=>{
    const path=await root();
    const tool=defineTool({name:'nexloop.probe',description:'Pure error-result probe',parameters:Type.Object({}),
      execute:async()=>({content:[{type:'text' as const,text:'synthetic tool response'}],
        ...(kind==='isError'?{isError:true}:{diagnostics:[{severity:'error' as const,code:'synthetic_error',message:'synthetic failure'}]})})});
    const {adapter,faux}=open(path,{tools:[tool]});
    faux.setResponses([fauxAssistantMessage(fauxToolCall('nexloop.probe',{}, {id:'error-result-tool'}),{stopReason:'toolUse'}),fauxAssistantMessage('complete')]);
    const cmd=command();await adapter.start(cmd,'synthetic input');
    await until(async()=>(await adapter.inspect(cmd)).submission_status==='done');
    expect((await adapter.inspect(cmd)).runtime_outcome).toBe('failed');
    expect(counts(path,cmd.run_id).tasks.some(row=>String(row.kind).includes('tool')&&String(row.record).includes('completed'))).toBe(true);
  });

  it('rejects fresh runtime creation after execution was authorized while preserving existing replay',async()=>{
    const path=await root();let ever=false;
    const {adapter}=open(path,{authorize:async()=>({ever_execution_authorized:ever})});
    const cmd=command();const receipt=await adapter.start(cmd,'synthetic input');
    await until(async()=>(await adapter.inspect(cmd)).submission_status==='done');
    ever=true;expect(await adapter.start(cmd,'synthetic input')).toEqual(receipt);
    await adapter.close();await rm(join(path,cmd.run_id,'runtime.sqlite'));
    const reopened=open(path,{authorize:async()=>({ever_execution_authorized:true})}).adapter;
    await expect(reopened.start(cmd,'synthetic input')).rejects.toMatchObject({code:'runtime_state_missing'});
    expect(await readdir(join(path,cmd.run_id))).not.toContain('runtime.sqlite');
  });

  it('requires explicit boolean execution history before creating storage',async()=>{
    const path=await root();const cmd=command();
    const {adapter}=open(path,{authorize:async()=>({ever_execution_authorized:undefined})});
    await expect(adapter.start(cmd,'input')).rejects.toMatchObject({code:'runtime_authorization_denied'});
    expect(await readdir(path)).toEqual([]);
  });

  it.each(['directory','empty_sqlite'] as const)('recovers unexecuted %s initialization with the original Run/request',async stage=>{
    const path=await root(),cmd=command(),directory=join(path,cmd.run_id);
    await mkdir(directory,{mode:0o700});
    if(stage==='empty_sqlite')await writeFile(join(directory,'runtime.sqlite'),'',{mode:0o600});
    const {adapter}=open(path);const receipt=await adapter.start(cmd,'synthetic input');
    await until(async()=>(await adapter.inspect(cmd)).runtime_outcome==='succeeded');
    expect(receipt.run_id).toBe(cmd.run_id);expect(receipt.request_id).toBe(cmd.request_id);
    expect(counts(path,cmd.run_id).submissions).toBe(1);
    expect((await adapter.inspect(cmd)).persistence).toEqual({journal_mode:'wal',synchronous:2});
  });

  it('preserves orphaned WAL evidence when the main SQLite file is missing',async()=>{
    const path=await root(),cmd=command(),directory=join(path,cmd.run_id);
    await mkdir(directory,{mode:0o700});await writeFile(join(directory,'runtime.sqlite-wal'),'synthetic orphan evidence',{mode:0o600});
    const {adapter}=open(path);await expect(adapter.start(cmd,'input')).rejects.toMatchObject({code:'runtime_state_missing'});
    expect(await readdir(directory)).toEqual(['runtime.sqlite-wal']);
  });

  it('cancels an actual in-flight Generation and preserves its persisted submission',async()=>{
    const path=await root();const {adapter,faux}=open(path);let reached=false;
    faux.setResponses([(_request,options)=>new Promise((_,reject)=>{
      reached=true;options!.signal!.addEventListener('abort',()=>reject(options!.signal!.reason),{once:true});
    })]);
    const cmd=command();await adapter.start(cmd,'cancel input');
    await until(async()=>reached);
    expect(await adapter.cancel(cmd)).toMatchObject({run_id:cmd.run_id,status:'cancel_requested'});
    await until(async()=>(await adapter.inspect(cmd)).active_tasks===0);
    expect((await adapter.inspect(cmd)).runtime_outcome).toBe('cancelled');
    expect(counts(path,cmd.run_id).submissions).toBe(1);
  });

  it('fails missing runtime state without silently creating a fresh session',async()=>{
    const path=await root();const {adapter}=open(path);const cmd=command();
    await expect(adapter.resume(cmd,'input')).rejects.toMatchObject({code:'runtime_state_missing'});
    await expect(adapter.inspect(cmd)).rejects.toMatchObject({code:'runtime_state_missing'});
    await adapter.start(cmd,'input');
    await until(async()=>(await adapter.inspect(cmd)).submission_status==='done');
    await adapter.close();
    // Deliberately remove only this owned disposable Run file, then require
    // recovery to report loss rather than creating an empty replacement.
    await rm(join(path,cmd.run_id,'runtime.sqlite'));
    const reopened=open(path).adapter;
    await expect(reopened.resume(cmd,'input')).rejects.toMatchObject({code:'runtime_state_missing'});
  });

  it('rechecks authenticated authority and owner before admitting work',async()=>{
    const path=await root();let authorized=false;
    const {adapter}=open(path,{authorize:async()=>{if(!authorized)throw new Error('synthetic authority denied');return {ever_execution_authorized:false};}});
    const cmd=command();await expect(adapter.start(cmd,'input')).rejects.toMatchObject({code:'runtime_authorization_denied'});
    authorized=true;await adapter.start(cmd,'input');
    authorized=false;await expect(adapter.inspect(cmd)).rejects.toMatchObject({code:'runtime_authorization_denied'});
  });

  it('rejects changed server binding after reopening persisted state',async()=>{
    const path=await root();const {adapter}=open(path);const cmd=command();
    await adapter.start(cmd,'input');
    await until(async()=>(await adapter.inspect(cmd)).submission_status==='done');
    await adapter.close();
    const reopened=open(path).adapter;
    const changed={...cmd,consumer_ref:'consumer:different'};
    await expect(reopened.resume(changed,'input')).rejects.toMatchObject({code:'runtime_request_conflict'});
    expect(counts(path,cmd.run_id).submissions).toBe(1);
  });

  it.each(['model','cost','tool'] as const)('enforces durable %s budget against actual Generation/Tool',async kind=>{
    const path=await root();let executions=0,requests=0;
    const tool=defineTool({name:'nexloop.probe',description:'Pure probe',parameters:Type.Object({}),
      execute:async()=>{executions++;return {content:[{type:'text' as const,text:'ok'}]};}});
    const policy=kind==='cost'?{kind:'bounded_request',currency:'USD',maximum_request_cost:'0.6'}:{kind:'deterministic_zero',currency:'USD'};
    const {adapter,faux}=open(path,{tools:[tool],costPolicy:policy});const cmd=command();
    if(kind==='model')cmd.budget.maximum_model_turns=1;
    if(kind==='tool')cmd.budget.maximum_tool_calls=1;
    faux.setResponses([()=>{requests++;return fauxAssistantMessage([
      fauxToolCall('nexloop.probe',{}, {id:'budget-tool-1'}),
      ...(kind==='tool'?[fauxToolCall('nexloop.probe',{}, {id:'budget-tool-2'})]:[])],{stopReason:'toolUse'});},
      ()=>{requests++;return fauxAssistantMessage('complete');}]);
    await adapter.start(cmd,'budget input');
    await until(async()=>(await adapter.inspect(cmd)).active_tasks===0);
    expect(executions).toBe(1);
    if(kind!=='tool')expect(requests).toBe(1);
    expect(JSON.stringify(counts(path,cmd.run_id))).toContain('runtime_budget_exhausted');
  });

  it.each(['missing','currency','nonzero_usage'] as const)('fails closed on %s cost policy',async kind=>{
    const path=await root();let requests=0;
    const costPolicy=kind==='missing'?undefined:{kind:'deterministic_zero',currency:kind==='currency'?'EUR':'USD'};
    const {adapter,faux}=open(path,{costPolicy},kind==='nonzero_usage'?{models:[{id:'faux-1',cost:{input:1,output:1,cacheRead:1,cacheWrite:1}}]}:{});const cmd=command();
    faux.setResponses([()=>{requests++;const message=structuredClone(fauxAssistantMessage('completion'));
      return message;}]);
    await adapter.start(cmd,'cost policy input');
    await until(async()=>(await adapter.inspect(cmd)).active_tasks===0);
    expect(requests).toBe(kind==='nonzero_usage'?1:0);
    expect(JSON.stringify(counts(path,cmd.run_id))).toContain(kind==='nonzero_usage'?'runtime_model_cost_mismatch':'runtime_cost_policy_missing');
  });

  it('aborts actual unresponsive Generation at active timeout',async()=>{
    const path=await root();const {adapter,faux}=open(path);let reached=false,aborted=false;
    faux.setResponses([(_request,options)=>new Promise((_,reject)=>{
      reached=true;options!.signal!.addEventListener('abort',()=>{aborted=true;reject(options!.signal!.reason);},{once:true});
    })]);
    const cmd=command();cmd.budget.active_timeout_seconds=1;
    await adapter.start(cmd,'timeout input');await until(async()=>reached);
    await until(async()=>aborted,3000);
    await until(async()=>(await adapter.inspect(cmd)).active_tasks===0);
  });

  it('rejects expiration reached while trusted authorization awaits',async()=>{
    const path=await root();const cmd=command();cmd.not_after=new Date(Date.now()+100).toISOString();
    const {adapter}=open(path,{authorize:async()=>{await new Promise(resolve=>setTimeout(resolve,150));return {ever_execution_authorized:false};}});
    await expect(adapter.start(cmd,'input')).rejects.toMatchObject({code:'runtime_command_expired'});
    expect(await readdir(path)).toEqual([]);
  });

  it.each(['model','provider_throw','tool','authorization'] as const)('keeps %s error sentinel out of runtime files and next model transcript',async kind=>{
    const sentinel='synthetic-secret-sentinel-'+crypto.randomUUID();const path=await root();let nextTranscript='';
    const stdout=vi.spyOn(process.stdout,'write'),stderr=vi.spyOn(process.stderr,'write');
    const tool=defineTool({name:'nexloop.probe',description:'Pure fault probe',parameters:Type.Object({}),
      execute:async()=>{throw new Error(sentinel);}});
    const {adapter,faux}=open(path,{tools:[tool],authorize:async()=>{if(kind==='authorization')throw new Error(sentinel);return {ever_execution_authorized:false};}});
    const cmd=command();
    if(kind==='authorization'){
      await expect(adapter.start(cmd,'input')).rejects.toMatchObject({code:'runtime_authorization_denied'});
      expect(await readdir(path)).toEqual([]);
      expect(JSON.stringify(stdout.mock.calls)).not.toContain(sentinel);
      expect(JSON.stringify(stderr.mock.calls)).not.toContain(sentinel);return;
    }
    if(kind==='provider_throw'){
      faux.setResponses([()=>{throw new Error(sentinel);}]);
    }else if(kind==='model'){
      const message=fauxAssistantMessage(sentinel,{stopReason:'error',errorMessage:sentinel});
      Object.assign(message,{diagnostics:{synthetic:sentinel},rawStopReason:sentinel});
      faux.setResponses([message]);
    }else faux.setResponses([fauxAssistantMessage(fauxToolCall('nexloop.probe',{}, {id:'sentinel-tool'}),{stopReason:'toolUse'}),
      request=>{nextTranscript=JSON.stringify(request.messages);return fauxAssistantMessage('complete');}]);
    await adapter.start(cmd,'public synthetic input');
    await until(async()=>(await adapter.inspect(cmd)).active_tasks===0);
    expect(JSON.stringify(stdout.mock.calls)).not.toContain(sentinel);
    expect(JSON.stringify(stderr.mock.calls)).not.toContain(sentinel);
    expect((await adapter.inspect(cmd)).runtime_outcome).toBe('failed');
    expect(nextTranscript).not.toContain(sentinel);
    expect(JSON.stringify(counts(path,cmd.run_id))).not.toContain(sentinel);
    for(const filename of await readdir(join(path,cmd.run_id))){
      expect((await readFile(join(path,cmd.run_id,filename))).includes(Buffer.from(sentinel))).toBe(false);
    }
  });

  it('SIGKILLs a real Generation process then resumes the same SQLite submission',async()=>{
    const path=await root();const cmd=command();
    const fixture=fileURLToPath(new URL('./pi-runtime-process.ts',import.meta.url));
    const child=spawn(process.execPath,['--experimental-strip-types',fixture],{stdio:['pipe','pipe','pipe'],env:{PATH:process.env.PATH}});
    let output='';let errors='';child.stdout.setEncoding('utf8');child.stderr.setEncoding('utf8');
    child.stdout.on('data',chunk=>{output+=chunk;});child.stderr.on('data',chunk=>{errors+=chunk;});
    child.stdin.end(JSON.stringify({root:path,command:cmd}));
    try{
      await until(async()=>{if(child.exitCode!==null)throw new Error('Pi child exited: '+errors);return output.includes('generation_started');},20_000);
      const before=counts(path,cmd.run_id);expect(before.conversations).toBe(1);expect(before.submissions).toBe(1);
      const exited=once(child,'exit');expect(child.kill('SIGKILL')).toBe(true);
      const [,sig]=await exited;expect(sig).toBe('SIGKILL');
      const {adapter}=open(path);const restored=await adapter.resume(cmd,'recover input');
      await until(async()=>(await adapter.inspect(cmd)).submission_status==='done');
      expect(restored.run_id).toBe(cmd.run_id);
      expect(restored.conversation_id).toBe(before.conversation_id);
      expect(restored.submission_id).toBe(before.submission_id);
      const generation=before.tasks.find(row=>String(row.kind).includes('generation'));
      expect(generation).toBeDefined();
      expect(counts(path,cmd.run_id).tasks.some(row=>row.id===generation!.id)).toBe(true);
      expect((await adapter.inspect(cmd)).persistence).toEqual({journal_mode:'wal',synchronous:2});
      expect(counts(path,cmd.run_id).submissions).toBe(1);
      expect(counts(path,cmd.run_id).conversations).toBe(1);
    }finally{if(child.exitCode===null&&child.signalCode===null){child.kill('SIGKILL');await once(child,'exit');}}
  },30_000);
});
