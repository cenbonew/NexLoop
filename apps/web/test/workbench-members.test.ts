// NX-058 workbench UI, part 2: the owner's members page (facts first, then the controls each row allows), the one-time secret, and the
// approvals page naming the member Actions.
import {createElement as h,type ReactNode} from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
import {expect,test} from 'vitest';
import {me,members,oneTimeSecret,OPERATION_ACTION} from '../src/workbench/api';
import {OneTimeSecret,WriteResult} from '../src/workbench/actions';
import {MemberControlPanel,Members,approversFor,memberControls} from '../src/workbench/pages/Members';
import {APPROVAL_ACTION_TEXT} from '../src/workbench/pages/Approvals';

const row=(principal:string,role:string|null,status:string,extra:Record<string,unknown>={})=>({principal_id:principal,label:'p-0123abcd',username:principal.replace('synthetic-',''),
  role,member_status:status,account_status:'active',vote_eligible_from:role==='owner'?'2026-10-20T00:00:00+00:00':null,erased:false,
  person_all_revoked:status==='revoked',person:'s-'+Array.from(principal).reduce((a,c)=>(a*31+c.charCodeAt(0))>>>0,7).toString(16).padStart(8,'0').slice(-8),human_active:true,...extra});
const raw={members:[row('synthetic-owner','owner','active'),row('synthetic-second','owner','active'),row('synthetic-op','operator','active'),
    row('synthetic-paused','reviewer','suspended'),row('synthetic-gone','operator','revoked',{role:null})],
  invitations:[{invitation_id:'3f2b6a52-1c1e-4c55-9d0e-9a2f4a1b7c11',username:'new-reviewer',role:'reviewer',issued_by:'synthetic-owner',issued_at:'2026-10-25T01:00:00+00:00',expires_at:'2026-10-26T01:00:00+00:00'}],
  resets:[{reset_id:'4f2b6a52-1c1e-4c55-9d0e-9a2f4a1b7c11',target_principal:'synthetic-op',issued_by:'synthetic-owner',issued_at:'2026-10-25T01:00:00+00:00',expires_at:'2026-10-25T01:30:00+00:00',state:'completed',completed_at:'2026-10-25T01:10:00+00:00',kind:'member'}]};
const owner=me({principal_id:'synthetic-owner',role:'owner',actions:['eios:action:nexloop.workbench.read:1',...Object.values(OPERATION_ACTION).filter(a=>a.includes('workbench_member'))]});
const operator=me({principal_id:'synthetic-op',role:'operator',actions:['eios:action:nexloop.workbench.read:1']});
function render(member:ReturnType<typeof me>,data:unknown){const c=new QueryClient({defaultOptions:{queries:{retry:false,staleTime:Infinity}}});
  c.setQueryData(['workbench','members'],data);c.setQueryData(['workbench','me'],member);
  return renderToStaticMarkup(h(QueryClientProvider,{client:c},h(Members,{})) as ReactNode);}
/** One row's control form, as the page opens it under the table when the member picks it (UX audit B9). */
function panel(member:ReturnType<typeof me>,data:ReturnType<typeof members>,principal:string,kind:string){
  const c=new QueryClient({defaultOptions:{queries:{retry:false,staleTime:Infinity}}});
  const row=data.members.find(m=>m.principal_id===principal)!;
  return renderToStaticMarkup(h(QueryClientProvider,{client:c},h(MemberControlPanel,{member,row,members:data.members,kind})) as ReactNode);}

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
  // R2: no reset code for an account that is not active; the erasure only when every membership of the person is revoked
  expect(memberControls({...m[2],account_status:'disabled'},'synthetic-owner')).toEqual(['change_role','suspend','revoke','promote']);
  expect(memberControls({...m[1],account_status:'invited'},'synthetic-owner')).toEqual(['demote','suspend_owner','revoke_owner']);
  expect(memberControls({...m[4],person_all_revoked:false},'synthetic-owner')).toEqual([]);
});

test('NX-058 UI: the owner sees facts then controls; an operator gets no form', ()=>{
  const html=render(owner,members(raw));
  for(const t of ['成员与账号','未接受的邀请','new-reviewer','最近 90 天的重置码','已完成','邀请成员','负责人（需另一位负责人批准）',
    '<option value="change_role">改角色</option>','<option value="promote">提升为负责人</option>','<option value="demote">降级这位负责人（需谨慎）</option>',
    '<option value="erase">擦除这位成员的个人数据（需谨慎）</option>'])expect(html).toContain(t);
  // B9: no row form is open until one is picked; the only form on the page is the invitation
  expect(html).not.toContain('member-operation');expect(html).not.toMatch(/aria-label="(改角色|停用|撤销成员|提升为负责人|降级这位负责人|擦除这位成员的个人数据)"/);
  expect(html).toContain('aria-label="邀请成员"');
  expect(panel(owner,members(raw),'synthetic-op','promote')).toContain('需要另一位负责人批准');
  const asOperator=render(operator,members({...raw,members:raw.members.slice(2,3)}));
  expect(asOperator).not.toContain('<form');
});

test('NX-058 UI: a reset code or invitation token is shown once, with the link; a sqlstate code is no secret', ()=>{
  expect(oneTimeSecret({code:'ABCD-EFGH-IJKL-MNOP-QRST',replayed:false})).toEqual({kind:'code',value:'ABCD-EFGH-IJKL-MNOP-QRST'});

  expect(oneTimeSecret({status:'execution_refused',code:'22023'})).toBeNull();
  expect(oneTimeSecret({token:'t'.repeat(43)})).toBeNull();  // not a fresh execution (no replayed:false)
  expect(oneTimeSecret({code:'ABCD-EFGH-IJKL-MNOP-QRST',replayed:true})).toBeNull();
  expect(oneTimeSecret({code:'ABCD-EFGH-IJKL-MNOP-QRST',replayed:false,approval:'required'})).toBeNull();
  expect(oneTimeSecret({token:'t'.repeat(43),replayed:false,approval:'consumed'})?.kind).toBe('token');
  const html=renderToStaticMarkup(h(OneTimeSecret,{kind:'token',value:'t'.repeat(43)}));
  expect(html).toContain('只显示这一次');expect(html).toContain('/workbench/invitation#token=');
});

test('NX-058 UI: the approvals page names every owner-only member Action', ()=>{
  for(const op of ['promote_workbench_member','demote_workbench_owner','suspend_workbench_owner','resume_workbench_owner','revoke_workbench_owner',
    'issue_workbench_owner_reset','invite_workbench_owner','erase_workbench_member'])expect(APPROVAL_ACTION_TEXT[op]).toBeTruthy();
});


test('R3: approval-gated controls are disabled with the reason and the operations channel when nobody could approve now', ()=>{
  const now=Date.parse('2026-10-26T00:00:00+00:00');
  const m=members(raw).members;
  expect(approversFor(m,'synthetic-owner','synthetic-op',now)).toBe(1);  // the second owner
  expect(approversFor(m,'synthetic-owner','synthetic-second',now)).toBe(0);  // two owners: the target never approves its own request
  expect(approversFor(m.map(x=>x.principal_id==='synthetic-second'?{...x,vote_eligible_from:'2026-10-27T00:00:00+00:00'}:x),'synthetic-owner','synthetic-op',now)).toBe(0);  // cooldown
  const html=panel(owner,members(raw),'synthetic-second','demote');
  expect(html).toContain('现在没有能批准的负责人');expect(html).toContain('运维通道');
  // the demotion of the only other owner: disabled (no approver), and in any case a new role is required (R1)
  const demote=html.slice(html.indexOf('aria-label="降级这位负责人"'));
  expect(demote.slice(0,demote.indexOf('</form>'))).toMatch(/<button type="submit"[^>]* disabled=""/);
});

test('R1: a demotion cannot be submitted without a new role, even with an approver', ()=>{
  const three=members({...raw,members:[...raw.members,row('synthetic-third','owner','active')]});
  const html=panel(owner,three,'synthetic-second','demote');
  const demote=html.slice(html.indexOf('aria-label="降级这位负责人"'));
  expect(demote.slice(0,demote.indexOf('</form>'))).toMatch(/<button type="submit"[^>]* disabled=""/);
  expect(html).not.toContain('现在没有能批准的负责人（除你和被处置的负责人之外');
});


test('B2: approvers are counted by person, active humans only', ()=>{
  const now=Date.parse('2026-10-26T00:00:00+00:00');
  const three=members({...raw,members:[...raw.members,row('synthetic-third','owner','active')]}).members;
  expect(approversFor(three,'synthetic-owner','synthetic-op',now)).toBe(2);
  const sameAsSelf=three.map(m=>m.principal_id==='synthetic-third'?{...m,person:three[0].person}:m);  // the requester's other principal
  expect(approversFor(sameAsSelf,'synthetic-owner','synthetic-op',now)).toBe(1);
  const disabled=three.map(m=>m.principal_id==='synthetic-third'?{...m,human_active:false}:m);
  expect(approversFor(disabled,'synthetic-owner','synthetic-op',now)).toBe(1);
  expect(()=>members({...raw,members:[{...raw.members[0],person:'synthetic-owner'}]})).toThrow();
});

test('B5: executing an owner reset or an owner invitation from the approvals page shows the secret once; a replay shows the hint', ()=>{
  const code='ABCD-EFGH-IJKL-MNOP-QRST';
  const executed=renderToStaticMarkup(h(WriteResult,{operation:'issue_workbench_owner_reset',
    data:{reset_id:'r',target_principal:'p',replayed:false,approval_id:'a',approval:'consumed',code}}));
  expect(executed.split(code).length-1).toBe(1);expect(executed).toContain('只显示这一次');expect(executed).toContain('已按批准执行');
  const invite=renderToStaticMarkup(h(WriteResult,{operation:'invite_workbench_owner',
    data:{invitation_id:'i',username:'new-owner',role:'owner',replayed:false,approval:'consumed',token:'t'.repeat(43)}}));
  expect(invite.split('t'.repeat(43)).length-1).toBe(2);  // the token and the link carrying it, in one box
  expect(invite).toContain('/workbench/invitation#token=');
  const replay=renderToStaticMarkup(h(WriteResult,{operation:'issue_workbench_owner_reset',
    data:{outcome_id:'o',operation:'issue_workbench_owner_reset',replayed:true,secret:'not_shown_again',next:'revoke_and_request_again'}}));
  expect(replay).toContain('不会再次显示');expect(replay).not.toContain('只显示这一次');
});
