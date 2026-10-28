// @vitest-environment jsdom
// Time inputs are Beijing time, as every time on the workbench is shown (L6, 10-28): the commitment operations' datetime-local fields
// and the experiment's end are converted with one function, whatever the browser's zone - here a browser in New York.
process.env.TZ='America/New_York';
import {createElement as h} from 'react';
import {act} from 'react';
import {createRoot,type Root} from 'react-dom/client';
import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
import {afterEach,beforeEach,expect,test,vi} from 'vitest';
import {me} from '../src/workbench/api';
import {CommitmentControls} from '../src/workbench/actions';
import {EMPTY_DRAFT,registrationOf} from '../src/workbench/pages/Experiments';
import {beijingLocalToUtc} from '../src/workbench/ui';

(globalThis as {IS_REACT_ACT_ENVIRONMENT?:boolean}).IS_REACT_ACT_ENVIRONMENT=true;
const id=(c:string)=>c.repeat(64);
const operator=me({principal_id:'synthetic-staff-operator',role:'operator',
  actions:['nexloop.workbench.read','nexloop.commitment.read','Commitment.extend','Commitment.attest'].map(a=>`eios:action:${a}:1`)});
const sent:{path:string;body:Record<string,unknown>}[]=[];
let root:Root;let box:HTMLDivElement;
beforeEach(()=>{sent.length=0;
  vi.stubGlobal('fetch',vi.fn(async(path:string,init?:RequestInit)=>{
    if(path.endsWith('/csrf'))return new Response(JSON.stringify({csrf_token:'synthetic-csrf'}),{status:200});
    sent.push({path,body:JSON.parse(String(init?.body))});return new Response(JSON.stringify({status:'ok'}),{status:200});}));
  box=document.createElement('div');document.body.append(box);root=createRoot(box);});
afterEach(()=>{act(()=>root.unmount());box.remove();vi.unstubAllGlobals();});
const settle=async()=>{for(let i=0;i<6;i++)await act(async()=>{await new Promise(r=>setTimeout(r,0));});};
function set(el:HTMLInputElement|HTMLTextAreaElement,value:string){
  const proto=el instanceof HTMLTextAreaElement?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;
  act(()=>{Object.getOwnPropertyDescriptor(proto,'value')!.set!.call(el,value);el.dispatchEvent(new Event('input',{bubbles:true}));});
}

test('the browser here is not in Beijing time',()=>{
  expect(new Date('2026-10-28T12:00').toISOString()).not.toBe('2026-10-28T04:00:00.000Z');
});

test('beijingLocalToUtc reads the value as +08:00; anything else is not a time',()=>{
  expect(beijingLocalToUtc('2026-10-28T12:00')).toBe('2026-10-28T04:00:00.000Z');
  expect(beijingLocalToUtc('2026-10-28T12:00:30')).toBe('2026-10-28T04:00:30.000Z');
  expect(beijingLocalToUtc('2026-03-08T02:30')).toBe('2026-03-07T18:30:00.000Z');  // a New York DST gap is no gap in Beijing
  for(const bad of ['','2026-10-28','2026-10-28T12:00Z','2026-13-40T99:99'])expect(beijingLocalToUtc(bad)).toBeNull();
});

test('the experiment end and a commitment extension both send the Beijing time as UTC',async()=>{
  const r=registrationOf({...EMPTY_DRAFT,experiment_id:'followup.timing',world:'real',hypothesis:'h',candidate:'followup.later',control:'followup.default',metric:'m',
    sample_candidate:'20',sample_control:'20',model_cny:'1',ends_at:'2026-10-28T12:00',seed:'1'});
  expect('registration' in r&&r.registration.ends_at).toBe('2026-10-28T04:00:00Z');
  const client=new QueryClient({defaultOptions:{queries:{retry:false,staleTime:Infinity}}});
  client.setQueryData(['workbench','me'],operator);
  client.setQueryData(['workbench','commitment',id('1')],{commitment_id:id('1'),consumer_id:id('2'),revision:1,status:'open',properties:{status:'open'},quote:null,
    quote_status:'none',origin:'claim',evidence:{requested:[],delivered:[],customer_confirmed:[],problem_resolved:'unavailable'},events:[],exceptions:[]});
  act(()=>root.render(h(QueryClientProvider,{client},h(CommitmentControls,{commitmentId:id('1')}))));
  const form=box.querySelector<HTMLFormElement>('form[aria-label="延期"]')!;
  expect(form.textContent).toContain('新期限（北京时间）');
  set(form.querySelector<HTMLInputElement>('input[type=datetime-local]')!,'2026-11-01T09:30');
  set(form.querySelector('textarea')!,'客户要求延后');
  await act(async()=>{form.querySelector<HTMLButtonElement>('button[type=submit]')!.click();});await settle();
  expect(sent.map(s=>[s.path,s.body.due_at])).toEqual([['/api/v1/workbench/actions/extend_commitment','2026-11-01T01:30:00.000Z']]);
});
