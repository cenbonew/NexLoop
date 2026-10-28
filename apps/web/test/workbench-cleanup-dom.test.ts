// @vitest-environment jsdom
// UX audit S8 (C3, C4, C13, C19): a table scroller is a tab stop only while it overflows; the settings page names how many members
// there are and where they are managed instead of a second, truncated member table; the session check shows the workbench's shape
// and a failed evidence read offers a retry; the schema graph has no inline styles.
import {createElement as h} from 'react';
import {act} from 'react';
import {createRoot,type Root} from 'react-dom/client';
import {renderToStaticMarkup} from 'react-dom/server';
import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
import {afterEach,beforeEach,expect,test,vi} from 'vitest';
import {settings} from '../src/workbench/api';
import {DataTable} from '../src/workbench/DataTable';
import {Settings} from '../src/workbench/pages/Settings';
import {RemediationEvidencePanel} from '../src/workbench/pages/Actions';
import {WorkbenchApp} from '../src/workbench/WorkbenchApp';
import {SchemaGraph} from '../src/workbench/graph/SchemaGraph';

(globalThis as {IS_REACT_ACT_ENVIRONMENT?:boolean}).IS_REACT_ACT_ENVIRONMENT=true;
let root:Root;let box:HTMLDivElement;
beforeEach(()=>{box=document.createElement('div');document.body.append(box);root=createRoot(box);});
afterEach(()=>{act(()=>root.unmount());box.remove();vi.unstubAllGlobals();vi.restoreAllMocks();});
const settle=async()=>{for(let i=0;i<6;i++)await act(async()=>{await new Promise(r=>setTimeout(r,0));});};
const client=()=>new QueryClient({defaultOptions:{queries:{retry:false,staleTime:Infinity}}});

test('C3: the table scroller is a focusable region only when its table is wider than it',()=>{
  const rows=[{id:'r1'}];const columns=[{label:'名称',main:true,cell:(r:{id:string})=>r.id}];
  const width=(scroll:number,client:number)=>{vi.spyOn(HTMLElement.prototype,'scrollWidth','get').mockReturnValue(scroll);vi.spyOn(HTMLElement.prototype,'clientWidth','get').mockReturnValue(client);};
  width(400,400);
  act(()=>root.render(h(DataTable<{id:string}>,{label:'记录',rows,rowKey:r=>r.id,columns})));
  const scroller=box.querySelector('.table-scroll')!;
  expect(scroller.getAttribute('tabindex')).toBeNull();expect(scroller.getAttribute('role')).toBeNull();
  vi.restoreAllMocks();width(900,400);
  act(()=>root.render(h(DataTable<{id:string}>,{label:'记录',rows:[...rows,{id:'r2'}],rowKey:r=>r.id,columns})));
  expect(scroller.getAttribute('tabindex')).toBe('0');expect(scroller.getAttribute('aria-label')).toBe('记录（可横向滚动）');
});

test('C4: settings says how many members by role and leads to 成员与账号; no truncated principal table',()=>{
  const c=client();const onMembers=vi.fn();
  c.setQueryData(['workbench','settings'],settings({application_id:'synthetic-app',manifest_version:3,roles_version:2,roles:{},
    members:[{principal_id:'synthetic-staff-owner-principal',role:'owner'},{principal_id:'synthetic-staff-operator-principal',role:'operator'},{principal_id:'synthetic-staff-operator-2',role:'operator'}]}));
  c.setQueryData(['workbench','audit'],[]);
  act(()=>root.render(h(QueryClientProvider,{client:c},h(Settings,{onMembers}))));
  expect(box.querySelector('table[aria-label="成员与角色"]')).toBeNull();
  const line=box.querySelector('section[aria-labelledby="settings-members-title"]')!.textContent!;
  expect(line).toMatch(/^成员3 位成员（负责人 1、[^）]+ 2），在“成员与账号”页管理/);expect(line).not.toContain('synthetic-staff');
  act(()=>[...box.querySelectorAll('button')].find(b=>b.textContent==='“成员与账号”')!.click());expect(onMembers).toHaveBeenCalledTimes(1);
});

test('C13: while the session is checked the workbench shows its shape, not a line on white',()=>{
  vi.stubGlobal('fetch',vi.fn(()=>new Promise(()=>{})));
  window.history.replaceState(null,'','/workbench/overview');
  act(()=>root.render(h(QueryClientProvider,{client:client()},h(WorkbenchApp,{slots:{}}))));
  const skeleton=box.querySelector('.workbench-skeleton')!;
  expect(skeleton.getAttribute('aria-busy')).toBe('true');expect(skeleton.querySelectorAll('.skeleton-line').length).toBeGreaterThan(8);
  expect(skeleton.querySelector('.sr-only')?.textContent).toBe('正在加载…');
});

test('C13: an unreadable evidence chain is the shared unavailable state with a retry',async()=>{
  let calls=0;
  vi.stubGlobal('fetch',vi.fn(async()=>{calls++;return new Response('{}',{status:503});}));
  act(()=>root.render(h(QueryClientProvider,{client:client()},h(RemediationEvidencePanel,{id:'0b0c6c57-0000-4000-8000-000000000001'}))));
  await settle();
  expect(box.querySelector('[data-state="unavailable"]')?.getAttribute('role')).toBe('alert');
  const retry=[...box.querySelectorAll('button')].find(b=>b.textContent==='重试')!;
  await act(async()=>{retry.click();});await settle();expect(calls).toBe(2);
});

test('C19: the schema graph carries no inline style',()=>{
  const html=renderToStaticMarkup(h(SchemaGraph,{nodes:[{id:'A',label:'A'},{id:'B',label:'B'},{id:'C',label:'C'}],
    edges:[{id:'ab',from:'A',to:'B',label:'ab N:1'},{id:'cc',from:'C',to:'C',label:'cc N:1'}],focus:null,onFocus:()=>{},label:'g'}));
  expect(html).not.toContain('style=');expect(html).toContain('class="schema-graph-canvas"');expect(html).toContain('class="schema-edge-label"');
});
