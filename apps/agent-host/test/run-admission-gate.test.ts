/** ADR-022 §4: bounded global active-Run limit of the Agent Host. */
import {afterEach,describe,it,expect,vi} from 'vitest';
import {mkdtempSync,writeFileSync,chmodSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';import {join} from 'node:path';
import {RunAdmissionGate,validateAdmissionConfiguration,DEFAULT_MAXIMUM_ACTIVE_RUNS} from '../src/run-admission-gate.js';
import {RuntimeHost} from '../src/runtime-host.js';import {readPrivateMaterial} from '../src/private-material.js';

const far=()=>Date.now()+60_000;
afterEach(()=>{vi.useRealTimers();});

describe('RunAdmissionGate',()=>{
 it('admits up to the limit immediately; the fifth Run waits and is admitted when one ends',async()=>{
  const gate=new RunAdmissionGate(4,5_000);
  const slots=await Promise.all(['a','b','c','d'].map(run=>gate.acquire(run,far())));
  expect(slots.every(slot=>slot!==undefined)).toBe(true);expect(gate.activeRuns).toBe(4);
  let fifth:unknown='pending';const waiting=gate.acquire('e',far()).then(slot=>{fifth=slot;return slot;});
  await new Promise(resolve=>setTimeout(resolve,50));
  expect(fifth).toBe('pending');expect(gate.waiting).toBe(1);
  slots[0]!.release();const admitted=await waiting;
  expect(admitted).toBeDefined();expect(gate.activeRuns).toBe(4);expect(gate.waiting).toBe(0);
  slots[0]!.release();expect(gate.activeRuns).toBe(4);  // a release is effective once
 });
 it('rejects with the explicit retryable code when the bounded wait expires, and leaves the queue',async()=>{
  const gate=new RunAdmissionGate(1,80);const held=await gate.acquire('a',far());
  const started=Date.now();
  await expect(gate.acquire('b',far())).rejects.toMatchObject({code:'runtime_capacity_exhausted'});
  expect(Date.now()-started).toBeGreaterThanOrEqual(70);expect(gate.waiting).toBe(0);
  held!.release();expect(await gate.acquire('b',far())).toBeDefined();
 });
 it('never waits past the command deadline and refuses an already-expired one at once',async()=>{
  const gate=new RunAdmissionGate(1,10_000);await gate.acquire('a',far());
  const started=Date.now();
  await expect(gate.acquire('b',Date.now()+60)).rejects.toMatchObject({code:'runtime_capacity_exhausted'});
  expect(Date.now()-started).toBeLessThan(1_000);
  await expect(gate.acquire('c',Date.now()-1)).rejects.toMatchObject({code:'runtime_capacity_exhausted'});
 });
 it('admits waiters first-in first-out and a Run already holding a slot does not take another',async()=>{
  const gate=new RunAdmissionGate(1,5_000);const first=await gate.acquire('a',far());
  expect(await gate.acquire('a',far())).toBeUndefined();
  const order:string[]=[];
  const b=gate.acquire('b',far()).then(slot=>{order.push('b');return slot;});
  const c=gate.acquire('c',far()).then(slot=>{order.push('c');return slot;});
  first!.release();(await b)!.release();await c;
  expect(order).toEqual(['b','c']);
 });
 it('wait 0 rejects at once when full',async()=>{
  const gate=new RunAdmissionGate(1,0);await gate.acquire('a',far());
  await expect(gate.acquire('b',far())).rejects.toMatchObject({code:'runtime_capacity_exhausted'});
 });
 it('configuration: defaults, bounds and types',()=>{
  expect(validateAdmissionConfiguration(undefined,undefined)).toEqual({maximum:DEFAULT_MAXIMUM_ACTIVE_RUNS,waitMs:500});
  expect(validateAdmissionConfiguration(4,0)).toEqual({maximum:4,waitMs:0});
  for(const [maximum,wait] of [[0,500],[65,500],[2.5,500],['4',500],[null,500],[4,-1],[4,30001],[4,'500'],[4,1.5]])
   expect(()=>validateAdmissionConfiguration(maximum,wait)).toThrow('runtime configuration refused');
 });
});

describe('RuntimeHost refuses an invalid active-Run limit at startup',()=>{
 const roots:string[]=[];
 afterEach(()=>{for(const root of roots.splice(0))rmSync(root,{recursive:true});});
 function open(extra:Record<string,unknown>){
  const root=mkdtempSync(join(tmpdir(),'nexloop-host-admission-'));chmodSync(root,0o700);roots.push(root);
  const file=(name:string,value:string)=>{const path=join(root,name);writeFileSync(path,value,{mode:0o600});return path;};
  const config=file('runtime-config',JSON.stringify({guard_ca_file:file('guard-ca','synthetic-not-used-ca'),guard_key_file:file('guard-key','a'.repeat(64)),
   guard_url:'https://127.0.0.1:65431/internal/v1/runtime/authorize',runtime_profile:'deterministic-test',...extra}));
  return new RuntimeHost(root,config,readPrivateMaterial,()=>{});
 }
 it('accepts the default and an explicit valid limit',async()=>{
  for(const extra of [{},{maximum_active_runs:4,run_admission_wait_ms:500},{maximum_active_runs:1,run_admission_wait_ms:0}]){const host=open(extra);await host.close();}
 });
 it('fails on out-of-range or mistyped values',()=>{
  for(const extra of [{maximum_active_runs:0},{maximum_active_runs:65},{maximum_active_runs:'4'},{maximum_active_runs:4.5},{run_admission_wait_ms:-1},{run_admission_wait_ms:30001},{run_admission_wait_ms:'0'}])
   expect(()=>open(extra)).toThrow('runtime configuration refused');
 });
});

describe('NX-030 admission metrics',()=>{
 it('counts admissions, waits and timeouts as numbers only',async()=>{
  const {admissionMetrics}=await import('../src/run-admission-gate.js');
  const before=admissionMetrics();
  const gate=new RunAdmissionGate(1,20);
  const slot=await gate.acquire('metrics-a',far());
  const during=admissionMetrics();
  expect(during.available).toBe(true);expect(during.runs_started-before.runs_started).toBe(1);
  expect(during.active_runs-before.active_runs).toBe(1);expect(during.max_active_runs-before.max_active_runs).toBe(1);
  await expect(gate.acquire('metrics-b',far())).rejects.toMatchObject({code:'runtime_capacity_exhausted'});
  const after=admissionMetrics();
  expect(after.admission_timeouts-before.admission_timeouts).toBe(1);
  slot!.release();expect(admissionMetrics().active_runs-before.active_runs).toBe(0);
  expect(Object.values(admissionMetrics()).every(value=>typeof value==='number'||typeof value==='boolean')).toBe(true);
 });
});
