/** NX-041 Host simulation context protocol (L6 nx041-s7b-review 3.4): the four acceptance items, each with its negative.
 * 1. a simulation Host accepts only nexloop.context-pack.sim.v1;  2. a real Host refuses it;  3. the model reads it only rendered with
 * the simulation preamble and "[模拟][source: simulated]" on every line;  4. no fallback to v6 (config and input). */
import {mkdtempSync,writeFileSync,chmodSync,rmSync,mkdirSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {randomUUID} from 'node:crypto';
import {afterEach,describe,it,expect} from 'vitest';
import {RuntimeHost} from '../src/runtime-host.js';
import {readPrivateMaterial} from '../src/private-material.js';
import {CONTEXT_PROTOCOL_SIM,SIM_LINE,SIM_PREAMBLE,parseSimContextPack,renderSimContextPack} from '../dist/sim-context.js';
import * as protocolModule from '../dist/context-input.js';
import {command} from './pi-runtime-support.ts';

const roots:string[]=[];const hosts:RuntimeHost[]=[];
afterEach(async()=>{for(const h of hosts.splice(0))await h.close();for(const r of roots.splice(0))rmSync(r,{recursive:true,force:true});});
const SIM='sim:'+randomUUID(),HEX='a'.repeat(64),ACTIVATION='activation_'+'a'.repeat(24);
function simCommand(world=SIM){const c=command();c.world_id=world;c.mode=world.startsWith('shadow:')?'shadow':'simulation';c.runtime_profile='deterministic-test';return c;}
function pack(c:ReturnType<typeof command>,over:Record<string,unknown>={}){
  const evidence=[{subsection:'simulated_conversation',ref:'sim:event:'+'b'.repeat(64),revision:'1',content:{event_kind:'message',text:'我想预约明天下午'},
    content_hash:HEX,evidence_kind:'simulated_input',source:'simulated',at:'2026-10-21T08:00:00.000000Z',tags:['latest_consumer_turn']}];
  return {schema_version:CONTEXT_PROTOCOL_SIM,world:c.world_id,tenant_id:c.tenant_id,consumer_id:'c'.repeat(64),data_mode:'simulation',world_time:'2026-10-21T08:00:00Z',
    current_event:{kind:'simulated_consumer_event',event_id:'b'.repeat(64),sequence:1,event_kind:'message',provenance:'sim:event:'+'b'.repeat(64),source:'simulated'},
    consumer_state:[{subsection:'profile',ref:'sim:object:'+'c'.repeat(64),revision:'r1',content:{segment:'synthetic'},content_hash:HEX,evidence_kind:'synthetic_profile',
      source:'simulated',at:'2026-10-21T08:00:00Z',tags:[]}],
    evidence,budget_report:{event_limit:20,events_total:1,events_included:1,omitted:0},insufficient:[],...over};
}
const V6={schema_version:'nexloop.context-pack.v6',bindings:{world_id:'real'},user_statement:{text:'x'}};
function host(group:'real'|'simulation'|undefined,protocol?:string){
  const r=mkdtempSync(join(tmpdir(),'nexloop-sim-ctx-'));chmodSync(r,0o700);roots.push(r);
  const file=(name:string,value:string)=>{const p=join(r,name);writeFileSync(p,value,{mode:0o600});return p;};
  const sim=group==='simulation'?{world_group:'simulation',host_id:'sim-host-one',world_purge_key_file:file('purge','5'.repeat(64))}:group==='real'?{world_group:'real'}:{};
  const config=file('runtime-config',JSON.stringify({guard_ca_file:file('ca','synthetic-not-used-ca'),guard_key_file:file('key','a'.repeat(64)),
    guard_url:'https://127.0.0.1:65431/internal/v1/runtime/authorize',runtime_profile:'deterministic-test',...sim,...(protocol===undefined?{}:{context_input_protocol:protocol}),
    ...(protocol!==undefined&&protocol!==CONTEXT_PROTOCOL_SIM?{deterministic_message_from_input:true,effect_tools:true}:{})}));
  const runtime=join(r,'runtime');mkdirSync(runtime,{mode:0o700});
  let reads=0;const material=(path:string,max:number,empty?:boolean)=>{reads++;return readPrivateMaterial(path,max,empty);};
  const h=new RuntimeHost(runtime,config,material,()=>{});hosts.push(h);return {host:h,reads:()=>reads};
}
const start=(h:RuntimeHost,c:ReturnType<typeof command>,input:unknown)=>h.dispatch('start',{activation_ref:ACTIVATION,command:c,input:typeof input==='string'?input:JSON.stringify(input)});

describe('NX-041 Host simulation context protocol',()=>{
  it('the protocol module exports the simulation pack parser by name (L3 pi_run_blockers probe)',()=>{
    expect(typeof (protocolModule as Record<string,unknown>).parseSimContextPack).toBe('function');
    expect((protocolModule as Record<string,unknown>).CONTEXT_PROTOCOL_SIM).toBe('nexloop.context-pack.sim.v1');
  });
  it('the parser takes exactly a contract pack for this Run and nothing else',()=>{
    const c=simCommand();
    expect(parseSimContextPack(JSON.stringify(pack(c)),c).world).toBe(SIM);
    const bad:Array<[string,unknown,ReturnType<typeof command>?]>=[
      ['v6 pack',V6],['another world',pack(simCommand())],['another tenant',pack(c,{tenant_id:'other'})],['data mode real',pack(c,{data_mode:'real'})],
      ['extra key',pack(c,{goal:{}})],['budget not matching',pack(c,{budget_report:{event_limit:20,events_total:3,events_included:1,omitted:0}})],
      ['an item not simulated',pack(c,{evidence:[{...pack(c).evidence[0],source:'real'}]})],['unknown tag',pack(c,{evidence:[{...pack(c).evidence[0],tags:['vip']}]})],
      ['a real-mode command',pack(c),{...c,world_id:'real',mode:'real'}],['not JSON','{'],
    ];
    for(const [label,value,cmd] of bad)expect(()=>parseSimContextPack(typeof value==='string'?value:JSON.stringify(value),cmd??c),label).toThrow('runtime_context_invalid');
  });
  it('3. rendering: the fixed preamble, every line marked simulated, goal/supply/relationship explicitly empty (negative: no raw pack text)',()=>{
    const c=simCommand(),text=renderSimContextPack(parseSimContextPack(JSON.stringify(pack(c)),c)),lines=text.split('\n');
    expect(lines[0]).toBe(SIM_PREAMBLE);expect(SIM_PREAMBLE).toContain('source: simulated');
    expect(lines.slice(1).every(line=>line.startsWith(SIM_LINE+' '))).toBe(true);
    for(const section of ['goal','supply','relationship'])expect(lines).toContain(`${SIM_LINE} ${section}: 空：模拟包不含${section==='goal'?'目标':section==='supply'?'供给':'关系'}段（不从真实${section==='goal'?'目标':section==='supply'?'供给':'关系'}补全）`);
    expect(text).toContain('我想预约明天下午');expect(text).not.toContain('"schema_version"');  // the model never reads the raw pack
  });
  it('1. a simulation Host admits only the simulation pack; v6, another protocol or a wrong pack is refused before any guard or read',async()=>{
    const f=host('simulation',CONTEXT_PROTOCOL_SIM),c=simCommand(),before=f.reads();
    for(const input of [V6,{schema_version:'nexloop.context-pack.v2'},pack(simCommand()),'plain text',pack(c,{source:'real'})])
      await expect(start(f.host,c,input)).rejects.toMatchObject({code:'invalid_sim_context'});
    expect(f.reads()).toBe(before);
    // Positive control: the right pack passes the input check and reaches admission (the unreachable guard answers there).
    const reached=await start(f.host,c,pack(c)).then(()=>'ok',(error:{code?:string})=>error.code);
    expect(reached).not.toBe('invalid_sim_context');
  },20000);
  it('3. what a simulation Host gives the model is the rendered pack, never the raw one',async()=>{
    const f=host('simulation',CONTEXT_PROTOCOL_SIM),c=simCommand();
    const internals=f.host as unknown as {authorizeAdmission:unknown;adapter:{start:(cmd:unknown,input:string)=>Promise<unknown>;settled:()=>Promise<void>}};
    let given='';
    internals.authorizeAdmission=async()=>({ever_execution_authorized:true});
    internals.adapter.start=async(_cmd,input)=>{given=input;return {run_id:c.run_id};};internals.adapter.settled=async()=>{};
    await start(f.host,c,pack(c));
    expect(given.startsWith(SIM_PREAMBLE)).toBe(true);expect(given).not.toContain('"schema_version"');
  });
  it('2. a real Host refuses the simulation pack whatever its context mode',async()=>{
    for(const [group,protocol] of [[undefined,undefined],['real',undefined],['real','nexloop.context-pack.v2']] as const){
      const f=host(group,protocol),c=command();c.runtime_profile='deterministic-test';
      await expect(start(f.host,c,pack({...c,world_id:SIM,mode:'simulation'}))).rejects.toMatchObject({code:'invalid_sim_context'});
    }
    expect(()=>host('real',CONTEXT_PROTOCOL_SIM)).toThrow('runtime configuration refused');  // never configured for it
  });
  it('4. no fallback to v6: a simulation Host cannot be configured for v6 (or none), and v6 validation refuses the sim pack',()=>{
    for(const protocol of ['nexloop.context-pack.v6','nexloop.context-pack.v2',undefined])expect(()=>host('simulation',protocol)).toThrow('runtime configuration refused');
    const c=simCommand();
    expect(()=>protocolModule.validateContextInput(JSON.stringify(pack(c)),c,{},protocolModule.CONTEXT_PROTOCOL_V6)).toThrow('runtime_context_invalid');
    expect(()=>protocolModule.validateContextInput(JSON.stringify(pack(c)),c,{})).toThrow('runtime_context_invalid');
  });
});
