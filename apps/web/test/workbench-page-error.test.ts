// @vitest-environment jsdom
// UX audit A1 (P0): the Action page went blank once the remediation list arrived (a hook after an early return), and with no
// error boundary the whole workbench went with it. Mounted for real (not static markup) so the pending -> loaded re-render runs.
import {createElement as h} from 'react';
import {act} from 'react';
import {createRoot,type Root} from 'react-dom/client';
import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
import {afterEach,beforeEach,expect,test,vi} from 'vitest';
import {Remediations} from '../src/workbench/pages/Actions';
import {PageErrorBoundary} from '../src/workbench/kit';

(globalThis as {IS_REACT_ACT_ENVIRONMENT?:boolean}).IS_REACT_ACT_ENVIRONMENT=true;
const row={remediation_id:'0b0c6c57-0000-4000-8000-000000000001',source_intent_id:'0b0c6c57-0000-4000-8000-000000000002',source_action:'synthetic.send',
  source_action_version:1,consumer_id:null,failure_kind:'confirmed_not_delivered',failed_at:'2026-10-23T08:00:00Z',rule_id:'synthetic_escalate',rule_version:1,
  kind:'escalate',mode:'manual',state:'proposed',due_by:'2026-10-30T08:00:00Z',attempts:0,last_code:'proposed',created_at:'2026-10-23T08:01:00Z',plan_id:null};
const json=(status:number,body:unknown)=>new Response(JSON.stringify(body),{status,headers:{'Content-Type':'application/json'}});

let root:Root;let box:HTMLDivElement;
beforeEach(()=>{box=document.createElement('div');document.body.append(box);root=createRoot(box);});
afterEach(()=>{act(()=>root.unmount());box.remove();vi.unstubAllGlobals();vi.restoreAllMocks();});
const flush=async()=>{for(let i=0;i<5;i++)await act(async()=>{await new Promise(r=>setTimeout(r,0));});};

test('the remediation block renders loading, then the list, without a hook-order crash',async()=>{
  let release:()=>void=()=>{};const gate=new Promise<void>(r=>{release=r;});
  vi.stubGlobal('fetch',vi.fn(async(path:string)=>{
    if(path.startsWith('/api/v1/workbench/remediations')){await gate;return json(200,{remediations:[row],hidden:0,paused:false,switch_revision:0,rate_capped:[],held_rules:[]});}
    return json(403,{});}));
  const errors=vi.spyOn(console,'error').mockImplementation(()=>{});
  const client=new QueryClient({defaultOptions:{queries:{retry:false}}});
  act(()=>root.render(h(QueryClientProvider,{client},h(Remediations))));
  expect(box.querySelector('[data-state="loading"]')).not.toBeNull();
  release();await flush();
  expect(box.textContent).toContain('待处理 1 条');
  expect(box.querySelector('table[aria-label="补救"]')).not.toBeNull();
  expect(errors.mock.calls.flat().join(' ')).not.toMatch(/hooks/i);
});

test('an unreadable remediation list says so and offers a retry',async()=>{
  vi.stubGlobal('fetch',vi.fn(async()=>json(503,{})));
  const client=new QueryClient({defaultOptions:{queries:{retry:false}}});
  act(()=>root.render(h(QueryClientProvider,{client},h(Remediations))));
  await flush();
  const state=box.querySelector('[data-state="unavailable"]');
  expect(state?.getAttribute('role')).toBe('alert');
  expect([...box.querySelectorAll('button')].map(b=>b.textContent)).toContain('重试');
});

test('a page that throws is replaced by an alert with a retry; its siblings stay',async()=>{
  vi.spyOn(console,'error').mockImplementation(()=>{});
  let broken=true;
  function Page(){if(broken)throw new Error('synthetic render failure');return h('p',null,'页面内容');}
  act(()=>root.render(h('div',null,h('nav',{'aria-label':'工作台导航'},'导航'),h(PageErrorBoundary,null,h(Page)))));
  expect(box.querySelector('nav')?.textContent).toBe('导航');
  const alert=box.querySelector('[data-state="page-error"]');
  expect(alert?.getAttribute('role')).toBe('alert');
  expect(box.querySelector('h1')?.textContent).toBe('此页暂时无法显示');
  expect(box.querySelector('details')?.textContent).toContain('synthetic render failure');
  broken=false;
  const retry=[...box.querySelectorAll('button')].find(b=>b.textContent==='重试')!;
  act(()=>retry.click());
  expect(box.textContent).toContain('页面内容');
  expect(box.querySelector('[data-state="page-error"]')).toBeNull();
});
