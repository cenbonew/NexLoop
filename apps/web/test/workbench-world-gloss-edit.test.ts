// @vitest-environment jsdom
// The owner's glossary correction (docs/design/world-model-glossary.md §3.3/§4): world_glossary_set {kind, identifier, gloss}; 1-24
// characters with Chinese, or "" to clear; only a role carrying the Action sees it.
import {createElement as h,act} from 'react';
import {createRoot,type Root} from 'react-dom/client';
import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
import {afterEach,beforeEach,expect,test,vi} from 'vitest';
import {me,OPERATION_ACTION} from '../src/workbench/api';
import {GlossEditor,glossProblem} from '../src/workbench/GlossEdit';

(globalThis as {IS_REACT_ACT_ENVIRONMENT?:boolean}).IS_REACT_ACT_ENVIRONMENT=true;
const owner=me({principal_id:'synthetic-owner',role:'owner',actions:[OPERATION_ACTION.world_glossary_set]});
const operator=me({principal_id:'synthetic-op',role:'operator',actions:['eios:action:nexloop.workbench.read:1']});
const sent:{path:string;body:unknown}[]=[];let root:Root;let box:HTMLDivElement;
beforeEach(()=>{sent.length=0;
  vi.stubGlobal('fetch',vi.fn(async(path:string,init?:RequestInit)=>{
    if(path.endsWith('/csrf'))return new Response(JSON.stringify({csrf_token:'synthetic-csrf'}),{status:200});
    if(path.endsWith('/me'))return new Response(JSON.stringify(owner),{status:200});  // the refetch after a write
    sent.push({path,body:JSON.parse(String(init?.body))});
    return new Response(JSON.stringify({kind:'object_type',identifier:'Consumer',gloss:'顾客',gloss_source:'manual',revision:2}),{status:200});}));
  box=document.createElement('div');document.body.append(box);root=createRoot(box);});
afterEach(()=>{act(()=>root.unmount());box.remove();vi.unstubAllGlobals();});
const settle=async()=>{for(let i=0;i<6;i++)await act(async()=>{await new Promise(r=>setTimeout(r,0));});};
const mount=(member:typeof owner)=>{const client=new QueryClient({defaultOptions:{queries:{retry:false,staleTime:Infinity}}});client.setQueryData(['workbench','me'],member);
  act(()=>root.render(h(QueryClientProvider,{client},h(GlossEditor,{kind:'object_type',label:' Object Type ',targets:[{identifier:'Consumer',gloss:'客户'},{identifier:'Goal',gloss:null}]}))));};
const button=(text:string)=>[...box.querySelectorAll('button')].find(b=>b.textContent===text);
const setter=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value')!.set!;
const type=(el:HTMLInputElement,v:string)=>act(()=>{setter.call(el,v);el.dispatchEvent(new Event('input',{bubbles:true}));});

test('the rules: 1-24 characters with Chinese, or an explicit clear',()=>{
  expect(glossProblem('顾客',false)).toBeNull();expect(glossProblem('',true)).toBeNull();
  expect(glossProblem('  ',false)).toBe('填写中文译名，或勾选“清空中文”');expect(glossProblem('customer',false)).toBe('译名需要包含中文');
  expect(glossProblem('客'.repeat(25),false)).toBe('译名最多 24 个字');expect(glossProblem('客'.repeat(24),false)).toBeNull();
});

test('only the owner sees it; a correction sends {kind, identifier, gloss}; a clear sends ""; an invalid one is not sent',async()=>{
  mount(operator);expect(box.textContent).toBe('');act(()=>root.unmount());root=createRoot(box);
  mount(owner);act(()=>button('改正 Object Type 的中文译名')!.click());
  const input=box.querySelector<HTMLInputElement>('input:not([type=checkbox])')!;expect(input.value).toBe('客户');expect(input.maxLength).toBe(24);
  const submit=()=>box.querySelector<HTMLButtonElement>('button[type=submit]')!;
  type(input,'customer');expect(submit().disabled).toBe(true);expect(box.textContent).toContain('译名需要包含中文');
  type(input,'顾客');expect(submit().disabled).toBe(false);
  await act(async()=>{submit().click();});await settle();
  expect(sent).toEqual([{path:'/api/v1/workbench/actions/world_glossary_set',body:{kind:'object_type',identifier:'Consumer',gloss:'顾客'}}]);
  const select=box.querySelector('select')!;act(()=>{select.value='Goal';select.dispatchEvent(new Event('change',{bubbles:true}));});
  expect(input.value).toBe('');
  act(()=>box.querySelector<HTMLInputElement>('input[type=checkbox]')!.click());expect(input.disabled).toBe(true);
  await act(async()=>{submit().click();});await settle();
  expect(sent[1]).toEqual({path:'/api/v1/workbench/actions/world_glossary_set',body:{kind:'object_type',identifier:'Goal',gloss:''}});
});
