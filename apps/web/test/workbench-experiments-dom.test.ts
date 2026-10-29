// @vitest-environment jsdom
// Experiment space slice 2: start, stop and register confirm in place (ConfirmedForm); stop needs the owner's reason; an unknown result
// keeps the same request for a retry until the refetched state shows it took effect, then the control is gone.
import {createElement as h} from 'react';
import {act} from 'react';
import {createRoot,type Root} from 'react-dom/client';
import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
import {afterEach,beforeEach,expect,test,vi} from 'vitest';
import {experiments,me,OPERATION_ACTION} from '../src/workbench/api';
import {EMPTY_DRAFT,Operations,Register,registrationOf} from '../src/workbench/pages/Experiments';

(globalThis as {IS_REACT_ACT_ENVIRONMENT?:boolean}).IS_REACT_ACT_ENVIRONMENT=true;
const owner=me({principal_id:'synthetic-owner',role:'owner',actions:[OPERATION_ACTION.register_experiment,OPERATION_ACTION.start_experiment,OPERATION_ACTION.stop_experiment]});
const operator=me({principal_id:'synthetic-op',role:'operator',actions:['eios:action:nexloop.workbench.read:1']});
const base={experiment_id:'followup.timing',world:'sim:00000000-0000-4000-8000-000000000001',world_kind:'simulation',stage:'screening',hypothesis:'回访延后',
  candidate:{strategy_id:'followup.later',version:1},control:{strategy_id:'followup.default',version:1},primary_metric:{key:'reply_within_24h',direction:'increase'},
  guardrails:{},sample_size:{candidate:20,control:20},budget:{model_cny:1,incentive_cny:0},ends_at:null,registration_digest:'b'.repeat(64),
  registered_at:'2026-10-28T11:00:00+00:00',status:'registered',started_at:null,stopped_at:null,stop_reason:null,stopped_by_guard:false,
  spent:{model_cny:0,incentive_cny:0},allocation:{candidate:0,control:0,excluded:0},report:null,budget_drill:false,notice:null,twin:null};
const row=(changes:Record<string,unknown>={})=>experiments({experiments:[{...base,...changes}]})[0];
const sent:{path:string;body:unknown;key:string|null}[]=[];
let status=200;let root:Root;let box:HTMLDivElement;let client:QueryClient;
beforeEach(()=>{sent.length=0;status=200;client=new QueryClient();
  vi.stubGlobal('fetch',vi.fn(async(path:string,init?:RequestInit)=>{
    if(path.endsWith('/csrf'))return new Response(JSON.stringify({csrf_token:'synthetic-csrf'}),{status:200});
    sent.push({path,body:JSON.parse(String(init?.body)),key:new Headers(init?.headers).get('Idempotency-Key')});
    return new Response(JSON.stringify(status===200?{status:'ok'}:{}),{status});}));
  box=document.createElement('div');document.body.append(box);root=createRoot(box);});
afterEach(()=>{act(()=>root.unmount());box.remove();vi.unstubAllGlobals();});
const settle=async()=>{for(let i=0;i<6;i++)await act(async()=>{await new Promise(r=>setTimeout(r,0));});};
const button=(text:string)=>[...box.querySelectorAll('button')].find(b=>b.textContent===text);
const render=(node:ReturnType<typeof h>)=>act(()=>root.render(h(QueryClientProvider,{client},node)));
const submit=async()=>{await act(async()=>{box.querySelector<HTMLButtonElement>('button[type=submit]')!.click();});await settle();};

test('start: nothing sent on the first click; the confirmation names the effect and sends only the experiment id',async()=>{
  render(h(Operations,{member:owner,x:row()}));
  expect(button('停止实验')).toBeUndefined();
  act(()=>button('开始实验')!.click());
  expect(sent).toHaveLength(0);expect(box.textContent).toContain('从现在起按登记分配客户');
  await submit();
  expect(sent.map(s=>[s.path,s.body])).toEqual([['/api/v1/workbench/actions/start_experiment',{experiment_id:'followup.timing'}]]);
});

test('stop is danger-toned and needs the owner\'s own reason, which is what is sent',async()=>{
  render(h(Operations,{member:owner,x:row({status:'started',started_at:'2026-10-28T11:01:00+00:00'})}));
  expect(button('开始实验')).toBeUndefined();
  act(()=>button('停止实验')!.click());
  const confirm=box.querySelector<HTMLButtonElement>('button[type=submit]')!;
  expect(confirm.disabled).toBe(true);expect(confirm.className).toBe('btn-danger');
  const textarea=box.querySelector('textarea')!;
  act(()=>{Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value')!.set!.call(textarea,' 指标异常，先停 ');textarea.dispatchEvent(new Event('input',{bubbles:true}));});
  await submit();
  expect(sent.map(s=>s.body)).toEqual([{experiment_id:'followup.timing',reason:'指标异常，先停'}]);
});

test('an unknown result offers the same request again; once the refetched state shows it started, the start control is gone',async()=>{
  status=503;
  render(h(Operations,{member:owner,x:row()}));
  act(()=>button('开始实验')!.click());await submit();
  expect(box.textContent).toContain('用同一请求重试');
  await submit();
  expect(sent).toHaveLength(2);expect(sent[0].key).toBeTruthy();expect(sent[1].key).toBe(sent[0].key);
  render(h(Operations,{member:owner,x:row({status:'started',started_at:'2026-10-28T11:01:00+00:00'})}));  // the reconciled read
  expect(button('开始实验')).toBeUndefined();expect(button('用同一请求重试')).toBeUndefined();expect(button('停止实验')).toBeDefined();
  render(h(Operations,{member:owner,x:row({status:'stopped'})}));expect(box.textContent).toBe('');
});

test('a role without the operations gets no control',()=>{
  render(h('div',null,h(Operations,{member:operator,x:row()}),h(Register,{member:operator,listed:[]})));
  expect(box.querySelector('button')).toBeNull();
});

test('registration: the draft says what is missing; a complete one is the contract shape; a listed id closes the confirmation',async()=>{
  const draft={...EMPTY_DRAFT,experiment_id:'budget.drill',world:'sim:00000000-0000-4000-8000-000000000001',hypothesis:'预算用尽后拒绝',candidate:'followup.later',
    control:'followup.default',metric:'reply_within_24h',sample_candidate:'20',sample_control:'20',model_cny:'1',ends_at:'2026-10-28T12:00',twin:'trial.service_consumer',
    seed:'20261028',budget_drill:true};
  expect(registrationOf(EMPTY_DRAFT)).toEqual({missing:'实验编号（小写字母开头，可含数字 . _ -）'});
  expect(registrationOf({...draft,sample_control:'0'})).toEqual({missing:'计划样本'});
  const r=registrationOf(draft);
  expect('registration' in r&&r.registration).toMatchObject({schema:'nexloop-experiment-registration/1',experiment_id:'budget.drill',stage:'screening',
    candidate:{strategy_id:'followup.later',version:1},guardrails:{refusal_or_unsubscribe_rate_delta:0.02},budget:{model_cny:1,incentive_cny:0},
    twin:{twin_id:'trial.service_consumer',version:1},allocation_seed:20261028,budget_drill:true});
  expect('registration' in r&&r.registration.ends_at).toBe('2026-10-28T04:00:00Z');  // 12:00 Beijing time, whatever the test machine's zone
  expect(registrationOf({...draft,ends_at:'2026-10-28'})).toEqual({missing:'结束时间'});
  expect('registration' in registrationOf({...draft,twin:''})&&(registrationOf({...draft,twin:''}) as {registration:{twin:unknown}}).registration.twin).toBeNull();
  render(h(Register,{member:owner,listed:[]}));
  act(()=>button('登记新实验')!.click());
  expect(box.textContent).toContain('还需要填写：实验编号');expect(box.textContent).toContain('按北京时间填写，提交时换算为 UTC');expect(button('登记')).toBeUndefined();expect(sent).toHaveLength(0);
  render(h(Register,{member:owner,listed:['']}));
  expect(box.textContent).toContain('这个编号已在列表中');
});

test('W4: the registration form closes with 取消 at its top and with Escape, focus back on its opener; the confirmation panel keeps its own Escape',()=>{
  render(h(Register,{member:owner,listed:[]}));
  act(()=>button('登记新实验')!.click());
  const cancels=[...box.querySelectorAll('button')].filter(b=>b.textContent==='取消');
  expect(cancels.length).toBeGreaterThanOrEqual(1);expect(box.querySelector('.experiment-register-head button')!.textContent).toBe('取消');
  act(()=>cancels[0].click());
  expect(box.querySelector('.experiment-register')).toBeNull();expect(document.activeElement).toBe(button('登记新实验'));
  act(()=>button('登记新实验')!.click());
  const input=box.querySelector<HTMLInputElement>('#exp-hypothesis')!;
  act(()=>{input.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}));});
  expect(box.querySelector('.experiment-register')).toBeNull();expect(document.activeElement).toBe(button('登记新实验'));
  // Escape inside the confirmation panel closes only the panel: the form stays open with what was filled.
  act(()=>button('登记新实验')!.click());
  const fill:Record<string,string>={experiment_id:'esc.check',world:'sim:00000000-0000-4000-8000-000000000001',hypothesis:'h',candidate:'followup.later',
    control:'followup.default',metric:'reply_within_24h',sample_candidate:'20',sample_control:'20',model_cny:'1',ends_at:'2026-10-28T12:00',twin:'trial.service_consumer',seed:'20261028'};
  const setter=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value')!.set!;
  for(const [k,v] of Object.entries(fill))act(()=>{const el=box.querySelector<HTMLInputElement>('#exp-'+k)!;setter.call(el,v);el.dispatchEvent(new Event('input',{bubbles:true}));});
  act(()=>button('登记')!.click());
  const panel=box.querySelector<HTMLElement>('.experiment-register .confirm-panel')!;expect(panel).not.toBeNull();
  act(()=>{panel.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true,cancelable:true}));});
  expect(box.querySelector('.confirm-panel')).toBeNull();expect(box.querySelector('.experiment-register')).not.toBeNull();
  expect(box.querySelector<HTMLInputElement>('#exp-experiment_id')!.value).toBe('esc.check');expect(button('登记')).toBeDefined();
});
