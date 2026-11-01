// @vitest-environment jsdom
// F14 (K4g): a tab or filter change is URL state of the same page, not a new page: the page is not re-mounted, so after the next list
// has loaded the focus is still on the selected tab and the arrow keys, Home and End keep moving the selection.
import {createElement as h,act} from 'react';
import {createRoot,type Root} from 'react-dom/client';
import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
import {afterEach,beforeEach,expect,test,vi} from 'vitest';
import {WorkbenchApp} from '../src/workbench/WorkbenchApp';

(globalThis as {IS_REACT_ACT_ENVIRONMENT?:boolean}).IS_REACT_ACT_ENVIRONMENT=true;
const json=(status:number,body:unknown)=>new Response(JSON.stringify(body),{status,headers:{'Content-Type':'application/json'}});
let root:Root;let box:HTMLDivElement;
beforeEach(()=>{window.history.replaceState(null,'','/workbench/commitments');
  vi.stubGlobal('fetch',vi.fn(async(path:string)=>path==='/api/v1/workbench/auth/session'
    ?json(200,{authenticated:true,tenant_id:'synthetic-tenant',principal_id:'synthetic-owner',restricted:false})
    :path.startsWith('/api/v1/workbench/commitments?')?json(200,{commitments:[],exceptions:[],counts:{open:0,ended:0,exceptions_in_scope:0,exceptions_all:0},total:0,next_cursor:null})
    :json(503,{})));
  box=document.createElement('div');document.body.append(box);root=createRoot(box);});
afterEach(()=>{act(()=>root.unmount());box.remove();vi.unstubAllGlobals();});
const settle=async()=>{for(let i=0;i<8;i++)await act(async()=>{await new Promise(r=>setTimeout(r,20));});};
const tabs=()=>[...box.querySelectorAll<HTMLButtonElement>('[role=tablist][aria-label=承诺范围] [role=tab]')];
const selected=()=>tabs().find(t=>t.getAttribute('aria-selected')==='true')!;
const press=async(key:string)=>{act(()=>{(document.activeElement as HTMLElement).dispatchEvent(new KeyboardEvent('keydown',{key,bubbles:true}));});await settle();};

test('F14: after the next list loads the focus is still on the selected tab, in the same tab list',async()=>{
  act(()=>root.render(h(QueryClientProvider,{client:new QueryClient({defaultOptions:{queries:{retry:false}}})},h(WorkbenchApp,{slots:{}}))));await settle();
  const list=box.querySelector('[role=tablist][aria-label=承诺范围]')!;
  selected().focus();expect(selected().textContent).toContain('未结束');
  await press('ArrowRight');
  expect(window.location.search).toBe('?status=ended');expect(selected().textContent).toContain('已结束');
  expect(document.activeElement).toBe(selected());expect(box.querySelector('[role=tablist][aria-label=承诺范围]')).toBe(list);
  await press('End');expect(selected().textContent).toBe('全部');expect(document.activeElement).toBe(selected());
  await press('Home');expect(selected().textContent).toContain('未结束');expect(document.activeElement).toBe(selected());
  expect(box.querySelector('[role=tablist][aria-label=承诺范围]')).toBe(list);
});
