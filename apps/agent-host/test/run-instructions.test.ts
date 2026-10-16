import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { afterEach, describe, expect, it } from 'vitest';
import { fauxAssistantMessage } from '../../../vendor/pi/packages/ai/dist/index.js';
import { PiRuntimeAdapter } from '../src/pi-runtime-adapter.ts';
import { RUN_INSTRUCTIONS } from '../src/runtime-effect-tools.ts';
import { command, deterministic, until } from './pi-runtime-support.ts';

// 10/16 stage smoke: a deepseek-flash message Run answered in plain text (never delivered) and "succeeded" without a reply.
// A real model is now told that only tool calls act and that the reply goes through nexloop.service.request.
const directories:string[]=[];const adapters:PiRuntimeAdapter[]=[];
afterEach(async()=>{for(const a of adapters.splice(0))await a.close();for(const d of directories.splice(0))await rm(d,{recursive:true,force:true});});

async function seenSystemPrompt(instructions?:string){
  const path=await mkdtemp(join(tmpdir(),'nexloop-run-instructions-'));directories.push(path);
  const provider=deterministic();let seen:unknown='unset';
  // Pi renders agent instructions as a system section of the transcript (durable harness/generation.js).
  provider.faux.setResponses([(context:{messages:Array<{role:string;content?:unknown}>})=>{seen=JSON.stringify(context.messages.filter(m=>m.role==='system'));return fauxAssistantMessage('done');}]);
  const adapter=new PiRuntimeAdapter({root:path,models:provider.models,model:{provider:'faux',modelId:'faux-1'},costPolicy:{kind:'deterministic_zero',currency:'USD'},
    assertOwner:()=>{},authorize:async()=>({ever_execution_authorized:false}),...(instructions?{instructions}:{})});
  adapters.push(adapter);
  const cmd=command();await adapter.start(cmd,'{"user_statement":"hello"}');
  await until(async()=>(await adapter.inspect(cmd)).submission_status==='done');
  return seen;
}

describe('run instructions for real-model Runs',()=>{
  it('reach the model as its instructions, and only when given',async()=>{
    expect(String(await seenSystemPrompt(RUN_INSTRUCTIONS))).toContain('call nexloop.service.request exactly once');
    expect(String(await seenSystemPrompt())).not.toContain('nexloop.service.request');
  });
  it('say that text outside a tool call is never delivered (the reply tool says it too: runtime-effect-tools.test)',()=>{
    expect(RUN_INSTRUCTIONS).toContain('never shown to anyone');
  });
});
