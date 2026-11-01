// @vitest-environment jsdom
// K4f (owner Q-3, Q-A–Q-E 2026-11-01): 待我处理 is the workbench home and reads GET /pending alone; the former 总览 is its 健康 tab
// (GET /overview); /workbench/overview still opens it under the new address without an extra history entry (L6 R1/R2).
import {readFileSync} from 'node:fs';
import {resolve} from 'node:path';
import {createElement as h,act} from 'react';
import {createRoot,type Root} from 'react-dom/client';
import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
import {afterEach,beforeEach,expect,test,vi} from 'vitest';
import {NAV_GROUPS,WorkbenchApp,pageTitle} from '../src/workbench/WorkbenchApp';
import {PAGES,parseRoute,routePath,type Route} from '../src/workbench/route';

(globalThis as {IS_REACT_ACT_ENVIRONMENT?:boolean}).IS_REACT_ACT_ENVIRONMENT=true;
const example=JSON.parse(readFileSync(resolve(__dirname,'../../../packages/contracts/examples/workbench-pending.valid.json'),'utf8'));
const CANDIDATE='0b6c1e2a-3d4f-4a5b-8c6d-7e8f9a0b1c2d';
// What the server returns a reviewer: only candidate definitions (it may act on nothing else).
const reviewerPending={...example,questions:[],categories:example.categories.map((c:{category:string})=>c.category==='candidate_review'
  ?{category:c.category,count:2,oldest_waiting_seconds:7200,oldest_ref:'candidate:'+CANDIDATE}:{category:c.category,count:0,oldest_waiting_seconds:null,oldest_ref:null})};
const json=(status:number,body:unknown)=>new Response(JSON.stringify(body),{status,headers:{'Content-Type':'application/json'}});
let calls:string[]=[];let root:Root;let box:HTMLDivElement;
beforeEach(()=>{calls=[];
  vi.stubGlobal('fetch',vi.fn(async(path:string)=>{calls.push(path);
    if(path==='/api/v1/workbench/auth/session')return json(200,{authenticated:true,tenant_id:'synthetic-tenant',principal_id:'synthetic-reviewer',restricted:false});
    if(path==='/api/v1/workbench/pending')return json(200,reviewerPending);
    if(path==='/api/v1/workbench/overview')return json(403,{code:'forbidden'});
    return json(503,{});}));
  box=document.createElement('div');document.body.append(box);root=createRoot(box);});
afterEach(()=>{act(()=>root.unmount());box.remove();vi.unstubAllGlobals();});
const settle=async()=>{for(let i=0;i<8;i++)await act(async()=>{await new Promise(r=>setTimeout(r,20));});};
async function mount(path:string){window.history.replaceState(null,'',path);
  act(()=>root.render(h(QueryClientProvider,{client:new QueryClient({defaultOptions:{queries:{retry:false}}})},h(WorkbenchApp,{slots:{}}))));await settle();}

test('routes: home is the default, the tab is URL state, the old 总览 path maps to the 健康 tab',()=>{
  expect(PAGES.home).toBe('待我处理');expect('overview' in PAGES).toBe(false);
  for(const p of ['/workbench','/workbench/','/workbench/home','/workbench/unknown'])expect(parseRoute(p)).toEqual({page:'home'});
  expect(parseRoute('/workbench/overview')).toEqual({page:'home',query:{tab:'health'}});
  expect(parseRoute('/workbench/home','?tab=bogus')).toEqual({page:'home'});
  for(const r of [{page:'home'},{page:'home',query:{tab:'health'}}] as Route[])expect(parseRoute(...(routePath(r).split('?').map((x,i)=>i?'?'+x:x) as [string,string?]))).toEqual(r);
  expect(routePath({page:'home',query:{tab:'health'}})).toBe('/workbench/home?tab=health');
  expect(NAV_GROUPS[0].pages[0]).toBe('home');expect(NAV_GROUPS.flatMap(g=>g.pages)).not.toContain('overview');
  expect(pageTitle({page:'home'})).toBe('待我处理 · 负责人工作台 · NexLoop');
});

test('L6 R2: with the overview refused (403), the reviewer still sees its candidate definitions; only /pending is read',async()=>{
  await mount('/workbench');
  expect(box.querySelector('.workbench-page h1')!.textContent).toBe('待我处理');
  const row=box.querySelector('[data-category=candidate_review]')!;expect(row.textContent).toContain('待审核候选定义');
  expect(row.querySelector('a')!.getAttribute('href')).toBe('/workbench/knowledge/'+CANDIDATE);
  expect(calls).toContain('/api/v1/workbench/pending');expect(calls).not.toContain('/api/v1/workbench/overview');
  expect(box.querySelector('[role=tab][aria-selected=true]')!.textContent).toBe('待我处理');
});

test('the 健康 tab reads /overview alone and is switched in place; a refusal there stays on that tab',async()=>{
  await mount('/workbench/home');
  const before=window.history.length;
  act(()=>[...box.querySelectorAll<HTMLButtonElement>('[role=tab]')].find(b=>b.textContent==='健康')!.click());await settle();
  expect(window.location.pathname+window.location.search).toBe('/workbench/home?tab=health');expect(window.history.length).toBe(before);
  expect(calls).toContain('/api/v1/workbench/overview');
  expect(box.querySelector('[data-category]')).toBeNull();expect(box.querySelector('[role=alert]')).not.toBeNull();
});

test('an old /workbench/overview bookmark opens the 健康 tab under the new address, with no extra history entry',async()=>{
  const before=window.history.length;
  await mount('/workbench/overview');
  expect(window.location.pathname+window.location.search).toBe('/workbench/home?tab=health');expect(window.history.length).toBe(before);
  expect(box.querySelector('[role=tab][aria-selected=true]')!.textContent).toBe('健康');
  expect(calls).not.toContain('/api/v1/workbench/pending');
});
