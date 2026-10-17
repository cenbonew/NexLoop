/** Owner 2026-10-18 (option B): a tool error refused with a fixed code (guard or Host) and followed later by a success of the
 * same tool was corrected by the model: the Run succeeds and records recovered_tool_errors [{tool, code}]. Every other case
 * fails the Run as before: a code outside the set, no later success, a later success of another tool only, a thrown tool, an
 * authorization failure, any other failure of the Run. */
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { afterEach, describe, expect, it } from 'vitest';
import { Type, fauxAssistantMessage, fauxToolCall } from '../../../vendor/pi/packages/ai/dist/index.js';
import { defineTool } from '../../../vendor/pi/packages/durable/dist/index.js';
import { PiRuntimeAdapter } from '../src/pi-runtime-adapter.ts';
import { command, deterministic, until } from './pi-runtime-support.ts';

const directories:string[]=[];const adapters:PiRuntimeAdapter[]=[];
afterEach(async()=>{for(const a of adapters.splice(0))await a.close();for(const d of directories.splice(0))await rm(d,{recursive:true,force:true});});

type Step={code?:string;throws?:boolean};
function tool(name:string,steps:Step[]){
  let call=0;
  return defineTool({name,description:'probe',parameters:Type.Object({}),execute:async()=>{
    const step=steps[Math.min(call++,steps.length-1)]!;
    if(step.throws)throw new Error('synthetic failure');
    return step.code?{isError:true,content:[{type:'text' as const,text:JSON.stringify({code:step.code})}]}:{content:[{type:'text' as const,text:'{"ok":true}'}]};
  }});
}
async function run(tools:unknown[],calls:string[],extra:Record<string,unknown>={},tail?:unknown){
  const path=await mkdtemp(join(tmpdir(),'nexloop-recovered-'));directories.push(path);
  const provider=deterministic({});
  const adapter=new PiRuntimeAdapter({root:path,models:provider.models,model:{provider:'faux',modelId:'faux-1'},tools:tools as never,
    costPolicy:{kind:'deterministic_zero',currency:'USD'},assertOwner:()=>{},authorize:async()=>({ever_execution_authorized:false}),...extra});
  adapters.push(adapter);
  provider.faux.setResponses([...calls.map((name,i)=>fauxAssistantMessage(fauxToolCall(name,{},{id:'call-'+i}),{stopReason:'toolUse'})),
    tail??fauxAssistantMessage('done')]);
  const cmd=command();await adapter.start(cmd,'customer message');
  await until(async()=>(await adapter.inspect(cmd)).active_tasks===0);
  return adapter.inspect(cmd);
}

describe('corrected tool errors (option B)',()=>{
  it('a refused call followed by a success of the same tool: succeeded, recorded',async()=>{
    const got=await run([tool('nexloop.reply',[{code:'effect_intent_unavailable'},{code:'request_scope_wrong_service'},{}])],['nexloop.reply','nexloop.reply','nexloop.reply']);
    expect(got.runtime_outcome).toBe('succeeded');
    expect(got.recovered_tool_errors).toEqual([{tool:'nexloop.reply',code:'effect_intent_unavailable'},{tool:'nexloop.reply',code:'request_scope_wrong_service'}]);
    expect(got.failure_code).toBeUndefined();
  });
  it.each(['invalid_effect_request','outside_catalog_terms'])('%s is a correctable refusal',async code=>{
    const got=await run([tool('nexloop.reply',[{code},{}])],['nexloop.reply','nexloop.reply']);
    expect(got.runtime_outcome).toBe('succeeded');expect(got.recovered_tool_errors).toEqual([{tool:'nexloop.reply',code}]);
  });
  it('a code outside the set (intent_payload_conflict) still fails the Run',async()=>{
    const got=await run([tool('nexloop.reply',[{code:'intent_payload_conflict'},{}])],['nexloop.reply','nexloop.reply']);
    expect(got.runtime_outcome).toBe('failed');expect(got.failure_detail).toBe('intent_payload_conflict');expect(got.failure_tool).toBe('nexloop.reply');
    expect(got.recovered_tool_errors).toBeUndefined();
  });
  it('no later success of that tool still fails the Run',async()=>{
    const got=await run([tool('nexloop.reply',[{code:'effect_intent_unavailable'}])],['nexloop.reply']);
    expect(got.runtime_outcome).toBe('failed');expect(got.failure_detail).toBe('effect_intent_unavailable');
  });
  it('a later success of another tool only still fails the Run',async()=>{
    const got=await run([tool('nexloop.reply',[{code:'effect_intent_unavailable'}]),tool('nexloop.other',[{}])],['nexloop.reply','nexloop.other']);
    expect(got.runtime_outcome).toBe('failed');expect(got.failure_tool).toBe('nexloop.reply');
  });
  it('a thrown tool is never corrected',async()=>{
    const got=await run([tool('nexloop.reply',[{throws:true},{}])],['nexloop.reply','nexloop.reply']);
    expect(got.runtime_outcome).toBe('failed');expect(got.failure_code).toBe('runtime_tool_error');expect(got.recovered_tool_errors).toBeUndefined();
  });
  it('an authorization failure is never corrected',async()=>{
    let calls=0;
    const got=await run([tool('nexloop.reply',[{code:'effect_intent_unavailable'},{}])],['nexloop.reply','nexloop.reply'],
      {authorize:async(_c:unknown,operation:string)=>{if(operation==='tool'&&calls++===1)throw new Error('denied');return {ever_execution_authorized:false};}});
    expect(got.runtime_outcome).toBe('failed');expect(got.recovered_tool_errors).toBeUndefined();
  });
  it('any other failure of the Run still fails it (a provider error after the correction)',async()=>{
    const got=await run([tool('nexloop.reply',[{code:'effect_intent_unavailable'},{}])],['nexloop.reply','nexloop.reply'],{},
      fauxAssistantMessage('x',{stopReason:'error',errorMessage:'503 upstream'}));
    expect(got.runtime_outcome).toBe('failed');expect(got.recovered_tool_errors).toBeUndefined();
  });
});
