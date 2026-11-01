// @vitest-environment jsdom
// K4g G1: the page boundary is keyed by page and object only (F14), so it clears an error itself when the route's filters change;
// without an error a filter change re-mounts nothing. Pausing remediation is one confirmation with an optional reason; resuming
// still needs one.
import {createElement as h,act,useRef} from 'react';
import {createRoot,type Root} from 'react-dom/client';
import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
import {afterEach,beforeEach,expect,test,vi} from 'vitest';
import {PageErrorBoundary} from '../src/workbench/kit';
import {RemediationSwitchForm} from '../src/workbench/actions';
import {me,OPERATION_ACTION} from '../src/workbench/api';

(globalThis as {IS_REACT_ACT_ENVIRONMENT?:boolean}).IS_REACT_ACT_ENVIRONMENT=true;
let root:Root;let box:HTMLDivElement;
beforeEach(()=>{box=document.createElement('div');document.body.append(box);root=createRoot(box);vi.spyOn(console,'error').mockImplementation(()=>{});});
afterEach(()=>{act(()=>root.unmount());box.remove();vi.restoreAllMocks();});

let mounts=0;
function Filtered({filter}:{filter:string}){const first=useRef(true);if(first.current){first.current=false;mounts++;}
  if(filter==='broken')throw new Error('synthetic render failure');return h('p',{id:'rows'},'rows for '+filter);}
const page=(filter:string)=>act(()=>root.render(h(PageErrorBoundary,{key:'/workbench/commitments',resetKey:'/workbench/commitments?status='+filter},h(Filtered,{filter}))));

test('an error under one filter is cleared when the filter changes; without an error nothing is re-mounted',()=>{
  mounts=0;page('open');expect(box.querySelector('#rows')!.textContent).toBe('rows for open');
  page('ended');expect(box.querySelector('#rows')!.textContent).toBe('rows for ended');expect(mounts).toBe(1);  // F14: same page, not re-mounted
  page('broken');expect(box.querySelector('[data-page-error]')).not.toBeNull();expect(box.querySelector('#rows')).toBeNull();
  page('all');expect(box.querySelector('[data-page-error]')).toBeNull();expect(box.querySelector('#rows')!.textContent).toBe('rows for all');
});

const owner=me({principal_id:'synthetic-owner',role:'owner',actions:[OPERATION_ACTION.switch_remediation]});
const mountSwitch=(paused:boolean)=>act(()=>root.render(h(QueryClientProvider,{client:new QueryClient()},h(RemediationSwitchForm,{member:owner,paused}))));
const open=()=>act(()=>box.querySelector<HTMLButtonElement>('.confirm-trigger')!.click());
const submit=()=>box.querySelector<HTMLButtonElement>('.confirm-panel button[type=submit]')!;

test('pausing remediation: one confirmation, the reason optional; resuming: the reason required',()=>{
  mountSwitch(false);expect(box.querySelector('textarea')).toBeNull();open();
  expect(box.querySelector('label')!.textContent).toContain('（选填）');expect(box.querySelector('textarea')!.required).toBe(false);
  expect(submit().disabled).toBe(false);
  act(()=>root.unmount());root=createRoot(box);mountSwitch(true);open();
  expect(box.querySelector('textarea')!.required).toBe(true);expect(submit().disabled).toBe(true);
});
