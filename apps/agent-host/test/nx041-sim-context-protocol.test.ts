/**
 * NX-041 S7b review 3.4: the acceptances the Host's simulation Context input protocol must meet before a simulation Run reads the
 * sim pack (nexloop.context-pack.sim.v1). The protocol is not built yet (it waits for D10): each acceptance is an `it.fails` - the
 * strict xfail of vitest - so it is executed today and, once the protocol exists and meets it, the test turns red and must become a
 * plain test. The same condition is pi_run_blockers' Host prerequisite (tests/test_nx041_s7_pg.py: the Host reads
 * nexloop.context-pack.sim.v1).
 *
 * Expected exports of src/context-input.ts (names fixed here; the protocol task implements them):
 *   contextProtocolAccepts(group:'real'|'simulation', schemaVersion:string):boolean
 *   renderSimulationContext(pack:object):string   - the text the model sees
 */
import {readFileSync} from 'node:fs';
import {describe,it,expect} from 'vitest';

const SIM_PACK=JSON.parse(readFileSync(new URL('../../../packages/contracts/examples/context-pack-sim.valid.json',import.meta.url),'utf8'));
const V6='nexloop.context-pack.v6',SIM='nexloop.context-pack.sim.v1';
// eslint-disable-next-line @typescript-eslint/no-explicit-any
const protocol=async()=>await import('../src/context-input.ts') as any;
const PREFIX='【模拟环境】以下均为模拟输入，不是真实客户或真实事实。';

describe('NX-041 S7b: simulation Context input protocol (pending: D10)',()=>{
  it.fails('a simulation-group Host accepts the sim pack and nothing else',async()=>{
    const m=await protocol();
    expect(m.contextProtocolAccepts('simulation',SIM)).toBe(true);
    expect(m.contextProtocolAccepts('simulation',V6)).toBe(false);
  });
  it.fails('a real-group Host refuses the sim pack (it keeps v6)',async()=>{
    const m=await protocol();
    expect(m.contextProtocolAccepts('real',SIM)).toBe(false);
    expect(m.contextProtocolAccepts('real',V6)).toBe(true);
  });
  it.fails('the sim pack is rendered to the model behind the fixed simulation prefix',async()=>{
    const m=await protocol();
    const text=m.renderSimulationContext(SIM_PACK) as string;
    expect(text.startsWith(PREFIX)).toBe(true);
    expect(text).toContain(SIM_PACK.evidence[0].content.text);
  });
  it.fails('a sim pack never falls back to v6 (an unreadable sim pack is refused, not replaced)',async()=>{
    const m=await protocol();
    expect(typeof m.renderSimulationContext).toBe('function');  // the refusal must come from the protocol, not from its absence
    expect(()=>m.renderSimulationContext({...SIM_PACK,schema_version:V6})).toThrow();
    expect(()=>m.renderSimulationContext({...SIM_PACK,evidence:'not a list'})).toThrow();
  });
  it('today: the Host does not read the sim pack yet (the Host prerequisite of pi_run_blockers)',()=>{
    const sources=['context-input.ts','runtime-host.ts','pi-runtime-adapter.ts'].map(f=>readFileSync(new URL('../src/'+f,import.meta.url),'utf8')).join('\n');
    expect(sources.includes(SIM)).toBe(false);
  });
});
