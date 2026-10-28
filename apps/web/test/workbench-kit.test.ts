// Workbench UI kit, batch 1 (docs/design/workbench-ui-spec.md §2–§8 and appendix A): every shared component's contract.
import {createElement as h} from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {expect,test} from 'vitest';
import {readFileSync} from 'node:fs';
import {Card,CardGrid,EmptyState,IdText,KeyValue,MetricCard,PageHeader,ScopeText,SectionCard,shorten} from '../src/workbench/kit';
import {DataTable} from '../src/workbench/DataTable';
import {StatusBadge} from '../src/workbench/ui';
import {DOMAINS,UNKNOWN_LABEL,known,statusLabel,statusTone,unknownCodes} from '../src/workbench/status-labels';
import {groupEvidence} from '../src/workbench/pages/Knowledge';

const r=(e:Parameters<typeof renderToStaticMarkup>[0])=>renderToStaticMarkup(e);
const HEX='548560cd'+'0'.repeat(52)+'a3f0';

test('PageHeader: one h1 with the lead under it, actions in their own group, the toolbar after the head',()=>{
  const html=r(h(PageHeader,{title:'总览',lead:'今天需要关注什么。',actions:h('button',{type:'button'},'暂停'),toolbar:h('span',null,'筛选')}));
  expect(html).toMatch(/^<header class="page-header"><div class="page-header-text"><h1 id="workbench-page-title">总览<\/h1><p class="workbench-lead">今天需要关注什么。<\/p><\/div><div class="workbench-actions page-header-actions" role="group" aria-label="可执行的操作"><button/);
  expect(html.endsWith('<div class="workbench-toolbar"><span>筛选</span></div>')).toBe(true);
  expect(r(h(PageHeader,{title:'A'}))).toBe('<header class="page-header"><div class="page-header-text"><h1 id="workbench-page-title">A</h1></div></header>');
});

test('EmptyState: one title per kind, unavailable and forbidden are alerts and say "not zero", retry only where it helps, details folded',()=>{
  const empty=r(h(EmptyState,{kind:'empty',subject:'承诺'}));
  expect(empty).toContain('role="status"');expect(empty).toContain('data-state="empty"');expect(empty).toContain('暂无承诺');expect(empty).toContain('有新记录时会出现在这里。');
  expect(r(h(EmptyState,{kind:'empty',subject:'Artifact'}))).toContain('暂无 Artifact');  // a space before a Latin entity name
  const off=r(h(EmptyState,{kind:'unavailable',subject:'Embedding 预算',onRetry:()=>{},details:'HTTP 503'}));
  expect(off).toContain('role="alert"');expect(off).toContain('Embedding 预算暂不可用');expect(off).toContain('未加载，不代表数量为零。');
  expect(off).toContain('>重试</button>');expect(off).toContain('<summary>技术细节</summary>HTTP 503');
  const no=r(h(EmptyState,{kind:'forbidden',subject:'审计',onRetry:()=>{}}));
  expect(no).toContain('role="alert"');expect(no).toContain('无权查看审计');expect(no).not.toContain('重试');
  const loading=r(h(EmptyState,{kind:'loading',subject:'承诺'}));
  expect(loading).toContain('aria-busy="true"');expect(loading).toContain('正在加载承诺');expect(loading.match(/skeleton-line/g)?.length).toBe(3);
  expect(r(h(EmptyState,{kind:'filtered',subject:'客户'}))).toContain('没有符合筛选条件的客户');
  expect(r(h(EmptyState,{kind:'empty',subject:'x',compact:true}))).toContain('class="state state-empty state-compact"');
});

test('Card / SectionCard / CardGrid / MetricCard: equal-height grid, footer at the bottom, a missing block keeps its place and says so',()=>{
  const card=r(h(Card,{title:'承诺',badge:h(StatusBadge,{text:'正常',tone:'ok'}),footer:h('button',{type:'button'},'查看')},h('p',null,'未结束 2 条')));
  expect(card).toMatch(/^<article class="card" aria-label="承诺"><div class="card-head"><h2>承诺<\/h2><span class="status-badge tone-ok"/);
  expect(card).toContain('<div class="card-body"><p>未结束 2 条</p></div><div class="card-foot"><button');
  const missing=r(h(SectionCard<string>,{title:'队列积压',section:{status:'unavailable',data:null},footer:h('a',null,'x'),children:()=>'never'}));
  expect(missing).toContain('class="card card-unavailable"');expect(missing).toContain('队列积压暂不可用');expect(missing).not.toContain('never');expect(missing).not.toContain('card-foot');
  expect(r(h(SectionCard<string>,{title:'T',section:{status:'forbidden',data:null},children:()=>'x'}))).toContain('无权查看 T');
  expect(r(h(SectionCard<number>,{title:'T',section:{status:'ok',data:3},children:n=>`共 ${n}`}))).toContain('<div class="card-body">共 3</div>');
  expect(r(h(CardGrid,{label:'总览'},'x'))).toBe('<div class="card-grid" aria-label="总览">x</div>');
  expect(r(h(CardGrid,{min:'24rem'},'x'))).toContain('class="card-grid card-grid-24"');
  const link=r(h(MetricCard,{label:'Object Type',value:12,hint:'类型定义',onOpen:()=>{}}));
  expect(link).toMatch(/^<a class="metric-card metric-link" href="#" aria-label="Object Type 12，查看"><span class="metric-label">Object Type<\/span><strong class="metric-value">12<\/strong><span class="metric-hint">类型定义<\/span><\/a>$/);
  expect(r(h(MetricCard,{label:'连接',value:'11 个'}))).toMatch(/^<div class="metric-card">/);  // not clickable: not a button or link
});

test('KeyValue: a description list of key and value rows with an optional hint',()=>{
  const html=r(h(KeyValue,{label:'备份',items:[{key:'最近一次校验',value:'30 分钟'},{key:'WAL 归档',value:'正常',hint:'最近 1 小时'}]}));
  expect(html).toBe('<dl class="key-value" aria-label="备份"><div class="kv-row"><dt>最近一次校验</dt><dd>30 分钟</dd></div><div class="kv-row"><dt>WAL 归档</dt><dd>正常<span class="kv-hint">最近 1 小时</span></dd></div></dl>');
});

test('IdText: head…tail in monospace, the full value on hover and focus, a copy button; a name replaces the hash; short ids unchanged',()=>{
  expect(shorten(HEX)).toBe('548560cd…a3f0');expect(shorten('abc')).toBe('abc');expect(shorten(HEX,4,2)).toBe('5485…f0');
  const html=r(h(IdText,{value:HEX,kind:'客户'}));
  expect(html).toContain('<span class="id-kind">客户</span>');expect(html).toContain(`<code class="id-code" title="客户 ${HEX}" aria-label="客户 ${HEX}">548560cd…a3f0</code>`);  // C3: no tab stop
  expect(html).toContain('aria-label="复制客户 ID"');expect(html).toContain(`data-id="${HEX}"`);expect(html.split(HEX).length-1).toBe(4);  // data-id, title, aria-label, the copy button's title: never as text
  const named=r(h(IdText,{value:HEX,name:'王女士',kind:'客户'}));
  expect(named).toContain('<span class="id-name">王女士</span>');expect(named).toContain('class="id-code id-secondary"');
  expect(r(h(IdText,{value:HEX,copy:false}))).not.toContain('id-copy');
  expect(r(h(IdText,{value:HEX,expandable:true}))).toContain('aria-expanded="false"');
});

test('ScopeText: the scope kind in words and the Consumer as IdText - never consumer:<64 hex>; the tenant-wide scope has no reference',()=>{
  const consumer=r(h(ScopeText,{kind:'consumer',value:'consumer:'+HEX}));
  expect(consumer).toContain('客户');expect(consumer).toContain('548560cd…a3f0');expect(consumer).not.toContain('consumer:');
  expect(consumer).not.toMatch(new RegExp('>'+HEX+'<'));
  expect(r(h(ScopeText,{kind:'tenant',value:'*'}))).toBe('<span class="scope-text">整个租户</span>');
  expect(r(h(ScopeText,{kind:'action_type',value:'eios:action:nexloop.enterprise.deliver:1'}))).toContain('Action 类型 <code class="id-code">nexloop.enterprise.deliver:1</code>');
});

test('status labels: every domain is a glossary map; a known code shows its label and tone, an unlisted one 未知状态 with the code in the title',()=>{
  const labels=readFileSync(new URL('../src/workbench/labels.ts',import.meta.url),'utf8');
  for(const map of Object.values(DOMAINS))expect(Object.keys(map).length).toBeGreaterThan(0);
  expect(labels).toContain('export const COMMITMENT_EXCEPTION');
  expect(statusLabel('commitment_exception','no_active_plan')).toBe('没有进行中的计划');
  expect(statusLabel('commitment_exception','made_under_contact_restriction')).toBe('受限期间作出');
  expect(statusLabel('commitment_exception','source_deleted')).toBe('来源已删除');
  expect(statusLabel('job_status','dead_lettered')).toBe('死信');expect(statusTone('job_status','dead_lettered')).toBe('fail');
  expect(statusLabel('claim_resolution','needs_resolution')).toBe('待澄清');
  expect(statusTone('intent_state','unknown')).toBe('limited');expect(statusTone('intent_state','fulfilled')).toBe('ok');expect(statusTone('scope_kind','tenant')).toBe('neutral');
  expect(known('intent_state','toString')).toBe(false);  // own keys only
  expect(statusLabel('intent_state','teleported')).toBe(UNKNOWN_LABEL);expect(unknownCodes()).toContain('intent_state:teleported');
  const badge=r(h(StatusBadge,{domain:'commitment_exception',code:'no_active_plan'}));
  expect(badge).toBe('<span class="status-badge tone-limited" title="no_active_plan" data-code="no_active_plan"><span aria-hidden="true" class="status-mark">!</span>没有进行中的计划</span>');
  const unknown=r(h(StatusBadge,{domain:'plan_status',code:'warp'}));
  expect(unknown).toContain('>未知状态</span>');expect(unknown).toContain('title="warp"');expect(unknown).not.toContain('>warp<');
  expect(r(h(StatusBadge,{text:'已记录',tone:'ok'}))).toContain('tone-ok');  // the free-text form stays for wording that is not a code
});

test('DataTable: its own horizontal scroller, the header sticks past 20 rows, numbers right-aligned, the empty state instead of an empty table',()=>{
  const rows=Array.from({length:3},(_,i)=>({id:'r'+i,n:i}));
  const cols=[{label:'名称',main:true,cell:(x:{id:string})=>x.id},{label:'次数',num:true,cell:(x:{n:number})=>x.n}];
  const html=r(h(DataTable<{id:string;n:number}>,{label:'记录',rows,rowKey:x=>x.id,columns:cols}));
  expect(html).toMatch(/^<div class="table-scroll"><table class="data-table" aria-label="记录">/);  // C3: a tab stop only once it overflows (measured in the browser)
  expect(html).toContain('<th scope="col" class="cell-num">次数</th>');expect(html).toContain('<td data-label="次数" class="cell-num">2</td>');
  const long=r(h(DataTable<{id:string;n:number}>,{rows:Array.from({length:21},(_,i)=>({id:'r'+i,n:i})),rowKey:x=>x.id,columns:cols}));
  expect(long).toContain('class="data-table table-sticky"');
  expect(r(h(DataTable<{id:string;n:number}>,{label:'记录',rows:[],rowKey:x=>x.id,columns:cols,empty:{subject:'回填记录'}}))).toContain('暂无回填记录');
  expect(r(h(DataTable<{id:string;n:number}>,{label:'记录',rows:[],rowKey:x=>x.id,columns:cols,empty:{subject:'客户',filtered:true}}))).toContain('没有符合筛选条件的客户');
  expect(r(h(DataTable<{id:string;n:number}>,{rows:[],rowKey:x=>x.id,columns:cols}))).toContain('<tbody></tbody>');  // without `empty` the old shape stays
});

test('review evidence: Claims of one source message are grouped, the message text appears once; Claims without a message stay apart',()=>{
  const body={status:'ok' as const,body:'下周三之前寄出'};
  const ev=[{claim_id:'c1',predicate:'delivery_promise',source_message_id:'m1',content:body},{claim_id:'c2',predicate:'due_date',source_message_id:'m1',content:body},
    {claim_id:'c3',predicate:'tone',source_message_id:'m2',content:{status:'restricted' as const}},{claim_id:'c4',predicate:null,source_message_id:null,content:{status:'restricted' as const}}];
  const g=groupEvidence(ev);
  expect(g.map(x=>[x.message,x.claims.map(c=>c.claim_id)])).toEqual([['m1',['c1','c2']],['m2',['c3']],[null,['c4']]]);
  expect(g[0].content).toEqual(body);expect(g[2].content).toBeNull();
});

test('pages: Goals shows control events as label + IdText (never the 64-hex scope_ref as text); the evidence page shows a message once',async()=>{
  const {QueryClient,QueryClientProvider}=await import('@tanstack/react-query');
  const {goals}=await import('../src/workbench/api');
  const {Goals}=await import('../src/workbench/pages/Goals');
  const {EvidenceByMessage}=await import('../src/workbench/pages/Knowledge');
  const c=new QueryClient({defaultOptions:{queries:{retry:false,staleTime:Infinity}}});
  c.setQueryData(['workbench','goals'],goals({goals:[],budgets:[],control:{revision:3,
    scopes:[{scope_kind:'consumer',scope_ref:'consumer:'+HEX,paused:true,revision:2,reason:'客户要求'}],
    events:[{revision:3,event_kind:'pause',scope_kind:'consumer',scope_ref:'consumer:'+HEX,principal_id:'p',recorded_at:'2026-10-20T01:00:00+00:00'},
      {revision:2,event_kind:'teleport',scope_kind:'tenant',scope_ref:'*',principal_id:'p',recorded_at:'2026-10-20T00:00:00+00:00'}]}}));
  const html=renderToStaticMarkup(h(QueryClientProvider,{client:c},h(Goals,{})));
  expect(html).toContain('aria-label="控制事件"');expect(html).toContain('>暂停</span>');expect(html).toContain('548560cd…a3f0');expect(html).toContain('整个租户');
  expect(html).toContain('>未知状态</span>');expect(html).toContain('title="teleport"');
  expect(html).not.toContain('>consumer:');expect(html).not.toMatch(new RegExp('>[^<]*'+HEX));  // the full hash never as visible text
  expect(html).toContain('控制修订号');expect(html).not.toContain('revision');
  const body={status:'ok' as const,body:'下周三之前寄出'};
  const ev=renderToStaticMarkup(h(EvidenceByMessage,{evidence:[{claim_id:'c1',predicate:'delivery_promise',source_message_id:'m'.repeat(20),content:body},
    {claim_id:'c2',predicate:'due_date',source_message_id:'m'.repeat(20),content:body},{claim_id:'c3',predicate:'item',source_message_id:'m'.repeat(20),content:body}]}));
  expect(ev.split('下周三之前寄出').length-1).toBe(1);expect(ev).toContain('3 条 Claim');expect(ev).toContain('aria-label="证据：1 条消息，3 条 Claim"');
  expect(ev).toContain('delivery_promise');expect(ev).toContain('due_date');
});

test('C2: a compact empty state is one line; a compact unavailable one keeps saying the number is not zero; a full one keeps its next step',()=>{
  expect(r(h(EmptyState,{kind:'empty',subject:'承诺',compact:true}))).not.toContain('state-next');
  expect(r(h(EmptyState,{kind:'empty',subject:'承诺',compact:true,next:'自定义'}))).toContain('自定义');
  expect(r(h(EmptyState,{kind:'empty',subject:'承诺'}))).toContain('有新记录时会出现在这里。');
  expect(r(h(EmptyState,{kind:'unavailable',subject:'承诺',compact:true}))).toContain('未加载，不代表数量为零。');
});
