// @vitest-environment jsdom
// K4g G1: the page boundary is keyed by page and object only (F14), so it clears an error itself when the route's filters change;
// without an error a filter change re-mounts nothing. Pausing remediation is one confirmation with an optional reason; resuming
// still needs one.
import {createElement as h,act,useRef} from 'react';
import {createRoot,type Root} from 'react-dom/client';
import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
import {afterEach,beforeEach,expect,test,vi} from 'vitest';
import {PageErrorBoundary} from '../src/workbench/kit';
import {PAUSE_DEFAULT_REASON,RemediationSwitchForm} from '../src/workbench/actions';
import {readFileSync,readdirSync,statSync} from 'node:fs';
import {join,resolve} from 'node:path';
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

const settle=async()=>{for(let i=0;i<6;i++)await act(async()=>{await new Promise(r=>setTimeout(r,10));});};
test('K4i: an empty pause reason is sent as PAUSE_DEFAULT_REASON, the panel says so, and it is the only default reason in the source',async()=>{
  const bodies:unknown[]=[];
  vi.stubGlobal('fetch',vi.fn(async(path:string,init?:RequestInit)=>{
    if(path==='/api/v1/workbench/auth/csrf')return new Response(JSON.stringify({csrf_token:'synthetic-token'}),{status:200});
    if(path==='/api/v1/workbench/actions/switch_remediation'){bodies.push(JSON.parse(String(init!.body)));return new Response(JSON.stringify({status:'executed'}),{status:200});}
    return new Response('{}',{status:503});}));
  mountSwitch(false);open();
  expect(box.querySelector('[data-default-reason]')!.textContent).toBe(`留空时记录为“${PAUSE_DEFAULT_REASON}”。`);
  act(()=>submit().click());await settle();
  expect(bodies).toHaveLength(1);expect(JSON.stringify(bodies[0])).toContain(JSON.stringify(PAUSE_DEFAULT_REASON));
  vi.unstubAllGlobals();
  // The text itself appears once in the source: the constant.
  const files=(d:string):string[]=>readdirSync(d).flatMap(f=>{const x=join(d,f);return statSync(x).isDirectory()?files(x):/\.tsx?$/.test(f)?[x]:[];});
  const hits=files(resolve(__dirname,'../src')).filter(f=>readFileSync(f,'utf8').includes(PAUSE_DEFAULT_REASON));
  expect(hits.map(f=>f.replace(/.*\/src\//,''))).toEqual(['workbench/actions.tsx']);
  expect(readFileSync(resolve(__dirname,'../src/workbench/actions.tsx'),'utf8').split(PAUSE_DEFAULT_REASON).length-1).toBe(1);
  expect(PAUSE_DEFAULT_REASON).toBe('暂停补救（未填写原因）');
});
