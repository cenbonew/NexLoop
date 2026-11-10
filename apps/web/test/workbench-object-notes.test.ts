// @vitest-environment jsdom
// NX-062 M5c (L6 V-1, V-4): a note is shown as plain text only - markup, a javascript: link or Markdown in its text stays text (no
// element, no link); every note says who wrote it and that it is unverified; a machine note names its client. The input carries the
// "no personal data" hint and is offered only when the member's role carries the Action; what is sent is the typed text.
import {createElement as h} from 'react';
import {act} from 'react';
import {createRoot,type Root} from 'react-dom/client';
import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
import {afterEach,beforeEach,expect,test,vi} from 'vitest';
import {NOTE_INPUT_HINT,OPERATION_ACTION} from '../src/workbench/api';
import {ObjectNotes} from '../src/workbench/ObjectNotes';

(globalThis as {IS_REACT_ACT_ENVIRONMENT?:boolean}).IS_REACT_ACT_ENVIRONMENT=true;
const OBJECT='a'.repeat(64);
const HOSTILE='<img src=x onerror="window.__pwned=1"> [点我](javascript:alert(1)) <b>粗体</b>';
const sent:{path:string;body:unknown}[]=[];
let role:{role:string;actions:string[]};
let root:Root;let box:HTMLDivElement;

beforeEach(()=>{sent.length=0;role={role:'owner',actions:[OPERATION_ACTION.object_note_add]};
  vi.stubGlobal('fetch',vi.fn(async(path:string,init?:RequestInit)=>{
    if(path.endsWith('/csrf'))return new Response(JSON.stringify({csrf_token:'synthetic-csrf'}),{status:200});
    if(path==='/api/v1/workbench/me')return new Response(JSON.stringify({principal_id:'synthetic-member',...role}),{status:200});
    if(path.endsWith('/notes'))return new Response(JSON.stringify({type_name:'Commitment',object_id:OBJECT,notes:[
      {note_id:'1'.repeat(64),author_kind:'machine',client_id:'acme-agent',author_principal_id:null,text:HOSTILE,erased:false,created_at:'2026-11-10T07:00:00+00:00'},
      {note_id:'2'.repeat(64),author_kind:'human',client_id:null,author_principal_id:'synthetic-member',text:'客户周五前收货',erased:false,created_at:'2026-11-10T06:00:00+00:00'}]}),{status:200});
    sent.push({path,body:JSON.parse(String(init?.body))});return new Response(JSON.stringify({note_id:'3'.repeat(64),replayed:false}),{status:200});}));
  box=document.createElement('div');document.body.append(box);root=createRoot(box);});
afterEach(()=>{act(()=>root.unmount());box.remove();vi.unstubAllGlobals();});

async function render(){
  const client=new QueryClient({defaultOptions:{queries:{retry:false}}});
  await act(async()=>{root.render(h(QueryClientProvider,{client},h(ObjectNotes,{type:'Commitment',objectId:OBJECT})));});
  await act(async()=>{await new Promise(r=>setTimeout(r,0));});
}

test('notes are plain text, marked unverified, a machine note names its client',async()=>{
  await render();
  const texts=[...box.querySelectorAll('.note-text')];
  expect(texts.map(t=>t.textContent)).toEqual([HOSTILE,'客户周五前收货']);
  expect(box.querySelector('img')).toBeNull();
  expect(box.querySelector('a')).toBeNull();
  expect(box.querySelector('.note-list b')).toBeNull();
  expect((window as {__pwned?:number}).__pwned).toBeUndefined();
  for(const t of texts)expect([...t.childNodes].every(n=>n.nodeType===Node.TEXT_NODE)).toBe(true);
  const authors=[...box.querySelectorAll('.note-author')].map(a=>a.textContent??'');
  expect(authors[0]).toContain('机器客户端 acme-agent 的备注 · 未核实陈述');
  expect(authors[1]).toContain('成员的备注 · 未核实陈述');
});

test('the input carries the hint, sends the typed text, and is not offered without the Action',async()=>{
  await render();
  expect(box.textContent).toContain(NOTE_INPUT_HINT);
  const area=box.querySelector('textarea') as HTMLTextAreaElement;
  expect(area.maxLength).toBe(2000);
  await act(async()=>{
    const setter=Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value')!.set!;
    setter.call(area,'  一条新备注  ');area.dispatchEvent(new Event('input',{bubbles:true}));});
  await act(async()=>{(box.querySelector('form') as HTMLFormElement).requestSubmit();});
  await act(async()=>{await new Promise(r=>setTimeout(r,0));});
  expect(sent).toEqual([{path:'/api/v1/workbench/actions/object_note_add',body:{type_name:'Commitment',object_id:OBJECT,text:'一条新备注'}}]);
  act(()=>root.unmount());root=createRoot(box);
  role={role:'reviewer',actions:[]};
  await render();
  expect(box.querySelector('textarea')).toBeNull();
  expect(box.querySelectorAll('.note-text').length).toBe(2);  // the reviewer reads
});
