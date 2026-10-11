/** NX-031 (dispatcher 2026-10-11): the adapter keeps an unanswered guard (runtime_authorization_unavailable) apart from a
 * denial all the way to the Run outcome; inspection names it with one allowlisted failure_code and nothing else. */
import { mkdtemp, rm, readdir } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { afterEach, describe, expect, it } from 'vitest';
import { Type, fauxAssistantMessage, fauxToolCall } from '../../../vendor/pi/packages/ai/dist/index.js';
import { defineTool } from '../../../vendor/pi/packages/durable/dist/index.js';
import { PiRuntimeAdapter } from '../src/pi-runtime-adapter.ts';
import { RuntimeError } from '../src/runtime-adapter.ts';
import { command, deterministic, until } from './pi-runtime-support.ts';

const directories:string[]=[];const adapters:PiRuntimeAdapter[]=[];
afterEach(async()=>{for(const adapter of adapters.splice(0))await adapter.close();for(const directory of directories.splice(0))await rm(directory,{recursive:true,force:true});});
async function open(authorize:(operation:string)=>Promise<{ever_execution_authorized:boolean}>,toolFails=false){
  const path=await mkdtemp(join(tmpdir(),'nexloop-pi-unavailable-'));directories.push(path);
  const provider=deterministic();
  const tool=defineTool({name:'nexloop.probe',description:'Pure deterministic probe',parameters:Type.Object({}),execute:async()=>{if(toolFails)throw new Error('synthetic tool failure');return {content:[{type:'text' as const,text:'ok'}]};}});
  const adapter=new PiRuntimeAdapter({root:path,models:provider.models,model:{provider:'faux',modelId:'faux-1'},costPolicy:{kind:'deterministic_zero',currency:'USD'},
    assertOwner:()=>{},tools:[tool],authorize:async(_command,operation)=>authorize(operation)});
  adapters.push(adapter);
  provider.faux.setResponses([fauxAssistantMessage(fauxToolCall('nexloop.probe',{}, {id:'probe-call'}),{stopReason:'toolUse'}),fauxAssistantMessage('complete')]);
  return {adapter,path};
}

describe('runtime_authorization_unavailable through the Pi adapter',()=>{
  it('is kept at the admission boundary and creates no storage',async()=>{
    const {adapter,path}=await open(async()=>{throw new RuntimeError('runtime_authorization_unavailable');});
    await expect(adapter.start(command(),'input')).rejects.toMatchObject({code:'runtime_authorization_unavailable'});
    expect(await readdir(path)).toEqual([]);
  });
  it('a plain error or any other code is still denied',async()=>{
    for(const failure of [new Error('synthetic'),new RuntimeError('runtime_owner_unavailable')]){
      const {adapter}=await open(async()=>{throw failure;});
      await expect(adapter.start(command(),'input')).rejects.toMatchObject({code:'runtime_authorization_denied'});
    }
  });
  it.each(['tool','model'] as const)('an unavailable %s authorization fails the Run with failure_code',async kind=>{
    const {adapter}=await open(async operation=>{if(operation===kind)throw new RuntimeError('runtime_authorization_unavailable');return {ever_execution_authorized:operation==='tool'||operation==='model'};});
    const cmd=command();await adapter.start(cmd,'input');
    await until(async()=>(await adapter.inspect(cmd)).runtime_outcome!=='running');
    const inspection=await adapter.inspect(cmd);
    expect(inspection.runtime_outcome).toBe('failed');expect(inspection.failure_code).toBe('runtime_authorization_unavailable');
  });
  it('a denied tool authorization fails the Run as denied',async()=>{
    const {adapter}=await open(async operation=>{if(operation==='tool')throw new Error('synthetic deny');return {ever_execution_authorized:operation==='model'};});
    const cmd=command();await adapter.start(cmd,'input');
    await until(async()=>(await adapter.inspect(cmd)).runtime_outcome!=='running');
    const inspection=await adapter.inspect(cmd);
    expect(inspection.runtime_outcome).toBe('failed');expect(inspection.failure_code).toBe('runtime_authorization_denied');
  });
  it('a failed Run that no authorization caused has no failure_code',async()=>{
    const {adapter}=await open(async operation=>{if(operation==='model')return {ever_execution_authorized:true};if(operation==='tool')return {ever_execution_authorized:true};return {ever_execution_authorized:false};},true);
    const cmd=command();await adapter.start(cmd,'input');
    await until(async()=>(await adapter.inspect(cmd)).runtime_outcome!=='running');
    const inspection=await adapter.inspect(cmd);
    expect(inspection.runtime_outcome).toBe('failed');expect(inspection).not.toHaveProperty('failure_code');
  });
  it('a succeeded Run has no failure_code',async()=>{
    const {adapter}=await open(async operation=>({ever_execution_authorized:operation==='tool'||operation==='model'}));
    const cmd=command();await adapter.start(cmd,'input');
    await until(async()=>(await adapter.inspect(cmd)).runtime_outcome!=='running');
    const inspection=await adapter.inspect(cmd);
    expect(inspection.runtime_outcome).toBe('succeeded');expect(inspection).not.toHaveProperty('failure_code');
  });
});
