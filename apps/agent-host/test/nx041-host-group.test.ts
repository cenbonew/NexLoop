/** NX-041 S7 (design §6, D3; AT-046 "Host across groups"): a Host process runs the Runs of its own world group only. */
import {describe,it,expect} from 'vitest';
import {groupAccepts,parseWorldGroup} from '../dist/worlds.js';

const SIM='sim:00000000-0000-4000-8000-000000000001',SHADOW='shadow:00000000-0000-4000-8000-000000000002';

describe('NX-041 Host world group',()=>{
  it('defaults to the real group, which runs real and test only (positive control) and refuses sim and shadow',()=>{
    const group=parseWorldGroup(undefined);
    expect(group).toBe('real');
    expect(groupAccepts(group,'real')).toBe(true);expect(groupAccepts(group,'test')).toBe(true);
    expect(groupAccepts(group,SIM)).toBe(false);expect(groupAccepts(group,SHADOW)).toBe(false);
  });
  it('the simulation group runs sim and shadow only (positive control) and refuses real and test',()=>{
    const group=parseWorldGroup('simulation');
    expect(groupAccepts(group,SIM)).toBe(true);expect(groupAccepts(group,SHADOW)).toBe(true);
    expect(groupAccepts(group,'real')).toBe(false);expect(groupAccepts(group,'test')).toBe(false);
  });
  it('refuses anything that is not a world, and any other group value in the configuration',()=>{
    for(const world of ['simulation','sim:1',null,undefined,42])expect(groupAccepts('simulation',world)).toBe(false);
    for(const value of ['sim','test','shadow',true,'']) expect(()=>parseWorldGroup(value)).toThrow('runtime configuration refused');
  });
});

import {afterEach} from 'vitest';
import {mkdtempSync,writeFileSync,chmodSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';import {join} from 'node:path';
import {RuntimeHost} from '../src/runtime-host.js';import {readPrivateMaterial} from '../src/private-material.js';import {command} from './pi-runtime-support.ts';

const roots:string[]=[];const hosts:RuntimeHost[]=[];
afterEach(async()=>{for(const host of hosts.splice(0))await host.close();for(const root of roots.splice(0))rmSync(root,{recursive:true});});
function host(group?:string){
  const root=mkdtempSync(join(tmpdir(),'nexloop-host-group-'));chmodSync(root,0o700);roots.push(root);
  const file=(name:string,value:string)=>{const path=join(root,name);writeFileSync(path,value,{mode:0o600});return path;};
  const config=file('runtime-config',JSON.stringify({guard_ca_file:file('guard-ca','synthetic-not-used-ca'),guard_key_file:file('guard-key','a'.repeat(64)),
    guard_url:'https://127.0.0.1:65431/internal/v1/runtime/authorize',runtime_profile:'deterministic-test',...(group===undefined?{}:{world_group:group}),...(group==='simulation'?{context_input_protocol:'nexloop.context-pack.sim.v1'}:{}),
    // Integration with S6: a simulation Host also has its own id and purge key.
    ...(group==='simulation'?{host_id:'sim-host-a',world_purge_key_file:file('purge-key','b'.repeat(64))}:{})}));
  const h=new RuntimeHost(root,config,(path:string,max:number,empty?:boolean)=>readPrivateMaterial(path,max,empty),()=>{});hosts.push(h);return h;
}
const start=(h:RuntimeHost,world:string,mode:string)=>{const cmd={...command(),world_id:world,mode,runtime_profile:'deterministic-test'};
  return h.dispatch('start',{activation_ref:'activation_'+'g'.repeat(24),command:cmd,input:'test'});};

describe('NX-041 Host world group at dispatch (actual RuntimeHost)',()=>{
  it('a real-group Host refuses a sim Run before any guard request; a simulation-group Host refuses a real Run',async()=>{
    await expect(start(host(),SIM,'simulation')).rejects.toThrow('invalid_run_world');
    await expect(start(host('simulation'),'real','real')).rejects.toThrow('invalid_run_world');
    await expect(start(host('simulation'),'test','test')).rejects.toThrow('invalid_run_world');
  });
  it('positive control: a Run of the Host group passes the group check (it then fails only at the unreachable synthetic guard)',async()=>{
    const real=await start(host(),'real','real').then(()=>'started',(e:Error)=>e.message);
    const sim=await start(host('simulation'),SIM,'simulation').then(()=>'started',(e:Error)=>e.message);
    expect(real).not.toContain('invalid_run_world');expect(sim).not.toContain('invalid_run_world');
  });
  it('a configuration with another group value is refused',()=>{expect(()=>host('shadow')).toThrow('runtime configuration refused');});
});

import {mkdtemp,rm} from 'node:fs/promises';
import {PiRuntimeAdapter} from '../src/pi-runtime-adapter.ts';
import {deterministic,until} from './pi-runtime-support.ts';

const adapterRoots:string[]=[];const adapters:PiRuntimeAdapter[]=[];
afterEach(async()=>{for(const a of adapters.splice(0))await a.close();for(const r of adapterRoots.splice(0))await rm(r,{recursive:true,force:true});});
async function adapter(worldDirectories:boolean){
  const root=await mkdtemp(join(tmpdir(),'nexloop-resume-world-'));adapterRoots.push(root);const provider=deterministic();
  const a=new PiRuntimeAdapter({root,worldDirectories,models:provider.models,model:{provider:'faux',modelId:'faux-1'},
    costPolicy:{kind:'deterministic_zero',currency:'USD'},assertOwner:()=>{},authorize:async()=>({ever_execution_authorized:false})});
  adapters.push(a);return a;
}

describe('NX-041 S7 review 2.2: resume never changes the world of a Run',()=>{
  it('simulation Host: resuming a sim Run under another sim world finds no state; the same command resumes (positive control)',async()=>{
    const a=await adapter(true);const cmd={...command(),world_id:SIM,mode:'simulation'};
    await a.start(cmd,'input');await until(async()=>(await a.inspect(cmd)).submission_status==='done');
    await expect(a.resume({...cmd,world_id:SIM.replace('0001','0009')},'input')).rejects.toMatchObject({code:'runtime_state_missing'});
    await expect(a.resume({...cmd,world_id:SHADOW,mode:'shadow'},'input')).rejects.toMatchObject({code:'runtime_state_missing'});
    await expect(a.resume(cmd,'input')).resolves.toBeDefined();
  });
  it('real Host: resuming a real Run as test is a conflict with the stored command; the same command resumes (positive control)',async()=>{
    const a=await adapter(false);const cmd={...command(),world_id:'real',mode:'real'};
    await a.start(cmd,'input');await until(async()=>(await a.inspect(cmd)).submission_status==='done');
    await expect(a.resume({...cmd,world_id:'test',mode:'test'},'input')).rejects.toMatchObject({code:'runtime_request_conflict'});
    await expect(a.resume(cmd,'input')).resolves.toBeDefined();
  });
});
