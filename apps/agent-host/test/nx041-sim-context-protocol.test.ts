/**
 * NX-041 S7b review 3.4: the acceptances the Host's simulation Context input protocol must meet before a simulation Run reads the
 * sim pack (nexloop.context-pack.sim.v1). Written as `it.fails` while the protocol was pending; at integration L2's protocol
 * (src/sim-context.ts, re-exported by src/context-input.ts) is in, so they are plain tests against its API:
 *   parseSimContextPack(input:string, command):SimContextPack   - strict; throws on anything but a valid sim pack for this Run
 *   declaresSimContextPack(input):boolean                       - a real Host refuses such an input outright
 *   renderSimContextPack(pack):string                            - the text the model sees, behind SIM_PREAMBLE
 * The same condition is pi_run_blockers' Host prerequisite (tests/test_nx041_s7_pg.py: the Host has parseSimContextPack).
 */
import {readFileSync} from 'node:fs';
import {describe,it,expect} from 'vitest';
import {parseSimContextPack,declaresSimContextPack,renderSimContextPack} from '../src/context-input.ts';
import {SIM_PREAMBLE} from '../src/sim-context.ts';

const EXAMPLE=JSON.parse(readFileSync(new URL('../../../packages/contracts/examples/context-pack-sim.valid.json',import.meta.url),'utf8'));
// Integration: the contract example reports events_total 2 / events_included 1 under event_limit 20, which L2's consistency check
// (review 4.1: included = min(total, limit)) refuses; the protocol is tested with the example made consistent (one event, none omitted).
const SIM_PACK={...EXAMPLE,budget_report:{event_limit:20,events_total:1,events_included:1,omitted:0},insufficient:[]};
const V6='nexloop.context-pack.v6',SIM='nexloop.context-pack.sim.v1';
// L2 review 4.1: the pack is this Run's Consumer's (consumer_ref).
const COMMAND={world_id:SIM_PACK.world,mode:String(SIM_PACK.world).startsWith('sim:')?'simulation':'shadow',tenant_id:SIM_PACK.tenant_id,consumer_ref:'consumer:'+SIM_PACK.consumer_id};
// eslint-disable-next-line @typescript-eslint/no-explicit-any
const parse=(pack:unknown)=>parseSimContextPack(JSON.stringify(pack),COMMAND as any);

describe('NX-041 S7b: simulation Context input protocol',()=>{
  it('a simulation-group Host accepts the sim pack and nothing else',()=>{
    expect(parse(SIM_PACK).schema_version).toBe(SIM);
    expect(()=>parse({...SIM_PACK,schema_version:V6})).toThrow();
  });
  it('a real-group Host refuses the sim pack (it keeps v6)',()=>{
    expect(declaresSimContextPack(JSON.stringify(SIM_PACK))).toBe(true);
    expect(declaresSimContextPack(JSON.stringify({...SIM_PACK,schema_version:V6}))).toBe(false);
  });
  it('the sim pack is rendered to the model behind the fixed simulation prefix',()=>{
    const text=renderSimContextPack(parse(SIM_PACK));
    expect(text.startsWith(SIM_PREAMBLE)).toBe(true);
    expect(text).toContain(JSON.stringify(SIM_PACK.evidence[0].content));
  });
  it('a sim pack never falls back to v6 (an unreadable sim pack is refused, not replaced)',()=>{
    expect(()=>parse({...SIM_PACK,schema_version:V6})).toThrow();
    expect(()=>parse({...SIM_PACK,evidence:'not a list'})).toThrow();
  });
  it('the Host reads the sim pack (the Host prerequisite of pi_run_blockers is met)',()=>{
    const sources=['context-input.ts','runtime-host.ts','sim-context.ts'].map(f=>readFileSync(new URL('../src/'+f,import.meta.url),'utf8')).join('\n');
    expect(sources.includes(SIM)&&sources.includes('parseSimContextPack')).toBe(true);
  });
});
