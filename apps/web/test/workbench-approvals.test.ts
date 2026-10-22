import {createElement as h,type ReactNode} from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
import {expect,test} from 'vitest';
import {approvalOutcome,approvals,me} from '../src/workbench/api';
import {Approvals,resultText} from '../src/workbench/pages/Approvals';

const id='3f2b6a52-1c1e-4c55-9d0e-9a2f4a1b7c11';
const base={approval_id:id,capability:'real_dispatch.set',operation:'set_real_dispatch',action:'eios:action:nexloop.real_dispatch.set:1',requested_by:'synthetic-owner',
  requested_at:'2026-10-23T01:00:00+00:00',due_by:'2026-10-24T01:00:00+00:00',status:'waiting_approval',digest:'0123456789ab',policy_version:1,quorum:1,approvals:0,
  summary:{scope_kind:'tenant',enabled:true}};
const raw={to_decide:[base],mine:[{...base,approval_id:id.replace('3f','4f'),result:null}],
  to_execute:[{...base,approval_id:id.replace('3f','5f'),status:'approved',approvals:1,inputs:{scope_kind:'tenant',enabled:true,data_mode:'real',action_type:null,reason:'打开'}}]};
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
  expect(()=>approvals({to_decide:[],mine:[]})).toThrow();
});

test('NX-056 A4: the page offers approve and reject with a reason, execute and withdraw; an operator without the Actions gets no form', ()=>{
  const parsed=approvals(raw);
  const html=render(client([[['workbench','approvals'],parsed],[['workbench','me'],approver]]),h(Approvals,{}));
  for(const t of ['待我审批','待我执行','我发起的','批准','拒绝','执行','撤回','scope_kind=tenant','0123456789ab','0 / 1'])expect(html).toContain(t);
  expect(html).not.toContain('打开');  // the frozen reason is not shown
  const asOperator=render(client([[['workbench','approvals'],parsed],[['workbench','me'],operator]]),h(Approvals,{}));
  expect(asOperator).toContain('待我审批');expect(asOperator).not.toContain('<form');
  const empty=render(client([[['workbench','approvals'],{to_decide:[],mine:[],to_execute:[]}],[['workbench','me'],approver]]),h(Approvals,{}));
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
