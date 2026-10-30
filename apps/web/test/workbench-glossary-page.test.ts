// @vitest-environment jsdom
// Owner walkthrough R57 (W5): the glossary is managed on its own page under 管理 (世界模型译名), offered only to a role carrying
// world_glossary_set: browse and search every identifier by kind with 机翻 / 人工 marked, and open the existing correction form from a
// row (same checks, same sim rule). The type panel of the graph closes from its sticky head.
import {createElement as h,act,useState,type ReactNode} from 'react';
import {createRoot,type Root} from 'react-dom/client';
import {readFileSync} from 'node:fs';
import {resolve} from 'node:path';
import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
import {afterEach,beforeEach,expect,test} from 'vitest';
import {me,OPERATION_ACTION,worldModel} from '../src/workbench/api';
import {GLOSSARY_KINDS,Glossary,glossaryRows} from '../src/workbench/pages/Glossary';
import {NAV_GROUPS,navOffered} from '../src/workbench/WorkbenchApp';
import {PAGES} from '../src/workbench/route';
import {WorldModel} from '../src/workbench/pages/WorldModel';

(globalThis as {IS_REACT_ACT_ENVIRONMENT?:boolean}).IS_REACT_ACT_ENVIRONMENT=true;
const example=JSON.parse(readFileSync(resolve(__dirname,'../../../packages/contracts/examples/workbench-world-model-v2.valid.json'),'utf8'));
const owner=me({principal_id:'synthetic-owner',role:'owner',actions:[OPERATION_ACTION.world_glossary_set]});
const operator=me({principal_id:'synthetic-op',role:'operator',actions:['eios:action:nexloop.workbench.read:1']});
let root:Root;let box:HTMLDivElement;
beforeEach(()=>{box=document.createElement('div');document.body.append(box);root=createRoot(box);});
afterEach(()=>{act(()=>root.unmount());box.remove();});
const mount=(node:ReactNode,member:typeof owner,model:unknown=example)=>{const c=new QueryClient({defaultOptions:{queries:{retry:false,staleTime:Infinity}}});
  c.setQueryData(['workbench','world-model'],worldModel(model));c.setQueryData(['workbench','me'],member);
  act(()=>root.render(h(QueryClientProvider,{client:c},node)));};
const button=(text:string)=>[...box.querySelectorAll('button')].find(b=>b.textContent===text);

test('the sidebar offers 世界模型译名 under 管理, before 实验空间, only to a role carrying world_glossary_set',()=>{
  expect(PAGES.glossary).toBe('世界模型译名');
  const admin=NAV_GROUPS.find(g=>g.title==='管理')!.pages;expect(admin.indexOf('glossary')).toBe(admin.indexOf('experiments')-1);
  expect(navOffered('glossary',owner)).toBe(true);expect(navOffered('glossary',operator)).toBe(false);expect(navOffered('glossary',undefined)).toBe(false);
  expect(navOffered('settings',operator)).toBe(true);
});

test('every kind is listed with its source; sections carry none; a Property shared by types appears once',()=>{
  const m=worldModel(example);
  expect(GLOSSARY_KINDS.map(k=>k.label)).toEqual(['Object Type','Property','Link','反向名','Action','Function','Event Type','栏目名']);
  expect(glossaryRows(m,'object_type')).toContainEqual({identifier:'Consumer',gloss:'客户',source:'manual'});
  const props=glossaryRows(m,'property').map(r=>r.identifier);expect(new Set(props).size).toBe(props.length);
  expect(glossaryRows(m,'section').every(r=>r.source===null)).toBe(true);
});

test('the owner browses, searches and opens the correction form on the chosen identifier',()=>{
  mount(h(Glossary),owner);
  expect(box.querySelector('h1')!.textContent).toBe('世界模型译名');
  const kinds=[...box.querySelectorAll('.glossary-kinds button')];expect(kinds).toHaveLength(8);
  const rows=()=>[...box.querySelectorAll('tbody tr')].map(r=>r.textContent);
  expect(rows().some(r=>r!.includes('Consumer')&&r!.includes('客户')&&r!.includes('人工'))).toBe(true);
  const search=box.querySelector<HTMLInputElement>('input[type=search]')!;
  act(()=>{Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value')!.set!.call(search,'客户');search.dispatchEvent(new Event('input',{bubbles:true}));});
  expect(rows().every(r=>r!.includes('客户'))).toBe(true);
  act(()=>box.querySelector<HTMLButtonElement>('button[aria-label="改正 Consumer 的中文译名"]')!.click());
  expect(box.querySelector<HTMLSelectElement>('.gloss-edit select')!.value).toBe('Consumer');
  expect(box.querySelector<HTMLInputElement>('.gloss-edit input:not([type=checkbox])')!.value).toBe('客户');
  act(()=>button('收起')!.click());expect(box.querySelector('.gloss-edit')).toBeNull();
  act(()=>kinds.find(b=>b.textContent==='栏目名')!.dispatchEvent(new MouseEvent('click',{bubbles:true})));
  expect(box.textContent).toContain('Object Type');expect(box.textContent).not.toContain('机翻 ');
});

test('in a simulation the owner sees the rows and the disabled control with its reason, no row button; others are refused',()=>{
  mount(h(Glossary),owner,{...example,world:'sim:00000000-0000-4000-8000-000000000001'});
  expect(box.querySelectorAll('tbody tr').length).toBeGreaterThan(0);expect(box.querySelector('button[aria-label^="改正 "]')).toBeNull();
  expect(button('改正 Object Type 的中文译名')!.disabled).toBe(true);expect(box.textContent).toContain('只能在真实世界改正');
  act(()=>root.unmount());root=createRoot(box);mount(h(Glossary),operator);
  expect(box.textContent).toContain('你的角色没有管理世界模型译名的权限');expect(box.querySelector('table')).toBeNull();
});

test('W2: the type panel closes from the top of its sticky head, not below its content',()=>{
  let closed=false;
  mount(h(WorldModel,{section:'object_types' as never,query:{view:'graph',focus:'Consumer'},go:(_s,q)=>{if(!q?.focus)closed=true;}}),owner);
  const head=box.querySelector('.world-panel .card-head')!;const close=head.querySelector<HTMLButtonElement>('.panel-close')!;
  expect(close.textContent).toBe('关闭');expect(box.querySelector('.world-panel .card-foot')).toBeNull();
  // The panel sits in the canvas's row (SchemaGraph aside), so it can take the canvas's height.
  expect(box.querySelector('.schema-graph-body.with-aside>.world-panel')).not.toBeNull();
  act(()=>close.click());expect(closed).toBe(true);
});

test('L6 I24: 关闭 returns focus to the node the panel was opened from (the canvas when that node is gone)',()=>{
  const Host=()=>{const [focus,setFocus]=useState<string|null>('Consumer');
    return h(WorldModel,{section:'object_types' as never,query:{view:'graph',...(focus?{focus}:{})},go:(_s,q)=>setFocus(q?.focus??null)});};
  mount(h(Host),owner);
  const close=box.querySelector<HTMLButtonElement>('.world-panel .panel-close')!;close.focus();
  act(()=>close.click());
  expect(box.querySelector('.world-panel')).toBeNull();
  expect(document.activeElement).toBe(box.querySelector('[data-node="Consumer"]'));
});

