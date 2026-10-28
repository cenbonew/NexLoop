// @vitest-environment jsdom
// UX audit S6 (docs/design/workbench-ux-audit.md B10, B11): the customer detail leads with what is used most (latest conversations,
// commitments, operations), then persona and preferences, properties and the contact restriction, which moves up with a warning while
// it is in force; an empty section is one line. One customer's persona is assigned here (the customer is known; nothing is pasted),
// and the persona page only points here.
import {createElement as h} from 'react';
import {act} from 'react';
import {createRoot,type Root} from 'react-dom/client';
import {renderToStaticMarkup} from 'react-dom/server';
import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
import {afterEach,beforeEach,expect,test,vi} from 'vitest';
import {consumer,contactPreferences,me,OPERATION_ACTION,personaOverview} from '../src/workbench/api';
import {ConsumerDetail} from '../src/workbench/pages/Consumers';
import {Personas} from '../src/workbench/pages/Personas';

(globalThis as {IS_REACT_ACT_ENVIRONMENT?:boolean}).IS_REACT_ACT_ENVIRONMENT=true;
const cid='c'.repeat(64);
const conv=(n:number)=>({conversation_id:String(n%10).repeat(64),last_sequence:n,last_message_at:`2026-10-${String(10+n).padStart(2,'0')}T08:00:00+00:00`});
const detail=(over:Record<string,unknown>={})=>consumer({consumer_id:cid,paused:false,restriction:null,conversations:[],commitments:[],
  properties:{status:'ok',values:{},withheld:[]},...over});
const restriction={active:true,control_revision:7,rule_id:'stop-contact',restricted_at:'2026-10-20T08:00:00+00:00',released_at:null};
const doc={display_name:'小禾',tone:'亲切',expression:{max_chars:200,emoji:'few'},ai_disclosure:{deflect_statement:'我是本店的服务助手。'}};
const PERSONAS=[{persona_id:'xiaohe',version:2,status:'published',display_name:'小禾',created_at:'2026-10-19T02:00:00+00:00',document:doc},
  {persona_id:'lan',version:1,status:'published',display_name:'小蓝',created_at:'2026-10-19T02:00:00+00:00',document:{...doc,display_name:'小蓝'}}];
const overview=(desired:unknown=null,personas:unknown[]=PERSONAS)=>personaOverview({personas,
  rules:null,ai_disclosure:{mode:'none',when_asked:'deflect',block_human_claims:false,content_label:false,revision:0},
  consumer:{consumer_id:cid,desired,settled:null,events:[]}});
const owner=me({principal_id:'synthetic-owner',role:'owner',actions:['eios:action:nexloop.workbench.read:1',OPERATION_ACTION.assign_operator_persona,OPERATION_ACTION.publish_persona_rules]});
const operator=me({principal_id:'synthetic-operator',role:'operator',actions:['eios:action:nexloop.workbench.read:1']});
function client(view:unknown,member=owner,persona:unknown=overview()){
  const c=new QueryClient({defaultOptions:{queries:{retry:false,staleTime:Infinity}}});
  c.setQueryData(['workbench','consumer',cid],view);c.setQueryData(['workbench','me'],member);
  c.setQueryData(['workbench','operator-personas',cid],persona);c.setQueryData(['workbench','contact-preferences',cid],contactPreferences({preferences:[]}));
  c.setQueryData(['workbench','operator-personas'],persona);
  return c;
}
const page=(c:QueryClient)=>renderToStaticMarkup(h(QueryClientProvider,{client:c},h(ConsumerDetail,{consumerId:cid,onConversation:()=>{},actions:h('p',null,'操作区')})));
const headings=(html:string)=>[...html.matchAll(/<h2[^>]*>([^<]+)<\/h2>/g)].map(m=>m[1]);

test('the order: conversations, commitments, operations, persona, properties, then the restriction; empty sections are one line',()=>{
  const html=page(client(detail({conversations:[conv(1)],commitments:['d'.repeat(64)]})));
  expect(headings(html)).toEqual(['会话','承诺','对这位客户的操作','运营人设与沟通偏好','属性','联系限制']);
  expect(html).toMatch(/<section class="detail-section detail-empty" aria-labelledby="consumer-restriction"><h2 id="consumer-restriction">联系限制<\/h2><p class="note">无<\/p><\/section>/);
  expect(html).toMatch(/<h2 id="consumer-properties">属性<\/h2><p class="note">没有可读的 Property<\/p>/);
  expect(html).not.toContain('这位客户受联系限制');
  const empty=page(client(detail()));
  expect(empty).toContain('<h2 id="consumer-conversations">会话</h2><p class="note">还没有会话</p>');
  expect(empty).toContain('<h2 id="consumer-commitments">承诺</h2><p class="note">没有承诺</p>');
});

test('an active restriction comes second with a warning; a paused customer is said once, as a notice',()=>{
  const html=page(client(detail({restriction,paused:true,conversations:[conv(1)]})));
  expect(headings(html)).toEqual(['联系限制','会话','承诺','对这位客户的操作','运营人设与沟通偏好','属性']);
  expect(html).toContain('这位客户受联系限制');expect(html).toContain('stop-contact');
  expect(html.indexOf('主动联系已暂停')).toBeLessThan(html.indexOf('<h2 id="consumer-restriction">'));
  const released=page(client(detail({restriction:{...restriction,active:false,released_at:'2026-10-21T08:00:00+00:00'}})));
  expect(headings(released).at(-1)).toBe('联系限制');expect(released).toContain('已解除');expect(released).not.toContain('这位客户受联系限制');
});

test('withheld property groups are still named when nothing else is readable; forbidden properties are one line',()=>{
  expect(page(client(detail({properties:{status:'ok',values:{},withheld:['finance']}})))).toMatch(/没有可读的 Property；负责人标为受限的属性组不显示：<span><code class="id-code">finance<\/code>/);
  expect(page(client(detail({properties:{status:'forbidden',values:null,withheld:[]}})))).toContain('<h2 id="consumer-properties">属性</h2><p class="note">当前账号没有 Property 读取权限');
});

let root:Root;let box:HTMLDivElement;
const sent:{path:string;body:unknown}[]=[];
beforeEach(()=>{sent.length=0;
  vi.stubGlobal('fetch',vi.fn(async(path:string,init?:RequestInit)=>{
    if(path.endsWith('/csrf'))return new Response(JSON.stringify({csrf_token:'synthetic-csrf'}),{status:200});
    if(init?.method==='POST'){sent.push({path,body:JSON.parse(String(init.body))});return new Response(JSON.stringify({status:'ok'}),{status:200});}
    return new Response('{}',{status:503});}));
  box=document.createElement('div');document.body.append(box);root=createRoot(box);});
afterEach(()=>{act(()=>root.unmount());box.remove();vi.unstubAllGlobals();});
const settle=async()=>{for(let i=0;i<6;i++)await act(async()=>{await new Promise(r=>setTimeout(r,0));});};
const button=(text:string)=>[...box.querySelectorAll('button')].find(b=>b.textContent===text);
const mount=(c:QueryClient,node=h(ConsumerDetail,{consumerId:cid,onConversation:()=>{}}))=>act(()=>root.render(h(QueryClientProvider,{client:c},node)));
const setValue=(el:HTMLInputElement|HTMLSelectElement,value:string)=>act(()=>{
  Object.getOwnPropertyDescriptor(Object.getPrototypeOf(el),'value')!.set!.call(el,value);el.dispatchEvent(new Event(el.tagName==='SELECT'?'change':'input',{bubbles:true}));});

test('the latest 5 conversations first, newest on top; the rest one click away and back',()=>{
  mount(client(detail({conversations:[1,2,3,4,5,6,7].map(conv)})));
  const items=()=>[...box.querySelectorAll('.conversation-item')].map(b=>b.querySelector('.conversation-meta')!.textContent);
  expect(items()).toHaveLength(5);expect(items()[0]).toContain('7 条消息');
  const more=button('查看全部 7 个会话')!;expect(more.getAttribute('aria-expanded')).toBe('false');
  act(()=>more.click());
  expect(items()).toHaveLength(7);expect(button('只看最近 5 个')!.getAttribute('aria-expanded')).toBe('true');
  act(()=>button('只看最近 5 个')!.click());expect(items()).toHaveLength(5);
});

test('the persona is assigned on the customer page: the customer is fixed, a persona and a reason are what the member gives',async()=>{
  mount(client(detail()));
  const form=box.querySelector<HTMLFormElement>('form[aria-label="为这位客户指定人设"]')!;
  expect(form).not.toBeNull();expect(form.querySelector('#assign-consumer')).toBeNull();
  const submit=form.querySelector<HTMLButtonElement>('button[type=submit]')!;
  expect(submit.disabled).toBe(true);expect(form.querySelector('.disabled-reason')?.textContent).toBe('先选择一个人设。');
  setValue(form.querySelector('select')!,'lan');
  expect(form.querySelector('.disabled-reason')?.textContent).toBe('填写原因后才能提交。');
  setValue(form.querySelector<HTMLInputElement>('#assign-reason')!,' 老客户偏好小蓝 ');
  expect(submit.disabled).toBe(false);
  await act(async()=>{submit.click();});await settle();
  expect(sent).toEqual([{path:'/api/v1/workbench/actions/assign_operator_persona',body:{consumer_id:cid,persona_id:'lan',reason:'老客户偏好小蓝'}}]);
});

test('an owner assignment preselects its persona; clearing it goes back to the rules; a member without the Action gets no form',()=>{
  mount(client(detail(),owner,overview({persona_id:'xiaohe',source:'owner_assign',rules_version:null})));
  const select=box.querySelector<HTMLSelectElement>('#assign-persona')!;
  expect(select.value).toBe('xiaohe');expect(select.options[0].textContent).toBe('不指定（回到分群规则和租户默认）');
  expect(box.querySelector('.disabled-reason')?.textContent).toBe('选择另一个人设，或选“不指定”回到规则。');
  act(()=>root.unmount());root=createRoot(box);
  mount(client(detail(),operator));
  expect(box.querySelector('form[aria-label="为这位客户指定人设"]')).toBeNull();
  expect(box.textContent).toContain('运营人设与沟通偏好');
  // No published persona: nothing to choose, so no form that could never be sent.
  act(()=>root.unmount());root=createRoot(box);
  mount(client(detail(),owner,overview(null,[])));
  expect(box.querySelector('form[aria-label="为这位客户指定人设"]')).toBeNull();expect(box.textContent).toContain('暂无生效的人设');
});

test('the assignment tab of the persona page leads to the customer list',()=>{
  const onConsumers=vi.fn();
  mount(client(detail()),h(Personas,{onConsumers}));
  act(()=>[...box.querySelectorAll<HTMLButtonElement>('[role=tab]')].find(t=>t.textContent==='分配')!.click());
  expect(box.textContent).toContain('为单个客户指定人设：在客户详情的“运营人设与沟通偏好”里操作。');
  expect(box.querySelector('#assign-consumer')).toBeNull();
  act(()=>button('去客户列表')!.click());expect(onConsumers).toHaveBeenCalledTimes(1);
});
