// @vitest-environment jsdom
// Owner walkthrough R56: nothing on a workbench page touches its neighbour. Stacked cards keep a gap (W4 experiment detail), the
// review decision puts each field on its own row and the decision buttons in their own spaced row (知识工作台 审核证据), the evidence
// messages keep one gap, and the glossary correction sits beside the search box and in the type panel (W5), not below the table.
import {createElement as h,act,type ReactNode} from 'react';
import {createRoot,type Root} from 'react-dom/client';
import {readFileSync} from 'node:fs';
import {resolve} from 'node:path';
import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
import {afterEach,beforeEach,expect,test} from 'vitest';
import {experiments,me,OPERATION_ACTION,worldModel} from '../src/workbench/api';
import {candidateDetail} from '../src/review-api';
import {Detail} from '../src/workbench/pages/Experiments';
import {EvidenceByMessage,ReviewDecisionsView} from '../src/workbench/pages/Knowledge';
import {WorldModel} from '../src/workbench/pages/WorldModel';

(globalThis as {IS_REACT_ACT_ENVIRONMENT?:boolean}).IS_REACT_ACT_ENVIRONMENT=true;
const css=readFileSync(resolve(__dirname,'../src/styles.css'),'utf8');
const example=JSON.parse(readFileSync(resolve(__dirname,'../../../packages/contracts/examples/workbench-world-model-v2.valid.json'),'utf8'));
let root:Root;let box:HTMLDivElement;let style:HTMLStyleElement;
beforeEach(()=>{style=document.createElement('style');style.textContent=css;document.head.append(style);
  box=document.createElement('div');box.className='workbench';document.body.append(box);root=createRoot(box);});
afterEach(()=>{act(()=>root.unmount());box.remove();style.remove();});
const mount=(node:ReactNode,setup?:(c:QueryClient)=>void)=>{const c=new QueryClient({defaultOptions:{queries:{retry:false,staleTime:Infinity}}});setup?.(c);
  act(()=>root.render(h(QueryClientProvider,{client:c},h('div',{className:'workbench-page'},node))));};
const st=(e:Element)=>getComputedStyle(e);
// jsdom keeps the spacing tokens unresolved: --space-1 4px, -2 8px, -3 12px, -4 16px, -5 24px (styles.css :root).
const gap=(e:Element)=>st(e).getPropertyValue('gap');

test('W4: the experiment detail cards (登记, 护栏, 预算与分配, 最新报告) keep a 16 px gap; the status badge keeps its distance from the title',()=>{
  const x=experiments({experiments:[{experiment_id:'followup.timing',world:'sim:00000000-0000-4000-8000-000000000001',world_kind:'simulation',stage:'screening',hypothesis:'回访延后',
    candidate:{strategy_id:'followup.later',version:1},control:{strategy_id:'followup.default',version:1},primary_metric:{key:'reply_within_24h',direction:'increase'},
    guardrails:{},sample_size:{candidate:20,control:20},budget:{model_cny:1,incentive_cny:0},ends_at:null,registration_digest:'b'.repeat(64),
    registered_at:'2026-10-28T11:00:00+00:00',status:'registered',started_at:null,stopped_at:null,stop_reason:null,stopped_by_guard:false,
    spent:{model_cny:0,incentive_cny:0},allocation:{candidate:0,control:0,excluded:0},report:null,budget_drill:false,notice:null,twin:null}]})[0];
  mount(h(Detail,{x}));
  const detail=box.querySelector('.experiment-detail')!;expect(st(detail).display).toBe('flex');expect(st(detail).flexDirection).toBe('column');expect(gap(detail)).toBe('var(--space-4)');
  expect(detail.querySelectorAll(':scope>.card').length).toBeGreaterThanOrEqual(3);
  const head=detail.querySelector('.card-head')!;expect(gap(head)).toBe('var(--space-3)');expect(st(head.lastElementChild!).flexShrink).toBe('0');
  // Anywhere else, two cards stacked outside a grid keep the same gap.
  expect(css).toContain('.workbench :not(.card-grid)>.card+.card');
});

test('审核决定: the rationale and the merge target each on their own row, the three decisions in one spaced row of their own',()=>{
  const id='0f0e0d0c-0b0a-4908-8706-050403020100';
  const scores={lexical_similarity:0.2,core_term_containment:0,vector_cluster:0.26,rule_whitelist:0,weighted_total:0.092,best_match_ref:'eios:object_type:Consumer',threshold:0.375,config_version:'nx045-glue-v1-test64'};
  const data=candidateDetail({candidate_id:id,kind:'object_type',status:'pending_review',revision:2,created_at:'2026-10-09T01:00:00Z',dependent_claim_count:47,config_version:'nx045-glue-v1-test64',
    merge_scores:scores,candidate:{proposed:{display_name:'企业',name:'Enterprise'},recall:[{ref:'eios:object_type:Consumer',method:'fts',score:0.21}]},
    decisions:{enabled:true,reason:'governed_human_review_action',actions:['approve','merge_into','reject']},cooldown:null,evidence:[],similar:[],derivation_impact:null});
  mount(h(ReviewDecisionsView,{state:{status:'ok',data}}));
  const bar=box.querySelector('.decisions')!;expect(st(bar).display).toBe('flex');expect(st(bar).flexDirection).toBe('column');expect(gap(bar)).toBe('var(--space-3)');
  const fields=[...bar.querySelectorAll(':scope>.decision-field')];expect(fields.map(f=>f.querySelector('textarea,select')?.tagName)).toEqual(['TEXTAREA','SELECT']);
  const row=bar.querySelector(':scope>.decision-actions')!;expect([...row.children].map(b=>b.textContent)).toEqual(['批准发布','并入已有定义','拒绝']);
  expect(st(row).display).toBe('flex');expect(gap(row)).toBe('var(--space-2)');expect(st(row).flexWrap).toBe('wrap');
  expect(row.querySelector('select')).toBeNull();  // the select is never squeezed into the button row
  const section=box.querySelector('section[aria-labelledby="review-decisions-heading"]')!;expect(st(section).marginTop).toBe('var(--space-5)');
});

test('evidence messages: one 12 px gap between message cards, none of them touching',()=>{
  const claim=(n:number,msg:string)=>({claim_id:`00000000-0000-4000-8000-00000000000${n}`,source_message_id:msg,predicate:'人数',content:{status:'ok',body:'你好'},created_at:'2026-10-09T01:00:00Z'});
  mount(h(EvidenceByMessage,{evidence:[claim(1,'10000000-0000-4000-8000-000000000001'),claim(2,'10000000-0000-4000-8000-000000000002')] as never}));
  const list=box.querySelector('.evidence-list')!;expect(st(list).display).toBe('flex');expect(gap(list)).toBe('var(--space-3)');expect(st(list).getPropertyValue('list-style')).toBe('none');
  expect(list.querySelectorAll(':scope>.evidence-message').length).toBe(2);
});

test('R57 W5: the world model pages show glosses only - no correction control in a list, the summary or the type panel, even for the owner',()=>{
  const owner=me({principal_id:'synthetic-owner',role:'owner',actions:[OPERATION_ACTION.world_glossary_set]});
  const seed=(c:QueryClient)=>{c.setQueryData(['workbench','world-model'],worldModel(example));c.setQueryData(['workbench','me'],owner);};
  for(const s of [undefined,'object_types','properties','links','actions','functions']){act(()=>root.unmount());root=createRoot(box);mount(h(WorldModel,{section:s as never,go:()=>{}}),seed);
    expect(box.textContent).not.toContain('中文译名');expect(box.querySelector('.gloss-edit-open,.gloss-edit')).toBeNull();}
  act(()=>root.unmount());root=createRoot(box);mount(h(WorldModel,{section:'object_types' as never,query:{view:'graph',focus:'Consumer'},go:()=>{}}),seed);
  expect(box.querySelector('.world-panel')).not.toBeNull();expect(box.querySelector('.world-panel')!.textContent).not.toContain('中文译名');
});

test('R57: an inline badge keeps 8 px from the button or count beside it (演进停止开关, 总览 承诺)',()=>{
  expect(css).toContain('.workbench .status-badge+:not(.kv-hint):not(br){margin-left:var(--space-2)}');
  expect(css).toContain('.workbench :is(button,a,.badge-count,code,time)+.status-badge{margin-left:var(--space-2)}');
  const li=document.createElement('li');li.innerHTML='<span class="status-badge">运行中</span><button>停止…</button><span class="status-badge">没有期限</span><span class="badge-count">152</span>';
  box.append(li);
  const [b1,count]=[li.querySelector('button')!,li.querySelector('.badge-count')!];
  expect(st(b1).marginLeft).toBe('var(--space-2)');expect(st(count).marginLeft).toBe('var(--space-2)');
  expect(st(li.querySelectorAll('.status-badge')[1]).marginLeft).toBe('var(--space-2)');li.remove();
});
