// @vitest-environment jsdom
// Owner 2026-10-29: a world model list is searched by the English identifier or its Chinese gloss.
import {createElement as h,act} from 'react';
import {createRoot} from 'react-dom/client';
import {readFileSync} from 'node:fs';
import {resolve} from 'node:path';
import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
import {expect,test} from 'vitest';
import {worldModel} from '../src/workbench/api';
import {WorldModel} from '../src/workbench/pages/WorldModel';

(globalThis as {IS_REACT_ACT_ENVIRONMENT?:boolean}).IS_REACT_ACT_ENVIRONMENT=true;
const example=JSON.parse(readFileSync(resolve(__dirname,'../../../packages/contracts/examples/workbench-world-model.valid.json'),'utf8'));
const glossed=structuredClone(example);glossed.object_types[0].gloss='客户';glossed.object_types[1].gloss='会话';

test('typing 客户 or consum keeps Consumer, typing 会话 keeps Conversation; nothing found is the filtered state',()=>{
  const client=new QueryClient({defaultOptions:{queries:{retry:false,staleTime:Infinity}}});client.setQueryData(['workbench','world-model'],worldModel(glossed));
  const box=document.createElement('div');document.body.append(box);const root=createRoot(box);
  act(()=>root.render(h(QueryClientProvider,{client},h(WorldModel,{section:'object_types',go:()=>{}}))));
  const input=box.querySelector<HTMLInputElement>('input[type=search]')!;const setter=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value')!.set!;
  const type=(v:string)=>act(()=>{setter.call(input,v);input.dispatchEvent(new Event('input',{bubbles:true}));});
  const rows=()=>[...box.querySelectorAll('tbody tr code.term-id')].map(c=>c.textContent).filter(t=>t==='Consumer'||t==='Conversation');
  expect(rows()).toEqual(['Consumer','Conversation']);
  type('客户');expect(rows()).toEqual(['Consumer']);
  type('consum');expect(rows()).toEqual(['Consumer']);
  type('会话');expect(rows()).toEqual(['Conversation']);
  type('不存在');expect(box.querySelector('[data-state="filtered"]')).not.toBeNull();
  act(()=>root.unmount());box.remove();
});
