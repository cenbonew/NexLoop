// @vitest-environment jsdom
// UX audit B9: a member row offers its operations in one picker; the chosen operation opens as one form under the table, with focus
// on its heading; picking another (or another row's) replaces it; 关闭 closes it. The invitation is one form whose role decides
// whether it is the approval-gated owner invitation.
import {createElement as h} from 'react';
import {act} from 'react';
import {createRoot,type Root} from 'react-dom/client';
import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
import {afterEach,beforeEach,expect,test} from 'vitest';
import {me,members,OPERATION_ACTION} from '../src/workbench/api';
import {MembersView} from '../src/workbench/pages/Members';

(globalThis as {IS_REACT_ACT_ENVIRONMENT?:boolean}).IS_REACT_ACT_ENVIRONMENT=true;
const row=(principal:string,role:string|null,status:string)=>({principal_id:principal,label:'p-0123abcd',username:principal.replace('synthetic-',''),role,member_status:status,
  account_status:'active',vote_eligible_from:role==='owner'?'2026-10-20T00:00:00+00:00':null,erased:false,person_all_revoked:false,
  person:'s-'+Array.from(principal).reduce((a,c)=>(a*31+c.charCodeAt(0))>>>0,7).toString(16).padStart(8,'0').slice(-8),human_active:true});
const data=members({members:[row('synthetic-owner','owner','active'),row('synthetic-second','owner','active'),row('synthetic-op','operator','active'),row('synthetic-rev','reviewer','active')],
  invitations:[],resets:[]});
const owner=me({principal_id:'synthetic-owner',role:'owner',actions:['eios:action:nexloop.workbench.read:1',...Object.values(OPERATION_ACTION).filter(a=>a.includes('workbench_member'))]});

let root:Root;let box:HTMLDivElement;
beforeEach(()=>{box=document.createElement('div');document.body.append(box);root=createRoot(box);
  act(()=>root.render(h(QueryClientProvider,{client:new QueryClient()},h(MembersView,{data,member:owner}))));});
afterEach(()=>{act(()=>root.unmount());box.remove();});
const pick=(label:string,value:string)=>act(()=>{const s=box.querySelector<HTMLSelectElement>(`select[aria-label="${label}"]`)!;
  s.value=value;s.dispatchEvent(new Event('change',{bubbles:true}));});
const rowForms=()=>[...box.querySelectorAll('.member-operation form')].map(f=>f.getAttribute('aria-label'));

test('one operation at a time, opened under the table with focus on its heading; another pick replaces it; 关闭 closes it',()=>{
  expect(box.querySelectorAll('.member-operation')).toHaveLength(0);
  expect(box.querySelector('select[aria-label="对 owner 的操作"]')).toBeNull();  // never the member's own row
  pick('对 op 的操作','revoke');
  expect(rowForms()).toEqual(['撤销成员']);
  expect(document.activeElement?.textContent).toBe('撤销成员：op p-0123abcd');
  expect(box.querySelector('.member-operation button[type=submit]')?.className).toBe('btn-danger');
  pick('对 rev 的操作','change_role');
  expect(rowForms()).toEqual(['改角色']);
  expect(box.querySelector<HTMLSelectElement>('select[aria-label="对 op 的操作"]')!.value).toBe('');
  act(()=>[...box.querySelectorAll('button')].find(b=>b.textContent==='关闭')!.click());
  expect(box.querySelectorAll('.member-operation')).toHaveLength(0);
});

test('one invitation form: choosing 负责人 turns it into the approval-gated owner invitation',()=>{
  const invite=()=>[...box.querySelectorAll('form')].map(f=>f.getAttribute('aria-label')).filter(l=>l?.startsWith('邀请'));
  expect(invite()).toEqual(['邀请成员']);
  const radio=[...box.querySelectorAll<HTMLInputElement>('input[name="invite-role"]')];
  expect(radio.map(r=>r.parentElement?.textContent)).toEqual(['运营','审核人','负责人（需另一位负责人批准）']);
  act(()=>radio[2].click());
  expect(invite()).toEqual(['邀请负责人']);
  expect(box.textContent).toContain('需要另一位负责人批准');
});
