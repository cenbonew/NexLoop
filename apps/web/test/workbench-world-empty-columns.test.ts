// @vitest-environment jsdom
// K4l (owner 2026-11-02): an Object page whose property columns hold no value reads as broken. Columns empty on the whole page are
// collapsed with a line saying so and a page-local 显示空列 switch; a withheld (permission) column is never taken for empty; with no value
// at all only the Object column and the record columns stay, with the line saying where values come from.
import {createElement as h,act} from 'react';
import {createRoot,type Root} from 'react-dom/client';
import {readFileSync} from 'node:fs';
import {resolve} from 'node:path';
import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
import {afterEach,beforeEach,expect,test} from 'vitest';
import {objectPage,worldModel} from '../src/workbench/api';
import {NO_VALUES_TEXT,emptyColumns} from '../src/workbench/pages/WorldInstances';
import {WorldModel} from '../src/workbench/pages/WorldModel';

(globalThis as {IS_REACT_ACT_ENVIRONMENT?:boolean}).IS_REACT_ACT_ENVIRONMENT=true;
const json=(name:string)=>JSON.parse(readFileSync(resolve(__dirname,`../../../packages/contracts/examples/${name}.valid.json`),'utf8'));
const model=json('workbench-world-model-v2'),base=json('workbench-object-page');
const ids=['a','b','c'].map(c=>c.repeat(64));
const page=(props:(i:number)=>unknown[])=>objectPage({...base,next_after:null,items:ids.map((object_id,i)=>({...base.items[0],object_id,properties:props(i)}))});
const missing=(name:string)=>({name,status:'missing'});
let root:Root;let box:HTMLDivElement;
beforeEach(()=>{box=document.createElement('div');document.body.append(box);root=createRoot(box);});
afterEach(()=>{act(()=>root.unmount());box.remove();});
const mount=(p:ReturnType<typeof objectPage>)=>{const c=new QueryClient({defaultOptions:{queries:{retry:false,staleTime:Infinity}}});
  c.setQueryData(['workbench','world-model'],worldModel(model));c.setQueryData(['workbench','objects','Consumer',''],p);
  act(()=>root.render(h(QueryClientProvider,{client:c},h(WorldModel,{section:'objects' as never,query:{type:'Consumer'},go:()=>{}}))));};
const heads=()=>[...box.querySelectorAll('thead th')].map(t=>t.textContent);
const toggle=()=>box.querySelector<HTMLInputElement>('.empty-columns input[type=checkbox]');

test('emptyColumns: missing and absent are empty; one value or one withheld keeps the column',()=>{
  const p=page(i=>[missing('locale'),i===1?{name:'display_name',status:'ok',value:'示例',value_type:'text'}:missing('display_name'),
    {name:'income',status:'withheld',reason:'not_permitted'},...(i===2?[missing('timezone')]:[])]);
  expect(emptyColumns(p.items,['locale','display_name','income','timezone'])).toEqual(['locale','timezone']);
});

test('columns empty on the page are collapsed with a count; 显示空列 brings them back for this page only',()=>{
  mount(page(i=>[{name:'display_name',status:'ok',value:`客户 ${i}`,value_type:'text'},missing('locale'),missing('timezone'),
    {name:'income',status:'withheld',reason:'restricted'}]));
  expect(box.querySelector('.empty-columns')!.textContent).toContain('有 2 个属性在本页都还没有值，已收起');
  const shown=heads();expect(shown.some(t=>t!.includes('display_name'))).toBe(true);expect(shown.some(t=>t!.includes('income'))).toBe(true);
  expect(shown.some(t=>t!.includes('locale'))).toBe(false);expect(box.querySelectorAll('[data-code="withheld"]')).toHaveLength(3);
  expect(toggle()!.checked).toBe(false);act(()=>toggle()!.click());
  expect(heads().some(t=>t!.includes('locale'))).toBe(true);expect(heads().some(t=>t!.includes('timezone'))).toBe(true);
  act(()=>root.unmount());root=createRoot(box);mount(page(()=>[missing('locale'),{name:'display_name',status:'ok',value:'x',value_type:'text'}]));
  expect(toggle()!.checked).toBe(false);  // not stored anywhere: a new page starts collapsed
});

test('no value anywhere: only Object and the record columns stay, with the line on where values come from',()=>{
  mount(page(()=>['display_name','locale','timezone','lifecycle_status','external_id_refs'].map(missing)));
  expect(box.querySelector('.empty-columns')!.textContent).toContain(NO_VALUES_TEXT);
  expect(heads()).toEqual(['Object','Schema 版本','修订','更新时间']);expect(box.textContent).not.toContain('—');
});

test('a page whose objects carry no property entry at all says the same line, with no switch',()=>{
  mount(page(()=>[]));
  expect(box.querySelector('.empty-columns')!.textContent).toBe(NO_VALUES_TEXT);expect(toggle()).toBeNull();
});

test('a page whose properties are all withheld is a permission, not empty: nothing collapsed, no empty line',()=>{
  mount(page(()=>[{name:'income',status:'withheld',reason:'not_permitted'},{name:'locale',status:'withheld',reason:'restricted'}]));
  expect(box.querySelector('.empty-columns')).toBeNull();expect(heads()).toHaveLength(6);expect(box.querySelectorAll('[data-code="withheld"]')).toHaveLength(6);
});
