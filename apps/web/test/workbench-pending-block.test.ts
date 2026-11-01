// @vitest-environment jsdom
// K4e: the home 待我处理 block renders the server's workbench-pending v1 as it is (categories filtered by the server per member),
// count, longest wait and a link per category; all_clear is one line. Questions stay hidden until context-pack v7, even when returned.
import {createElement as h,act} from 'react';
import {createRoot,type Root} from 'react-dom/client';
import {readFileSync} from 'node:fs';
import {resolve} from 'node:path';
import {afterEach,beforeEach,expect,test} from 'vitest';
import {pending,PENDING_CATEGORIES,type Pending} from '../src/workbench/api';
import {PendingBody,QUESTIONS_SHOWN,pendingTarget} from '../src/workbench/pages/Pending';
import {routePath,type Route} from '../src/workbench/route';

(globalThis as {IS_REACT_ACT_ENVIRONMENT?:boolean}).IS_REACT_ACT_ENVIRONMENT=true;
const example=JSON.parse(readFileSync(resolve(__dirname,'../../../packages/contracts/examples/workbench-pending.valid.json'),'utf8'));
const CANDIDATE='0b6c1e2a-3d4f-4a5b-8c6d-7e8f9a0b1c2d';
const busy=(over:Record<string,number>)=>({...example,all_clear:false,categories:example.categories.map((c:{category:string})=>over[c.category]
  ?{category:c.category,count:over[c.category],oldest_waiting_seconds:5400,oldest_ref:c.category==='candidate_review'?'candidate:'+CANDIDATE:c.category+':synthetic-1'}
  :c.category==='question'?c:{...c})});
let root:Root;let box:HTMLDivElement;
beforeEach(()=>{box=document.createElement('div');document.body.append(box);root=createRoot(box);});
afterEach(()=>{act(()=>root.unmount());box.remove();});
const mount=(data:Pending,go:(r:Route)=>void=()=>{})=>act(()=>root.render(h(PendingBody,{data,go,href:routePath})));
const rows=()=>[...box.querySelectorAll<HTMLLIElement>('.pending-list li')];

test('the contract example parses; the parser holds the six categories in order and refuses what the contract does not allow',()=>{
  const p=pending(example);expect(p.categories.map(c=>c.category)).toEqual([...PENDING_CATEGORIES]);expect(p.questions).toHaveLength(1);
  expect(()=>pending({...example,extra:1})).toThrow();
  expect(()=>pending({...example,categories:example.categories.slice(1)})).toThrow();
  expect(()=>pending({...example,categories:[...example.categories].reverse()})).toThrow();
  expect(()=>pending({...example,all_clear:true})).toThrow();  // all_clear only when every count is zero
  expect(()=>pending({...example,categories:example.categories.map((c:object,i:number)=>i===1?{...c,category:'alert_ack'}:c)})).toThrow();
});

test('questions are not shown before context-pack v7, even when the server returns them',()=>{
  expect(QUESTIONS_SHOWN).toBe(false);
  mount(pending(example));  // only the question category has work
  expect(box.textContent).toContain('没有待处理事项');expect(box.textContent).not.toContain('问题');
  expect(box.textContent).not.toContain(example.questions[0].text);expect(box.querySelector('[data-category=question]')).toBeNull();
  expect(box.querySelectorAll('button,form,input,select,textarea').length).toBe(0);
});

test('each category with work shows its count, longest wait and where it is handled; nothing writes',()=>{
  let went:Route|null=null;
  mount(pending(busy({approval:4,remediation:2,reply_escalation:1,takeover_expiry:2,candidate_review:3})),r=>{went=r;});
  expect(rows().map(r=>r.dataset.category)).toEqual(['approval','remediation','reply_escalation','takeover_expiry','candidate_review']);
  const approval=rows()[0];expect(approval.textContent).toContain('待批审批');expect(approval.querySelector('.badge-count')!.textContent).toBe('4');
  expect(approval.textContent).toContain('最久等待 90 分钟');
  const link=approval.querySelector('a')!;expect(link.getAttribute('href')).toBe('/workbench/approvals');expect(link.getAttribute('aria-label')).toBe('去审批：待批审批（4）');
  act(()=>link.dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true})));expect(went).toEqual({page:'approvals'});
  expect(rows()[4].querySelector('a')!.getAttribute('href')).toBe('/workbench/knowledge/'+CANDIDATE);
  // L6 R1: takeovers are listed under 人工接管 on the 健康 tab.
  const takeover=rows()[3].querySelector('a')!;expect(takeover.getAttribute('href')).toBe('/workbench/home?tab=health');
  act(()=>takeover.dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true})));expect(went).toEqual({page:'home',query:{tab:'health'}});
  expect(box.querySelectorAll('button,form,input,select,textarea').length).toBe(0);
  expect(box.textContent).not.toContain('告警');
});

test('all_clear is one line with the last health check; categories with zero are not listed',()=>{
  const clear={...example,all_clear:true,questions:[],categories:example.categories.map((c:object)=>({...c,count:0,oldest_waiting_seconds:null,oldest_ref:null}))};
  mount(pending(clear));
  expect(rows()).toHaveLength(0);expect(box.textContent).toContain('没有待处理事项');expect(box.textContent).toContain('最近一次健康检查');
  act(()=>root.unmount());root=createRoot(box);mount(pending({...clear,last_health_check_at:null}));
  expect(box.textContent).toContain('还没有健康检查记录');
});

test('the block shows what the server returned for the member, with no role check of its own',()=>{
  // A reviewer's answer: only candidate definitions (the server filtered); the block renders exactly that.
  mount(pending(busy({candidate_review:1})));
  expect(rows().map(r=>r.dataset.category)).toEqual(['candidate_review']);
  expect(pendingTarget({category:'candidate_review',count:1,oldest_waiting_seconds:1,oldest_ref:'candidate:not-a-uuid'})!.route).toEqual({page:'knowledge'});
});
