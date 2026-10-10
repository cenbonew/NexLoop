import {createElement as h,type ReactNode} from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
import {afterEach,expect,test,vi} from 'vitest';
import {WRITE_TEXT,actions,consumer,me,overview,readActions,readConsumer,takeovers,workbenchAct,type Me} from '../src/workbench/api';
import {CommitmentControls,ConsumerControls,ConversationControls,DONE_TEXT,GOVERNED_SLOTS,governedWrite,takeoverText} from '../src/workbench/actions';
import {Actions} from '../src/workbench/pages/Actions';
import {WorkbenchPage} from '../src/workbench/WorkbenchApp';

const id=(c:string)=>c.repeat(64);
const ROLES:Record<'owner'|'operator',string[]>={
  owner:['nexloop.workbench.read','nexloop.contact.read','nexloop.commitment.read','Control.set','Contact.release','Commitment.cancel','Commitment.extend','Commitment.attest',
    'Commitment.condition_met','Commitment.mark_communication','nexloop.plan.request_reevaluation','nexloop.service.query_request','nexloop.conversation.takeover',
    'nexloop.conversation.handback','nexloop.message.staff_send'].map(a=>`eios:action:${a}:1`),
  operator:['nexloop.workbench.read','nexloop.contact.read','nexloop.commitment.read','Commitment.extend','Commitment.attest','Commitment.condition_met',
    'nexloop.plan.request_reevaluation','nexloop.service.query_request','nexloop.conversation.takeover','nexloop.conversation.handback','nexloop.message.staff_send'].map(a=>`eios:action:${a}:1`),
};
const member=(role:'owner'|'operator'):Me=>me({principal_id:'synthetic-staff-'+role,role,actions:ROLES[role]});
const intent=(reason:string|null)=>({intent_id:'0b0c6c57-0000-4000-8000-000000000001',receipt_id:'r',consumer_id:id('2'),action_name:'nexloop.service.refund',action_version:1,
  state:'accepted',created_at:'2026-10-10T09:00:00+00:00',control_revision:3,attempts:0,observation:null,age_seconds:5,
  dispatch:{status:'ok',prediction:{dispatchable:reason===null,reason,detail:{}}}});
const consumerView=(paused:boolean)=>({consumer_id:id('2'),paused,restriction:null,conversations:[],commitments:[],properties:{status:'ok',values:{},withheld:[]}});
const conversationView={conversation_id:id('5'),consumer_id:id('2'),messages:[
  {id:id('6'),sequence:1,accepted_at:'2026-10-10T09:00:00+00:00',direction:'inbound',sender_kind:'consumer',reply_to_message_id:null,provider:null,content:{status:'ok',actor:'c',body:'付款页面一直报错'}}]};
const takeover=(mine:boolean)=>({status:'ok',policy:{default_seconds:7200,max_seconds:86400},takeovers:[{takeover_id:'0b0c6c57-0000-4000-8000-0000000000aa',scope_kind:'conversation',
  scope_ref:id('5'),consumer_id:id('2'),taken_by:mine?'synthetic-staff-operator':'synthetic-staff-owner',mine,reason:'人工处理',started_at:'2026-10-10T09:00:00+00:00',expires_at:'2026-10-10T11:00:00+00:00'}]});

afterEach(()=>vi.unstubAllGlobals());

function client(seed:[unknown[],unknown][]){
  const c=new QueryClient({defaultOptions:{queries:{retry:false,staleTime:Infinity}}});
  for(const [key,value] of seed)c.setQueryData(key,value);
  return c;
}
const render=(c:QueryClient,node:ReactNode)=>renderToStaticMarkup(h(QueryClientProvider,{client:c},node));

/** A fake API: csrf + one write route, recording each write's headers and body. */
function server(handler:(operation:string,body:Record<string,unknown>)=>{status:number;body:unknown}|'network'){
  const writes:{operation:string;key:string;body:Record<string,unknown>}[]=[];
  vi.stubGlobal('fetch',vi.fn(async(url:string,init?:RequestInit)=>{
    if(url==='/api/v1/workbench/auth/csrf')return new Response(JSON.stringify({authenticated:true,csrf_token:'t'}),{status:200});
    const operation=url.replace('/api/v1/workbench/actions/','');const body=JSON.parse(String(init?.body));
    const headers=new Headers(init?.headers);writes.push({operation,key:headers.get('Idempotency-Key')!,body});
    expect(headers.get('X-CSRF-Token')).toBe('t');
    const r=handler(operation,body);if(r==='network')throw new TypeError('down');
    return new Response(typeof r.body==='string'?r.body:JSON.stringify(r.body),{status:r.status});
  }));
  return writes;
}

test('AT-045 writes: every outcome has its exact kind; 503, network loss and an unreadable 200 are "unknown", never failed or done',async()=>{
  const cases:[{status:number;body:unknown}|'network',string][]=[[{status:401,body:{code:'unauthenticated'}},'unauthenticated'],[{status:403,body:{code:'forbidden'}},'forbidden'],
    [{status:404,body:{code:'not_found'}},'not_found'],[{status:422,body:{code:'invalid_request'}},'invalid_request'],[{status:409,body:{code:'not_allowed_in_state'}},'not_allowed_in_state'],
    [{status:409,body:{code:'conflict'}},'conflict'],[{status:503,body:{code:'unavailable'}},'unavailable'],['network','unavailable'],[{status:200,body:'not json'},'invalid']];
  for(const [response,kind] of cases){server(()=>response);await expect(workbenchAct('set_control',{},'wb-key-0000000000000001')).rejects.toMatchObject({kind});}
  // The CSRF token could not be obtained: nothing was sent.
  vi.stubGlobal('fetch',vi.fn(async()=>new Response('{}',{status:503})));
  await expect(workbenchAct('set_control',{},'wb-key-0000000000000001')).rejects.toMatchObject({kind:'not_sent'});
  expect(WRITE_TEXT.unavailable).toContain('无法确认是否已执行');expect(WRITE_TEXT.unavailable).toContain('不会重复执行');
  expect(WRITE_TEXT.forbidden).toContain('403');expect(WRITE_TEXT.not_allowed_in_state).toContain('409');expect(WRITE_TEXT.not_sent).toContain('未提交');
  expect(DONE_TEXT.send_staff_reply).toContain('不代表问题已解决');expect(DONE_TEXT.request_effect_query).toContain('不会重发');
});

test('an unknown result is retried with the same Idempotency-Key; a definite outcome starts a new request',async()=>{
  let answer:{status:number;body:unknown}={status:503,body:{code:'unavailable'}};
  const writes=server(()=>answer);const cache=client([]);const pending={current:null as {body:string;key:string}|null};
  const body={scope_kind:'tenant',scope_ref:'*',paused:true,reason:'x'};
  await expect(governedWrite(cache,'set_control',body,pending)).rejects.toMatchObject({kind:'unavailable'});
  await expect(governedWrite(cache,'set_control',body,pending)).rejects.toMatchObject({kind:'unavailable'});
  answer={status:200,body:{operation:'set_control'}};await governedWrite(cache,'set_control',body,pending);
  answer={status:409,body:{code:'not_allowed_in_state'}};await expect(governedWrite(cache,'set_control',body,pending)).rejects.toMatchObject({kind:'not_allowed_in_state'});
  await expect(governedWrite(cache,'set_control',body,pending)).rejects.toMatchObject({kind:'not_allowed_in_state'});
  const keys=writes.map(w=>w.key);
  expect(keys[0]).toBe(keys[1]);expect(keys[2]).toBe(keys[0]);  // the replay of the unknown request is the request that succeeded
  expect(new Set([keys[0],keys[3],keys[4]]).size).toBe(3);expect(keys.every(k=>/^[A-Za-z0-9._~-]{16,160}$/.test(k))).toBe(true);
});

test('D2: an operator is not offered owner-only controls; the owner is',()=>{
  const seeds=(role:'owner'|'operator'):[unknown[],unknown][]=>[[['workbench','me'],member(role)],[['workbench','contact',false],{restrictions:[],escalations:[]}],
    [['workbench','consumer',id('2')],consumer(consumerView(false))],[['workbench','takeovers'],{...takeovers(takeover(false)),takeovers:[]}],
    [['workbench','commitment',id('1')],{commitment_id:id('1'),consumer_id:id('2'),revision:1,status:'open',properties:{status:'open'},quote:null,quote_status:'none',origin:'claim',
      evidence:{requested:[],delivered:[],customer_confirmed:[],problem_resolved:'unavailable'},events:[],exceptions:[]}]];
  const page=(role:'owner'|'operator',route:Parameters<typeof WorkbenchPage>[0]['route'])=>render(client(seeds(role)),h(WorkbenchPage,{route,go:()=>{},slots:GOVERNED_SLOTS}));
  for(const role of ['owner','operator'] as const){
    const goals=page(role,{page:'goals'}),contact=page(role,{page:'contact'}),detail=page(role,{page:'consumers',id:id('2')});
    const commitment=render(client(seeds(role)),h(CommitmentControls,{commitmentId:id('1')}));
    const owner=role==='owner';
    const form=(html:string,title:string)=>html.includes(`aria-label="${title}"`);
    expect(form(goals,'暂停 / 恢复')).toBe(owner);expect(form(contact,'解除联系限制')).toBe(owner);expect(form(detail,'暂停主动联系')).toBe(owner);
    expect(form(commitment,'取消承诺')).toBe(owner);expect(form(commitment,'标记为沟通类承诺')).toBe(owner);
    // Both roles: extend / attest a commitment, take a consumer over.
    expect(form(commitment,'延期')).toBe(true);expect(form(commitment,'人工证明已兑现')).toBe(true);expect(form(detail,'接管整个客户')).toBe(true);
  }
  // Without the role nothing is guessed: the action area says it is unavailable.
  const unknownRole=client([]);unknownRole.setQueryDefaults(['workbench','me'],{queryFn:()=>new Promise(()=>{})});
  expect(render(unknownRole,h(ConsumerControls,{consumerId:id('2')}))).toContain('正在加载');
});

test('takeover page: state, hand-back, and the staff reply only for the member who took it over',()=>{
  const base:[unknown[],unknown][]=[[['workbench','me'],member('operator')],[['workbench','conversation',id('5')],conversationView]];
  const none=render(client([...base,[['workbench','takeovers'],{...takeovers(takeover(true)),takeovers:[]}]]),h(ConversationControls,{conversationId:id('5')}));
  expect(none).toContain('接管本会话');expect(none).toContain('value="120"');expect(none).not.toContain('以人工客服身份回复');
  const mine=render(client([...base,[['workbench','takeovers'],takeovers(takeover(true))]]),h(ConversationControls,{conversationId:id('5')}));
  expect(mine).toContain('人工接管中（本会话）：由我接管');expect(mine).toContain('交还给 Agent');expect(mine).toContain('以人工客服身份回复');expect(mine).toContain('付款页面一直报错');
  const others=render(client([...base,[['workbench','takeovers'],takeovers(takeover(false))]]),h(ConversationControls,{conversationId:id('5')}));
  expect(others).toContain('由 synthetic-staff-owner接管');expect(others).toContain('只有发起接管的成员可以在接管期间回复');expect(others).not.toContain('以人工客服身份回复');
});

test('AT-006 interface part: after pausing in the workbench the next read shows the same refusal dispatch will apply; resume is not replay',async()=>{
  // The fake API stands in for SQL: the prediction it returns is the one dispatch uses (0124 control.nexloop_intent_dispatch_prediction).
  let state:{paused:boolean;reason:string|null}={paused:false,reason:null};
  server((operation,body)=>{expect(operation).toBe('set_control');
    state=body.paused?{paused:true,reason:'control_paused'}:{paused:false,reason:'control_revision_stale'};return {status:200,body:{operation}};});
  const reads=vi.fn(async(url:string,init?:RequestInit)=>{
    if(url.startsWith('/api/v1/workbench/actions/')||url==='/api/v1/workbench/auth/csrf')return writeFetch(url,init);
    if(url==='/api/v1/workbench/actions')return new Response(JSON.stringify({intents:[intent(state.reason)],unknown:{count:0,oldest_age_seconds:null}}),{status:200});
    return new Response(JSON.stringify(consumerView(state.paused)),{status:200});
  });
  const writeFetch=globalThis.fetch as unknown as (url:string,init?:RequestInit)=>Promise<Response>;
  vi.stubGlobal('fetch',reads);
  const cache=client([[['workbench','me'],member('owner')],[['workbench','takeovers'],{...takeovers(takeover(true)),takeovers:[]}]]);
  const show=async()=>{
    await cache.fetchQuery({queryKey:['workbench','actions',''],queryFn:()=>readActions()});
    await cache.fetchQuery({queryKey:['workbench','consumer',id('2')],queryFn:()=>readConsumer(id('2'))});
    return {actions:render(cache,h(Actions,{})),consumer:render(cache,h(ConsumerControls,{consumerId:id('2')}))};
  };
  let view=await show();expect(view.actions).toContain('可派发');expect(view.consumer).toContain('暂停主动联系');
  const pending={current:null};
  await governedWrite(cache,'set_control',{scope_kind:'consumer',scope_ref:id('2'),paused:true,reason:'客户投诉，先暂停'},pending);
  view=await show();expect(view.actions).toContain('已暂停');expect(view.actions).not.toContain('可派发');expect(view.consumer).toContain('恢复主动联系');
  await governedWrite(cache,'set_control',{scope_kind:'consumer',scope_ref:id('2'),paused:false,reason:'已处理'},pending);
  view=await show();expect(view.actions).toContain('控制已变更，派发时将被拒，等待复评');expect(view.consumer).toContain('暂停主动联系');
  expect(actions({intents:[intent('taken_over')],unknown:{count:0,oldest_age_seconds:null}}).intents[0].dispatch).toEqual({status:'ok',prediction:{dispatchable:false,reason:'taken_over'}});
});

test('overview takeover block: the takeovers in effect, labelled; an unavailable block is never "none"',()=>{
  const sections=(t:unknown)=>({goals:{status:'unavailable',data:null},commitments:{status:'unavailable',data:null},actions:{status:'unavailable',data:null},
    contact:{status:'unavailable',data:null},takeovers:t,backlog:{status:'unavailable',data:null},commercial:{status:'unavailable',data:null}});
  const parsed=overview(sections({status:'ok',data:takeover(false)}));
  expect(parsed.takeovers.status==='ok'&&parsed.takeovers.data.map(takeoverText)).toEqual(['人工接管中（本会话）：由 synthetic-staff-owner接管，到期 2026-10-10T11:00:00+00:00']);
  expect(overview(sections({status:'unavailable',data:null})).takeovers).toEqual({status:'unavailable',data:null});
});
