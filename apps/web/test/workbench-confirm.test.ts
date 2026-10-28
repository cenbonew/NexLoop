// @vitest-environment jsdom
// UX audit B5: a row action that used to run on one click with a reason nobody typed now opens a confirmation in place: the effect,
// the member's own reason, a confirm button that stays disabled (and says why) until the reason is there; 取消 / Escape close it and
// return focus to the trigger; what is sent carries the typed reason.
import {createElement as h} from 'react';
import {act} from 'react';
import {createRoot,type Root} from 'react-dom/client';
import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
import {afterEach,beforeEach,expect,test,vi} from 'vitest';
import {me,OPERATION_ACTION} from '../src/workbench/api';
import {ConfirmedForm} from '../src/workbench/actions';

(globalThis as {IS_REACT_ACT_ENVIRONMENT?:boolean}).IS_REACT_ACT_ENVIRONMENT=true;
const owner=me({principal_id:'synthetic-owner',role:'owner',actions:[OPERATION_ACTION.cancel_approval,OPERATION_ACTION.revoke_workbench_invitation]});
const sent:{path:string;body:unknown}[]=[];
let root:Root;let box:HTMLDivElement;
beforeEach(()=>{sent.length=0;
  vi.stubGlobal('fetch',vi.fn(async(path:string,init?:RequestInit)=>{
    if(path.endsWith('/csrf'))return new Response(JSON.stringify({csrf_token:'synthetic-csrf'}),{status:200});
    sent.push({path,body:JSON.parse(String(init?.body))});return new Response(JSON.stringify({status:'ok'}),{status:200});}));
  box=document.createElement('div');document.body.append(box);root=createRoot(box);});
afterEach(()=>{act(()=>root.unmount());box.remove();vi.unstubAllGlobals();});
const settle=async()=>{for(let i=0;i<6;i++)await act(async()=>{await new Promise(r=>setTimeout(r,0));});};
const button=(text:string)=>[...box.querySelectorAll('button')].find(b=>b.textContent===text)!;
function mount(props:Partial<Parameters<typeof ConfirmedForm>[0]>={}){
  act(()=>root.render(h(QueryClientProvider,{client:new QueryClient()},h(ConfirmedForm,{operation:'cancel_approval',member:owner,trigger:'撤回…',title:'确认撤回',
    impact:'影响：这条审批请求结束。',body:(reason:string)=>({approval_id:'a1',reason}),...props}))));
}
const type=(el:HTMLTextAreaElement,value:string)=>act(()=>{Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value')!.set!.call(el,value);el.dispatchEvent(new Event('input',{bubbles:true}));});

test('nothing is sent on the first click; the confirmation shows the effect, needs the reason, and sends the typed one',async()=>{
  mount();
  act(()=>button('撤回…').click());
  expect(sent).toHaveLength(0);
  expect(box.textContent).toContain('影响：这条审批请求结束。');
  const textarea=box.querySelector('textarea')!;expect(document.activeElement).toBe(textarea);
  const confirm=box.querySelector<HTMLButtonElement>('button[type=submit]')!;
  expect(confirm.disabled).toBe(true);expect(box.querySelector('.disabled-reason')?.textContent).toBe('填写原因后可确认。');
  type(textarea,'  提交错了  ');
  expect(confirm.disabled).toBe(false);
  await act(async()=>{confirm.click();});await settle();
  expect(sent).toEqual([{path:'/api/v1/workbench/actions/cancel_approval',body:{approval_id:'a1',reason:'提交错了'}}]);
});

test('取消 and Escape close it without sending and put focus back on the trigger',async()=>{
  mount();
  act(()=>button('撤回…').click());act(()=>button('取消').click());
  expect(box.querySelector('.confirm-panel')).toBeNull();expect(document.activeElement).toBe(button('撤回…'));
  act(()=>button('撤回…').click());
  act(()=>{box.querySelector('textarea')!.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}));});
  expect(box.querySelector('.confirm-panel')).toBeNull();expect(document.activeElement).toBe(button('撤回…'));
  expect(sent).toHaveLength(0);
});

test('without a reason field the confirm is ready at once and danger-toned; a role without the operation gets nothing',async()=>{
  mount({operation:'revoke_workbench_invitation',trigger:'撤销…',title:'确认撤销邀请',tone:'danger',withReason:false,body:()=>({invitation_id:'i1'})});
  expect(button('撤销…').className).toContain('confirm-trigger-danger');
  act(()=>button('撤销…').click());
  const confirm=box.querySelector<HTMLButtonElement>('button[type=submit]')!;
  expect(confirm.disabled).toBe(false);expect(confirm.className).toBe('btn-danger');expect(box.querySelector('textarea')).toBeNull();
  act(()=>root.render(h(QueryClientProvider,{client:new QueryClient()},h(ConfirmedForm,{operation:'dismiss_contact_preference',member:owner,trigger:'不采纳…',
    title:'确认不采纳',impact:'x',body:()=>({})}))));
  expect(box.textContent).toBe('');
});

test('L6 review: while the request is in flight neither 取消 nor Escape closes it; an unknown result keeps the same-request retry; then it closes',async()=>{
  let release:(r:Response)=>void=()=>{};
  vi.stubGlobal('fetch',vi.fn(async(path:string,init?:RequestInit)=>{
    if(path.endsWith('/csrf'))return new Response(JSON.stringify({csrf_token:'synthetic-csrf'}),{status:200});
    sent.push({path,body:JSON.parse(String(init?.body))});return new Promise<Response>(r=>{release=r;});}));
  mount();
  act(()=>button('撤回…').click());
  type(box.querySelector('textarea')!,'提交错了');
  await act(async()=>{box.querySelector<HTMLButtonElement>('button[type=submit]')!.click();});await settle();
  expect(sent).toHaveLength(1);
  const cancel=[...box.querySelectorAll<HTMLButtonElement>('.confirm-panel > button')].pop()!;
  expect(cancel.disabled).toBe(true);expect(cancel.textContent).toBe('等待结果…');
  act(()=>cancel.click());
  act(()=>{box.querySelector('textarea')!.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}));});
  expect(box.querySelector('.confirm-panel')).not.toBeNull();
  await act(async()=>{release(new Response(JSON.stringify({code:'unavailable'}),{status:503}));});await settle();  // the result is unknown
  expect(box.querySelector('.confirm-panel')).not.toBeNull();
  expect(box.querySelector('button[type=submit]')?.textContent).toBe('用同一请求重试');
  expect(button('取消').disabled).toBe(false);
  act(()=>{box.querySelector('textarea')!.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}));});
  expect(box.querySelector('.confirm-panel')).toBeNull();
});
