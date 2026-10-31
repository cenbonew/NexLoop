// @vitest-environment jsdom
// Owner S73 (2026-10-31): 管理 › 入口 lists read-only links to every reachable page: the workbench pages this member's role is
// offered, the same-origin pages (WebChat, readiness) built from the page's origin and opened in a new window, and the external
// reference endpoint staff page only when the deployment gives its URL (GET /api/v1/workbench/entries). No form, no button that acts.
import {createElement as h,act} from 'react';
import {createRoot,type Root} from 'react-dom/client';
import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
import {afterEach,beforeEach,expect,test} from 'vitest';
import {entryLinks,me,OPERATION_ACTION} from '../src/workbench/api';
import {Entries,originEntries} from '../src/workbench/pages/Entries';
import {NAV_GROUPS,navOffered} from '../src/workbench/WorkbenchApp';
import {PAGES,type Route} from '../src/workbench/route';

(globalThis as {IS_REACT_ACT_ENVIRONMENT?:boolean}).IS_REACT_ACT_ENVIRONMENT=true;
const owner=me({principal_id:'synthetic-owner',role:'owner',actions:[OPERATION_ACTION.world_glossary_set]});
const operator=me({principal_id:'synthetic-op',role:'operator',actions:['eios:action:nexloop.workbench.read:1']});
const ORIGIN='https://gateway.example.test:9443';
let root:Root;let box:HTMLDivElement;
beforeEach(()=>{box=document.createElement('div');document.body.append(box);root=createRoot(box);});
afterEach(()=>{act(()=>root.unmount());box.remove();});
const STAFF='https://endpoint.example.test:9444/staff';
const mount=(member:typeof owner,go:(r:Route)=>void=()=>{},staff:string|null=null)=>{const c=new QueryClient({defaultOptions:{queries:{retry:false,staleTime:Infinity}}});
  c.setQueryData(['workbench','me'],member);c.setQueryData(['workbench','entries'],entryLinks({reference_endpoint_staff_url:staff}));
  act(()=>root.render(h(QueryClientProvider,{client:c},h(Entries,{groups:NAV_GROUPS,offered:navOffered,go,origin:ORIGIN}))));};
const links=()=>[...box.querySelectorAll<HTMLAnchorElement>('a')];

test('入口 sits under 管理 right after 世界模型译名, offered to every role',()=>{
  expect(PAGES.entries).toBe('入口');
  const admin=NAV_GROUPS.find(g=>g.title==='管理')!.pages;expect(admin.indexOf('entries')).toBe(admin.indexOf('glossary')+1);
  expect(navOffered('entries',operator)).toBe(true);
});

test('the owner sees every workbench page, the world model sections, and the same-origin pages in a new window',()=>{
  let went:Route|null=null;mount(owner,r=>{went=r;});
  expect(box.querySelector('h1')!.textContent).toBe('入口');
  const hrefs=links().map(a=>a.getAttribute('href'));
  for(const page of NAV_GROUPS.flatMap(g=>g.pages).filter(p=>p!=='entries'))expect(hrefs).toContain('/workbench/'+page);
  expect(hrefs).not.toContain('/workbench/entries');expect(hrefs).toContain('/workbench/ontology/object-types');
  const external=links().filter(a=>a.target==='_blank');
  expect(external.map(a=>a.href)).toEqual(originEntries(ORIGIN).map(e=>e.href));expect(external[0].href).toBe(ORIGIN+'/');
  for(const a of external){expect(a.rel).toBe('noopener noreferrer');expect(a.textContent).toContain('新窗口打开');}
  // No URL from the deployment: no reference endpoint entry at all.
  expect(box.textContent).not.toContain('参考端点');expect(box.textContent).not.toContain('外部系统');
  // Read-only: nothing on the page acts.
  expect(box.querySelectorAll('button,form,input,select').length).toBe(0);
  act(()=>links().find(a=>a.getAttribute('href')==='/workbench/consumers')!.dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true})));
  expect(went).toEqual({page:'consumers'});
});

test('a page the role is not offered is left out, not refused',()=>{
  mount(operator);
  const hrefs=links().map(a=>a.getAttribute('href'));
  expect(hrefs).not.toContain('/workbench/glossary');expect(hrefs).toContain('/workbench/settings');
  expect(box.textContent).not.toContain('没有');expect(box.querySelector('[role=alert]')).toBeNull();
});

test('the reference endpoint staff page is linked in a new window when the deployment gives its URL',()=>{
  mount(owner,()=>{},STAFF);
  const staff=links().find(a=>a.href===STAFF)!;
  expect(staff.textContent).toContain('参考端点 · 员工页面');expect(staff.textContent).toContain('新窗口打开');
  expect(staff.target).toBe('_blank');expect(staff.rel).toBe('noopener noreferrer');
});

test('only a plain HTTPS URL from the server becomes a link',()=>{
  for(const bad of ['http://endpoint.example.test/staff','javascript:alert(1)','https://u:p@endpoint.example.test/staff','https://endpoint.example.test/staff?x=1','not a url',null])
    expect(entryLinks({reference_endpoint_staff_url:bad}).reference_endpoint_staff_url).toBeNull();
  expect(entryLinks({reference_endpoint_staff_url:STAFF}).reference_endpoint_staff_url).toBe(STAFF);
});
