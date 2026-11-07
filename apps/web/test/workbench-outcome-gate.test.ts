// @vitest-environment jsdom
// NX-061 B5 (migration 0418): the metrics snapshot's outcomes gate on the 运行状态 page. measured shows the gated failure rate and the
// sample count; insufficient_data says 样本不足（N/10），未判定 and never a rate or 正常; no block, or an older block without gate, is 无数据.
import {createElement as h,act} from 'react';
import {createRoot,type Root} from 'react-dom/client';
import {afterEach,beforeEach,expect,test} from 'vitest';
import {outcomeGate} from '../src/workbench/api';
import {OutcomesCard} from '../src/workbench/pages/Observability';

(globalThis as {IS_REACT_ACT_ENVIRONMENT?:boolean}).IS_REACT_ACT_ENVIRONMENT=true;
let root:Root;let box:HTMLDivElement;
beforeEach(()=>{box=document.createElement('div');document.body.append(box);root=createRoot(box);});
afterEach(()=>{act(()=>root.unmount());box.remove();});
const older={window_seconds:3600,classes:{succeeded:3},decided:3,failures:0,failure_rate:0,failure_rate_counting_refusals:0,unresolved:0};
const block=(decided:number,rate:number)=>({...older,decided,failure_rate:rate,min_decided:10,failure_rate_gated:decided>=10?rate:null,
  gate:decided>=10?'measured':'insufficient_data',insufficient_data:decided>=10?0:1});
const show=(value:unknown)=>{act(()=>root.render(h(OutcomesCard,{outcomes:outcomeGate(value)})));return box.querySelector('[data-gate]')!;};

test('parser: the gate as served; no block or an older block without gate is null; a malformed block is invalid, never thrown',()=>{
  expect(outcomeGate(block(12,0.25))).toEqual({gate:'measured',decided:12,min_decided:10,failure_rate:0.25});
  expect(outcomeGate(block(3,0))).toEqual({gate:'insufficient_data',decided:3,min_decided:10});
  expect(outcomeGate(undefined)).toBeNull();expect(outcomeGate(older)).toBeNull();
  expect(outcomeGate({...block(12,0.25),failure_rate_gated:null})).toEqual({gate:'invalid'});
  expect(outcomeGate({...block(3,0),gate:'within'})).toEqual({gate:'invalid'});expect(outcomeGate('x')).toEqual({gate:'invalid'});
});

test('measured shows the rate and samples; insufficient data is not judged and shows no rate; no data says 无数据',()=>{
  const m=show(block(12,0.25));expect(m.getAttribute('data-gate')).toBe('measured');expect(m.textContent).toBe('失败率 25.0%，样本 12 个');
  const i=show(block(3,0));expect(i.getAttribute('data-gate')).toBe('insufficient_data');expect(i.textContent).toBe('样本不足（3/10），未判定');
  expect(box.textContent).not.toContain('%');expect(box.textContent).not.toContain('正常');
  expect(show(undefined).textContent).toBe('无数据');expect(show(older).textContent).toBe('无数据');expect(box.textContent).not.toContain('%');
});

test('an invalid block says 格式无法识别',()=>{
  const v=show({...block(3,0),gate:'within'});expect(v.getAttribute('data-gate')).toBe('invalid');expect(v.textContent).toBe('格式无法识别');
});
