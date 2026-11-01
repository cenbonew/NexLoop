// @vitest-environment jsdom
// Owner S73 (2026-10-31), S80 after walkthrough R64: 管理 › 关联系统入口 lists read-only links to the same-origin pages (WebChat,
// readiness) built from the page's origin, and the reference endpoint staff page only when the deployment gives its URL
// (GET /api/v1/workbench/entries). The workbench pages are the sidebar's and are not repeated. Every link opens in a new window.
import {createElement as h,act} from 'react';
import {createRoot,type Root} from 'react-dom/client';
import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
import {afterEach,beforeEach,expect,test} from 'vitest';
import {entryLinks,me} from '../src/workbench/api';
import {Entries,originEntries} from '../src/workbench/pages/Entries';
import {NAV_GROUPS,navOffered} from '../src/workbench/WorkbenchApp';
import {PAGES,parseRoute,routePath} from '../src/workbench/route';

(globalThis as {IS_REACT_ACT_ENVIRONMENT?:boolean}).IS_REACT_ACT_ENVIRONMENT=true;
const operator=me({principal_id:'synthetic-op',role:'operator',actions:['eios:action:nexloop.workbench.read:1']});
const ORIGIN='https://gateway.example.test:9443';
const STAFF='https://endpoint.example.test:9444/staff';
let root:Root;let box:HTMLDivElement;
beforeEach(()=>{box=document.createElement('div');document.body.append(box);root=createRoot(box);});
afterEach(()=>{act(()=>root.unmount());box.remove();});
const mount=(staff:string|null=null)=>{const c=new QueryClient({defaultOptions:{queries:{retry:false,staleTime:Infinity}}});
  c.setQueryData(['workbench','entries'],entryLinks({reference_endpoint_staff_url:staff}));
  act(()=>root.render(h(QueryClientProvider,{client:c},h(Entries,{origin:ORIGIN}))));};
const links=()=>[...box.querySelectorAll<HTMLAnchorElement>('a')];

test('S80: 管理 ends 实验空间 · 译名管理 · 关联系统入口; the paths stay /experiments, /glossary, /entries',()=>{
  expect(PAGES.entries).toBe('关联系统入口');expect(PAGES.glossary).toBe('译名管理');
  expect(NAV_GROUPS.find(g=>g.title==='管理')!.pages.slice(-3)).toEqual(['experiments','glossary','entries']);
  expect(routePath({page:'entries'})).toBe('/workbench/entries');expect(parseRoute('/workbench/entries')).toEqual({page:'entries'});
  expect(routePath({page:'glossary'})).toBe('/workbench/glossary');
  expect(navOffered('entries',operator)).toBe(true);
});

test('only the same-origin and external groups, no workbench page repeated, every link in a new window',()=>{
  mount();
  expect(box.querySelector('h1')!.textContent).toBe('关联系统入口');
  expect([...box.querySelectorAll('h2')].map(x=>x.textContent)).toEqual(['同一网关的其他页面']);
  expect(box.textContent).toContain('列出同一网关的其他页面与外部系统的入口');
  expect(links().some(a=>a.getAttribute('href')!.includes('/workbench'))).toBe(false);
  expect(links().map(a=>a.href)).toEqual(originEntries(ORIGIN).map(e=>e.href));expect(links()[0].href).toBe(ORIGIN+'/');
  for(const a of links()){expect(a.target).toBe('_blank');expect(a.rel).toBe('noopener noreferrer');expect(a.textContent).toContain('新窗口打开');}
  // No URL from the deployment: no external group.
  expect(box.textContent).not.toContain('参考端点');
  expect(box.querySelectorAll('button,form,input,select').length).toBe(0);
});

test('the reference endpoint staff page is linked under 外部系统 when the deployment gives its URL',()=>{
  mount(STAFF);
  expect([...box.querySelectorAll('h2')].map(x=>x.textContent)).toEqual(['同一网关的其他页面','外部系统']);
  const staff=links().find(a=>a.href===STAFF)!;
  expect(staff.textContent).toContain('参考端点 · 员工页面');expect(staff.target).toBe('_blank');expect(staff.rel).toBe('noopener noreferrer');
});

test('only a plain HTTPS URL from the server becomes a link',()=>{
  for(const bad of ['http://endpoint.example.test/staff','javascript:alert(1)','https://u:p@endpoint.example.test/staff','https://endpoint.example.test/staff?x=1','not a url',null])
    expect(entryLinks({reference_endpoint_staff_url:bad}).reference_endpoint_staff_url).toBeNull();
  expect(entryLinks({reference_endpoint_staff_url:STAFF}).reference_endpoint_staff_url).toBe(STAFF);
});

// S81 / ADR-043: the entry ticket buttons (owner only on the server; offered targets from GET /entries).
import {entryTicket} from '../src/workbench/api';
import {enterWithTicket} from '../src/workbench/pages/Entries';
import {vi} from 'vitest';
const TICKET='A'.repeat(43);

test('S81: a button per offered target, none when the deployment offers none',()=>{
  const c=new QueryClient({defaultOptions:{queries:{retry:false,staleTime:Infinity}}});
  c.setQueryData(['workbench','entries'],entryLinks({reference_endpoint_staff_url:STAFF,entry_tickets:['webchat','reference_staff','other']}));
  act(()=>root.render(h(QueryClientProvider,{client:c},h(Entries,{origin:ORIGIN}))));
  expect([...box.querySelectorAll('button')].map(b=>b.textContent)).toEqual(['以测试客户身份直接进入 WebChat','以只读测试员工身份直接进入员工页面']);
  expect(entryLinks({entry_tickets:'webchat'}).entry_tickets).toEqual([]);
});

test('S81: only a well-formed answer for the asked target is used',()=>{
  expect(entryTicket({target:'webchat',action:'/api/v1/auth/entry',ticket:TICKET},'webchat')).toEqual({action:'/api/v1/auth/entry',ticket:TICKET});
  for(const bad of [{target:'webchat',action:'https://evil.example.test/api/v1/auth/entry',ticket:TICKET},{target:'webchat',action:'/api/v1/auth/entry',ticket:'short'},
    {target:'reference_staff',action:'/api/v1/auth/entry',ticket:TICKET},{target:'webchat',action:'/api/v1/auth/entry?ticket=x',ticket:TICKET}])
    expect(()=>entryTicket(bad,'webchat')).toThrow();
  expect(entryTicket({target:'reference_staff',action:'https://endpoint.example.test:9444/staff/entry',ticket:'aGVhZA.bWFj'},'reference_staff').action).toBe('https://endpoint.example.test:9444/staff/entry');
  for(const action of ['http://endpoint.example.test/staff/entry','https://endpoint.example.test/staff/entry?x=1','https://endpoint.example.test/staff'])
    expect(()=>entryTicket({target:'reference_staff',action,ticket:'aGVhZA.bWFj'},'reference_staff')).toThrow();
});

test('S81: the ticket goes into a POST form body in the opened window, never into a URL, and the form is removed',async()=>{
  const calls:string[]=[];
  vi.stubGlobal('fetch',vi.fn(async(url:string)=>{calls.push(url);
    return new Response(JSON.stringify(url.endsWith('/csrf')?{csrf_token:'c'}:{target:'webchat',action:'/api/v1/auth/entry',ticket:TICKET,expires_at:'x'}),{status:200});}));
  const submitted:{method:string;action:string;target:string;body:string}[]=[];
  HTMLFormElement.prototype.submit=function(this:HTMLFormElement){submitted.push({method:this.method,action:this.getAttribute('action')!,target:this.target,
    body:[...new FormData(this).entries()].map(([k,v])=>k+'='+v).join('&')});};
  const opened={opener:{} as unknown,close:vi.fn()};let named='';
  await enterWithTicket('webchat',document,name=>{named=name;return opened as unknown as Window;});
  expect(calls).toEqual(['/api/v1/workbench/auth/csrf','/api/v1/workbench/entry-tickets']);
  expect(submitted).toEqual([{method:'post',action:'/api/v1/auth/entry',target:named,body:'ticket='+TICKET}]);
  expect(opened.opener).toBeNull();expect(document.querySelector('form')).toBeNull();
  // A refusal closes the blank window and throws (the page shows the fixed hint).
  vi.stubGlobal('fetch',vi.fn(async(url:string)=>new Response(JSON.stringify(url.endsWith('/csrf')?{csrf_token:'c'}:{code:'not_owner'}),{status:url.endsWith('/csrf')?200:403})));
  await expect(enterWithTicket('webchat',document,()=>opened as unknown as Window)).rejects.toThrow();
  expect(opened.close).toHaveBeenCalled();
  vi.unstubAllGlobals();
});
