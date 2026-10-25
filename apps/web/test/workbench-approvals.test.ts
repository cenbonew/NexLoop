import {createElement as h,type ReactNode} from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
import {expect,test} from 'vitest';
import {approvalOutcome,approvals,me} from '../src/workbench/api';
import {Approvals,resultText,soloBanner,targetText} from '../src/workbench/pages/Approvals';

const id='3f2b6a52-1c1e-4c55-9d0e-9a2f4a1b7c11';
const base={approval_id:id,capability:'real_dispatch.set',operation:'set_real_dispatch',action:'eios:action:nexloop.real_dispatch.set:1',requested_by:'synthetic-owner',
  requested_at:'2026-10-23T01:00:00+00:00',due_by:'2026-10-24T01:00:00+00:00',status:'waiting_approval',digest:'0123456789ab',policy_version:1,quorum:1,approvals:0,
  summary:{scope_kind:'tenant',enabled:true},target_member:null};
const raw={to_decide:[base],mine:[{...base,approval_id:id.replace('3f','4f'),result:null}],
  to_execute:[{...base,approval_id:id.replace('3f','5f'),status:'approved',approvals:1,inputs:{scope_kind:'tenant',enabled:true,data_mode:'real',action_type:null,reason:'季度上线'}}],
  solo_mode:{enabled:false,others_eligible:true}};
const approver=me({principal_id:'synthetic-second',role:'owner',actions:['eios:action:nexloop.workbench.read:1','eios:action:nexloop.approvals.decide:1',
  'eios:action:nexloop.approvals.cancel:1','eios:action:nexloop.real_dispatch.set:1']});
const operator=me({principal_id:'synthetic-operator',role:'operator',actions:['eios:action:nexloop.workbench.read:1']});
function client(seed:[unknown[],unknown][]){const c=new QueryClient({defaultOptions:{queries:{retry:false,staleTime:Infinity}}});for(const [k,v] of seed)c.setQueryData(k,v);return c;}
const render=(c:QueryClient,node:ReactNode)=>renderToStaticMarkup(h(QueryClientProvider,{client:c},node));

test('NX-056 A4: the three columns parse strictly; a decide row never carries a body and only the requester gets inputs', ()=>{
  const parsed=approvals(raw);
  expect(parsed.to_execute[0].inputs?.data_mode).toBe('real');expect(parsed.mine[0].result).toBeNull();
  expect(()=>approvals({...raw,to_decide:[{...base,inputs:{reason:'x'}}]})).toThrow();  // another person's body
  expect(()=>approvals({...raw,to_decide:[{...base,body:{}}]})).toThrow();
  expect(()=>approvals({...raw,to_decide:[{...base,summary:{note:{nested:true}}}]})).toThrow();
  expect(()=>approvals({...raw,to_decide:[{...base,status:'consumed'}]})).toThrow();
  expect(()=>approvals({to_decide:[],mine:[],to_execute:[]})).toThrow();  // the banner's facts are required (A7)
  expect(()=>approvals({...raw,solo_mode:{enabled:true}})).toThrow();
});

test('NX-056 A4: the page offers approve and reject with a reason, execute and withdraw; an operator without the Actions gets no form', ()=>{
  const parsed=approvals(raw);
  const html=render(client([[['workbench','approvals'],parsed],[['workbench','me'],approver]]),h(Approvals,{}));
  for(const t of ['待我审批','待我执行','我发起的','批准','拒绝','执行','撤回','打开真实派发','scope_kind=tenant','0123456789ab','0 / 1'])expect(html).toContain(t);
  expect(html).not.toContain('<code>set_real_dispatch</code>');  // the Action in words, not its operation code
  expect(html).not.toContain('季度上线');  // the frozen reason is not shown
  const asOperator=render(client([[['workbench','approvals'],parsed],[['workbench','me'],operator]]),h(Approvals,{}));
  expect(asOperator).toContain('待我审批');expect(asOperator).not.toContain('<form');
  const empty=render(client([[['workbench','approvals'],{to_decide:[],mine:[],to_execute:[],solo_mode:{enabled:false,others_eligible:true}}],[['workbench','me'],approver]]),h(Approvals,{}));
  expect(empty).toContain('没有待你审批的请求');expect(empty).toContain('没有已批准待执行的请求');
});

test('NX-056 A4: a governed write under an approval policy says what happened to its approval', ()=>{
  expect(approvalOutcome({approval:'required',status:'waiting_approval',approval_id:id})).toContain('已提交审批');
  expect(approvalOutcome({status:'waiting_approval',approval_id:id,reason:'approver_no_longer_eligible'})).toContain('回到等待审批');
  expect(approvalOutcome({approval:'consumed',approval_id:id})).toContain('已按批准执行');
  expect(approvalOutcome({status:'execution_refused',code:'P0001',approval:'consumed'})).toContain('P0001');
  expect(approvalOutcome({status:'approval_consumed',result:'executed'})).toContain('没有再次执行');
  expect(approvalOutcome({status:'policy_changed'})).toContain('重新提交');
  expect(approvalOutcome({control_revision:3})).toBeNull();
  expect(resultText('execution_refused:P0001')).toBe('执行被拒（P0001）');expect(resultText(null)).toBe('—');
});

test('NX-056 A4 review 7: a frozen body the workbench cannot resubmit is listed without an execute form', ()=>{
  const notHere=approvals({...raw,to_decide:[],mine:[],to_execute:[{...raw.to_execute[0],inputs:null}]});
  expect(notHere.to_execute[0].inputs).toBeNull();
  const html=render(client([[['workbench','approvals'],notHere],[['workbench','me'],approver]]),h(Approvals,{}));
  expect(html).toContain('工作台不能执行这条请求');expect(html).not.toContain('aria-label="执行"');
});

test('NX-056 A7: the single-person mode banner and the owner\'s switch', ()=>{
  expect(soloBanner({enabled:false,others_eligible:false})).toBeNull();
  expect(soloBanner({enabled:true,others_eligible:false})).toContain('单人模式已开启');
  expect(soloBanner({enabled:true,others_eligible:true})).toContain('单人模式未生效');
  const owner=me({principal_id:'synthetic-owner',role:'owner',actions:['eios:action:nexloop.workbench.read:1','eios:action:nexloop.approvals.solo_mode.set:1']});
  const on=approvals({...raw,solo_mode:{enabled:true,others_eligible:false}});
  const html=render(client([[['workbench','approvals'],on],[['workbench','me'],owner]]),h(Approvals,{}));
  expect(html).toContain('单人模式已开启');expect(html).toContain('关闭单人模式');
  const asOperator=render(client([[['workbench','approvals'],on],[['workbench','me'],operator]]),h(Approvals,{}));
  expect(asOperator).not.toContain('关闭单人模式');
});

test('NX-056 B1: target_member parses strictly in its kinds and renders who the request acts on', ()=>{
  const member={kind:'member',label:'p-0a1b2c3d',username:'staff-ops',account_status:'active',member_status:'active',role:'operator'};
  const parsed=approvals({...raw,to_decide:[{...base,target_member:member}]});
  expect(parsed.to_decide[0].target_member).toEqual(member);
  expect(targetText(parsed.to_decide[0].target_member)).toBe('staff-ops p-0a1b2c3d（operator，active）');
  for(const kind of ['hidden','unresolved','erased'])expect(approvals({...raw,to_decide:[{...base,target_member:{kind}}]}).to_decide[0].target_member).toEqual({kind});
  expect(targetText({kind:'hidden'})).toContain('仅 owner');expect(targetText({kind:'erased'})).toBe('已擦除');expect(targetText(null)).toBe('—');
  const invitee={kind:'invitee',username:'new-owner',same_username_exists:false};
  expect(approvals({...raw,to_decide:[{...base,target_member:invitee}]}).to_decide[0].target_member).toEqual(invitee);
  expect(targetText(invitee)).toBe('邀请：new-owner');expect(targetText({...invitee,same_username_exists:true})).toContain('应拒绝');
  for(const bad of [{...member,principal_id:'x'},{...member,label:'full-principal-id'},{kind:'hidden',username:'x'},{kind:'invitee',username:'x'}])
    expect(()=>approvals({...raw,to_decide:[{...base,target_member:bad}]})).toThrow();
  const {target_member:_,...without}=base;
  expect(()=>approvals({...raw,to_decide:[without]})).toThrow();  // the key is always there
});
