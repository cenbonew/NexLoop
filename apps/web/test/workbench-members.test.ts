// NX-058 workbench UI, part 2: the owner's members page (facts first, then the controls each row allows), the one-time secret, and the
// approvals page naming the member Actions.
import {createElement as h,type ReactNode} from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
import {expect,test} from 'vitest';
import {me,members,oneTimeSecret,OPERATION_ACTION} from '../src/workbench/api';
import {OneTimeSecret} from '../src/workbench/actions';
import {Members,memberControls} from '../src/workbench/pages/Members';
import {APPROVAL_ACTION_TEXT} from '../src/workbench/pages/Approvals';

const row=(principal:string,role:string|null,status:string,extra:Record<string,unknown>={})=>({principal_id:principal,label:'p-0123abcd',username:principal.replace('synthetic-',''),
  role,member_status:status,account_status:'active',vote_eligible_from:role==='owner'?'2026-10-20T00:00:00+00:00':null,erased:false,...extra});
const raw={members:[row('synthetic-owner','owner','active'),row('synthetic-second','owner','active'),row('synthetic-op','operator','active'),
    row('synthetic-paused','reviewer','suspended'),row('synthetic-gone','operator','revoked',{role:null})],
  invitations:[{invitation_id:'3f2b6a52-1c1e-4c55-9d0e-9a2f4a1b7c11',username:'new-reviewer',role:'reviewer',issued_by:'synthetic-owner',issued_at:'2026-10-25T01:00:00+00:00',expires_at:'2026-10-26T01:00:00+00:00'}],
  resets:[{reset_id:'4f2b6a52-1c1e-4c55-9d0e-9a2f4a1b7c11',target_principal:'synthetic-op',issued_by:'synthetic-owner',issued_at:'2026-10-25T01:00:00+00:00',expires_at:'2026-10-25T01:30:00+00:00',state:'completed',completed_at:'2026-10-25T01:10:00+00:00',kind:'member'}]};
const owner=me({principal_id:'synthetic-owner',role:'owner',actions:['eios:action:nexloop.workbench.read:1',...Object.values(OPERATION_ACTION).filter(a=>a.includes('workbench_member'))]});
const operator=me({principal_id:'synthetic-op',role:'operator',actions:['eios:action:nexloop.workbench.read:1']});
function render(member:ReturnType<typeof me>,data:unknown){const c=new QueryClient({defaultOptions:{queries:{retry:false,staleTime:Infinity}}});
  c.setQueryData(['workbench','members'],data);c.setQueryData(['workbench','me'],member);
  return renderToStaticMarkup(h(QueryClientProvider,{client:c},h(Members,{})) as ReactNode);}

test('NX-058 UI: the members view parses strictly and refuses a row carrying anything else (a secret, an email)', ()=>{
  expect(members(raw).members).toHaveLength(5);
  expect(()=>members({...raw,members:[{...raw.members[0],email:'x@example.invalid'}]})).toThrow();
  expect(()=>members({...raw,invitations:[{...raw.invitations[0],token_hmac:'a'.repeat(64)}]})).toThrow();
  expect(()=>members({...raw,members:[{...raw.members[0],label:'synthetic-owner'}]})).toThrow();  // never a full principal as the label
  expect(()=>members({...raw,resets:[{...raw.resets[0],kind:'admin'}]})).toThrow();
});

test('NX-058 UI: each row offers only what its role and status allow; never the owner itself; a revoked member only the erasure', ()=>{
  const m=members(raw).members;
  expect(memberControls(m[0],'synthetic-owner')).toEqual([]);
  expect(memberControls(m[1],'synthetic-owner')).toEqual(['demote','suspend_owner','revoke_owner','issue_owner_reset']);
  expect(memberControls(m[2],'synthetic-owner')).toEqual(['change_role','suspend','revoke','issue_reset','promote']);
  expect(memberControls(m[3],'synthetic-owner')).toEqual(['resume','revoke']);
  expect(memberControls(m[4],'synthetic-owner')).toEqual(['erase']);
  expect(memberControls({...m[4],erased:true},'synthetic-owner')).toEqual([]);
});

test('NX-058 UI: the owner sees facts then controls; an operator gets no form', ()=>{
  const html=render(owner,members(raw));
  for(const t of ['成员与账号','未接受的邀请','new-reviewer','最近 90 天的重置码','已完成','邀请成员','邀请 owner','改角色','提升为 owner','降级这位 owner','擦除这位成员的个人数据',
    '需要另一位 owner 批准'])expect(html).toContain(t);
  expect(html.indexOf('<h2>成员</h2>')).toBeLessThan(html.indexOf('aria-label="改角色"'));
  const asOperator=render(operator,members({...raw,members:raw.members.slice(2,3)}));
  expect(asOperator).not.toContain('<form');
});

test('NX-058 UI: a reset code or invitation token is shown once, with the link; a sqlstate code is no secret', ()=>{
  expect(oneTimeSecret({code:'ABCD-EFGH-IJKL-MNOP-QRST',replayed:false})).toEqual({kind:'code',value:'ABCD-EFGH-IJKL-MNOP-QRST'});
  expect(oneTimeSecret({token:'t'.repeat(43)})?.kind).toBe('token');
  expect(oneTimeSecret({status:'execution_refused',code:'22023'})).toBeNull();
  const html=renderToStaticMarkup(h(OneTimeSecret,{kind:'token',value:'t'.repeat(43)}));
  expect(html).toContain('只显示这一次');expect(html).toContain('/workbench/invitation#token=');
});

test('NX-058 UI: the approvals page names every owner-only member Action', ()=>{
  for(const op of ['promote_workbench_member','demote_workbench_owner','suspend_workbench_owner','resume_workbench_owner','revoke_workbench_owner',
    'issue_workbench_owner_reset','invite_workbench_owner','erase_workbench_member'])expect(APPROVAL_ACTION_TEXT[op]).toBeTruthy();
});
