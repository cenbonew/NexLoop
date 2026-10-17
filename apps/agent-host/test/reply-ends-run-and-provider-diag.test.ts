/** 10/18 stage (Run 1fb1e35a): a message Run whose reply was accepted ran one more model turn, which failed, and the whole
 * Run was recorded failed; the provider failure said nothing. (1) A tool round whose every call asks to terminate ends the
 * Run there (the reply tool asks it in a message Run). (2) A failed model call leaves one allowlisted stderr line. */
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { Type, fauxAssistantMessage, fauxToolCall } from '../../../vendor/pi/packages/ai/dist/index.js';
import { defineTool } from '../../../vendor/pi/packages/durable/dist/index.js';
import { PiRuntimeAdapter, modelFailureLine } from '../src/pi-runtime-adapter.ts';
import { providerFailure } from '../src/runtime-provider-boundary.ts';
import { messageRun } from '../src/runtime-host.ts';
import { command, deterministic, until } from './pi-runtime-support.ts';

const directories:string[]=[];const adapters:PiRuntimeAdapter[]=[];
async function open(tools:unknown[],extra:Record<string,unknown>={}){
  const path=await mkdtemp(join(tmpdir(),'nexloop-reply-ends-'));directories.push(path);
  const provider=deterministic({});
  const adapter=new PiRuntimeAdapter({root:path,models:provider.models,model:{provider:'faux',modelId:'faux-1'},tools:tools as never,
    costPolicy:{kind:'deterministic_zero',currency:'USD'},assertOwner:()=>{},authorize:async()=>({ever_execution_authorized:false}),...extra});
  adapters.push(adapter);return {adapter,...provider};
}
afterEach(async()=>{vi.restoreAllMocks();for(const a of adapters.splice(0))await a.close();for(const d of directories.splice(0))await rm(d,{recursive:true,force:true});});

describe('a message Run ends with its reply',()=>{
  it('ends at the tool round when the reply asks to terminate: no closing model call, succeeded',async()=>{
    const reply=defineTool({name:'nexloop.reply',description:'reply',parameters:Type.Object({}),
      execute:async()=>({content:[{type:'text' as const,text:'{"state":"accepted"}'}],control:{terminate:true as const}})});
    const {adapter,faux}=await open([reply]);let closing=0;
    faux.setResponses([fauxAssistantMessage(fauxToolCall('nexloop.reply',{},{id:'reply-1'}),{stopReason:'toolUse'}),
      ()=>{closing++;return fauxAssistantMessage('closing words');}]);
    const cmd=command();await adapter.start(cmd,'customer message');
    await until(async()=>(await adapter.inspect(cmd)).submission_status==='done');
    const inspected=await adapter.inspect(cmd);
    expect(inspected.runtime_outcome).toBe('succeeded');expect(closing).toBe(0);expect(inspected.model_timing?.calls).toBe(1);
  });
  it('without the control the closing model turn still runs (Role and plan Runs keep their loop)',async()=>{
    const tool=defineTool({name:'nexloop.reply',description:'reply',parameters:Type.Object({}),execute:async()=>({content:[{type:'text' as const,text:'{}'}]})});
    const {adapter,faux}=await open([tool]);let closing=0;
    faux.setResponses([fauxAssistantMessage(fauxToolCall('nexloop.reply',{},{id:'reply-1'}),{stopReason:'toolUse'}),()=>{closing++;return fauxAssistantMessage('done');}]);
    const cmd=command();await adapter.start(cmd,'x');await until(async()=>(await adapter.inspect(cmd)).submission_status==='done');
    expect(closing).toBe(1);
  });
  it('the Host knows a message Run by its customer statement',()=>{
    expect(messageRun(JSON.stringify({user_statement:{message_id:'m'},current_event:{kind:'consumer_message'}}))).toBe(true);
    expect(messageRun(JSON.stringify({user_statement:null,current_event:{kind:'service_trigger'}}))).toBe(false);
    expect(messageRun('not json')).toBe(false);expect(messageRun(undefined)).toBe(false);
  });
});

describe('a failed model call leaves one allowlisted diagnostic line',()=>{
  it('provider error: kind and HTTP status, never the provider text',async()=>{
    const sentinel='synthetic-secret-sentinel-'+crypto.randomUUID();
    const stderr=vi.spyOn(process.stderr,'write').mockImplementation(()=>true);
    const {adapter,faux}=await open([]);
    faux.setResponses([fauxAssistantMessage(sentinel,{stopReason:'error',errorMessage:'503 Service Unavailable: '+sentinel})]);
    const cmd=command();await adapter.start(cmd,'x');await until(async()=>(await adapter.inspect(cmd)).active_tasks===0);
    const lines=stderr.mock.calls.map(c=>String(c[0])).filter(l=>l.includes('model call failed'));
    expect(lines).toHaveLength(1);
    const line=JSON.parse(lines[0]!);
    expect(line.detail).toMatchObject({run_id:cmd.run_id,call_sequence:1,kind:'provider_error',http_status:503,reason:'http',cause_code:null});
    expect(Object.keys(line.detail).sort()).toEqual(['call_sequence','cause_code','elapsed_ms','exception','http_status','kind','reason','run_id']);
    expect(JSON.stringify(stderr.mock.calls)).not.toContain(sentinel);
    expect((await adapter.inspect(cmd)).runtime_outcome).toBe('failed');
  });
  it('fields come only from the status and the class name',()=>{
    class ProviderTimeout extends Error{}
    expect(providerFailure('stream_exception',new ProviderTimeout('timed out after 12000 ms'))).toEqual({kind:'stream_exception',http_status:null,exception:'ProviderTimeout',reason:'timeout',cause_code:null});
    expect(providerFailure('provider_error',{status:429,errorMessage:'x'})).toEqual({kind:'provider_error',http_status:429,exception:null,reason:'http',cause_code:null});
    expect(providerFailure('provider_error',{errorMessage:'upstream said 200 ok'})).toEqual({kind:'provider_error',http_status:null,exception:null,reason:'other',cause_code:null});
    expect(JSON.parse(modelFailureLine('r',3,12085.4,{kind:'aborted',http_status:null,exception:null})).detail).toEqual(
      {run_id:'r',call_sequence:3,elapsed_ms:12085,kind:'aborted',http_status:null,exception:null,reason:'other',cause_code:null});
  });
  it('10/18: a reason from the provider text (never the text) and an undici/Node code from the cause chain',()=>{
    const connect=Object.assign(new TypeError('fetch failed'),{cause:Object.assign(new Error('Connect Timeout Error'),{code:'UND_ERR_CONNECT_TIMEOUT'})});
    expect(providerFailure('stream_exception',connect)).toMatchObject({reason:'timeout',cause_code:'UND_ERR_CONNECT_TIMEOUT',http_status:null});
    expect(providerFailure('provider_error',{errorMessage:'Connection error.'})).toMatchObject({reason:'connection',cause_code:null});
    expect(providerFailure('provider_error',{errorMessage:'Request timed out.'})).toMatchObject({reason:'timeout'});
    expect(providerFailure('aborted',{errorMessage:'Request was aborted'})).toMatchObject({reason:'aborted'});
    expect(providerFailure('provider_error',Object.assign(new Error('x'),{code:'not a code; secret'}))).toMatchObject({cause_code:null});
  });
});

describe('10/18: a configured model request timeout',()=>{
  async function optionsSeen(extra:Record<string,unknown>,budget?:number){
    const {adapter,models}=await open([],extra);
    const seen:unknown[]=[];const original=models.streamSimple.bind(models);
    (models as {streamSimple:unknown}).streamSimple=(m:unknown,c:unknown,o:unknown)=>{seen.push(o);return (original as (...a:unknown[])=>unknown)(m,c,o);};
    const cmd=command();if(budget!==undefined)(cmd.budget as {active_timeout_seconds:number}).active_timeout_seconds=budget;
    await adapter.start(cmd,'x');await until(async()=>(await adapter.inspect(cmd)).active_tasks===0);
    return seen as ({timeoutMs?:number}|undefined)[];
  }
  it('reaches the provider as timeoutMs, bounded by the Run budget; absent when not configured',async()=>{
    expect((await optionsSeen({modelRequestTimeoutMs:40000}))[0]?.timeoutMs).toBe(40000);
    const bounded=(await optionsSeen({modelRequestTimeoutMs:40000},20))[0]?.timeoutMs!;
    expect(bounded).toBeLessThanOrEqual(20000);expect(bounded).toBeGreaterThan(15000);
    expect((await optionsSeen({}))[0]?.timeoutMs).toBeUndefined();
  });
});
