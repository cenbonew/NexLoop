// @vitest-environment jsdom
// K5 (docs/design/k5-consumer-evidence-views.md v2) on the L2 contract examples: the consumer's evidence graph, a message's trace and the
// conversation metrics. Fixed orders (5 groups, 9 steps), nothing inferred about what is not listed, invisible said as a permission,
// erased said for the owner only, codes shown as labels or as they are, amounts kept as the server's decimal strings.
import {createElement as h,act} from 'react';
import {createRoot,type Root} from 'react-dom/client';
import {readFileSync} from 'node:fs';
import {resolve} from 'node:path';
import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
import {afterEach,beforeEach,expect,test,vi} from 'vitest';
import {EVIDENCE_KINDS,TRACE_STEPS,WorkbenchError,conversationMetrics,evidenceGraph,messageTrace} from '../src/workbench/api';
import {EvidenceSection,OPERATION_TEXT,groupNote,label,radialLayout} from '../src/workbench/pages/ConsumerEvidence';
import {ConversationMetricsCard,MessageTrace,STEP_TEXT} from '../src/workbench/pages/ConversationEvidence';

(globalThis as {IS_REACT_ACT_ENVIRONMENT?:boolean}).IS_REACT_ACT_ENVIRONMENT=true;
const json=(name:string)=>JSON.parse(readFileSync(resolve(__dirname,`../../../packages/contracts/examples/${name}.valid.json`),'utf8'));
const graphEx=json('workbench-consumer-evidence'),traceEx=json('workbench-message-trace'),metricsEx=json('workbench-conversation-metrics');
const cid=graphEx.consumer_id as string,C='c'.repeat(64),E='e'.repeat(64);
let root:Root;let box:HTMLDivElement;let urls:string[];
beforeEach(()=>{box=document.createElement('div');document.body.append(box);root=createRoot(box);urls=[];});
afterEach(()=>{act(()=>root.unmount());box.remove();vi.unstubAllGlobals();});
function stub(routes:(url:string)=>Response){vi.stubGlobal('fetch',vi.fn(async(url:string)=>{urls.push(url);return routes(url);}));}
async function mount(node:ReturnType<typeof h>){
  const c=new QueryClient({defaultOptions:{queries:{retry:false}}});
  await act(async()=>{root.render(h(QueryClientProvider,{client:c},node));});
  for(let i=0;i<3;i++)await act(async()=>{await new Promise(r=>setTimeout(r,0));});
}
const click=async(el:Element|null)=>{await act(async()=>{(el as HTMLElement).dispatchEvent(new MouseEvent('click',{bubbles:true}));});for(let i=0;i<3;i++)await act(async()=>{await new Promise(r=>setTimeout(r,0));});};

test('parsers: fixed group and step orders, world real only, amounts kept as strings',()=>{
  expect(evidenceGraph(graphEx).groups.map(g=>g.kind)).toEqual([...EVIDENCE_KINDS]);
  expect(EVIDENCE_KINDS).toEqual(['claim','commitment','action','outreach','candidate']);
  const swapped=structuredClone(graphEx);[swapped.groups[0],swapped.groups[1]]=[swapped.groups[1],swapped.groups[0]];
  expect(()=>evidenceGraph(swapped)).toThrow(WorkbenchError);expect(()=>evidenceGraph({...graphEx,world:'sim'})).toThrow(WorkbenchError);
  expect(messageTrace(traceEx).steps.map(s=>s.step)).toEqual([...TRACE_STEPS]);
  expect(TRACE_STEPS).toEqual(['message','relay','context','run','action','receipt','extraction','matching','ontology_update']);
  const t=structuredClone(traceEx);t.steps.reverse();expect(()=>messageTrace(t)).toThrow(WorkbenchError);
  const owner=conversationMetrics({...metricsEx,model_cost:{totals:[{currency:'CNY',amount:'0.1000000000000000055',amount_unit:'major',entries:2}],runs:2,unpriced_entries:0},withheld:[]});
  expect(owner.model_cost!.totals[0].amount).toBe('0.1000000000000000055');
});

test('group notes: invisible as a permission, owner_only as such, erased only when a number',()=>{
  const g=evidenceGraph(graphEx).groups;
  expect(groupNote(g[0])).toContain('不可见 2 个：涉及你没有读取权限的属性');expect(groupNote(g[0])).toContain('不是数据丢失');
  expect(groupNote(g[3])).toBe('仅负责人可见 2 个');expect(groupNote(g[1])).toBeNull();
  expect(groupNote({...g[0],erased:null})).not.toContain('已删除');expect(groupNote({...g[0],erased:3})).toContain('已删除 3 个');
  expect(label(OPERATION_TEXT,'commitment.fulfil')).toBe('履行承诺');expect(label(OPERATION_TEXT,'x.unknown_code')).toBe('x.unknown_code');
});

test('the graph draws listed nodes and only edges between them; the panel shows the chain from returned fields',async()=>{
  const lay=radialLayout(evidenceGraph(graphEx));expect(lay.nodes).toHaveLength(4);expect(lay.edges).toHaveLength(3);
  stub(()=>Response.json(graphEx));await mount(h(EvidenceSection,{consumerId:cid}));
  expect([...box.querySelectorAll('.evidence-groups li')].map(li=>li.getAttribute('data-kind'))).toEqual([...EVIDENCE_KINDS]);
  expect(box.textContent).toContain('仅负责人可见 2 个');expect(box.textContent).toContain('还没有候选');expect(box.textContent).not.toContain('已删除');
  await click(box.querySelector(`[data-node="claim:${C}"]`));
  const panel=box.querySelector('.evidence-panel')!;
  expect(panel.textContent).toContain('原文第 3 轮 · 客户说的');expect(panel.textContent).toContain('原文只对审核人');
  await click(box.querySelector(`[data-node="claim:${'d'.repeat(64)}"]`));
  expect(box.querySelector('.evidence-panel')!.textContent).toContain('已更正');expect(box.querySelector('.evidence-panel')!.textContent).not.toContain('prefers');
  await click(box.querySelector(`[data-node="commitment:${E}"]`));
  expect(box.querySelector('.evidence-panel')!.textContent).toContain('由 Claim');
  await click(box.querySelector(`[data-node="action:intent-1"]`));expect(box.querySelector('.evidence-panel')!.textContent).toContain('履行承诺');
  await click([...box.querySelectorAll('.segmented button')].find(b=>b.textContent==='表格')!);
  expect(box.querySelectorAll('tbody tr')).toHaveLength(4);
});

test('a commitment whose source is not listed says no more than that; paging asks ?kind=&after= and appends',async()=>{
  const g=structuredClone(graphEx);g.groups[1].items[0].claim_id=null;g.edges=g.edges.filter((e:{source:string})=>!e.source.startsWith('commitment:'));
  g.groups[0].next_cursor='cur-1';g.groups[0].collapsed=1;
  const page=structuredClone(graphEx);page.groups[0].items=[{...graphEx.groups[0].items[0],claim_id:'f'.repeat(64)}];page.groups[0].next_cursor=null;page.groups[0].collapsed=0;
  stub(url=>Response.json(url.includes('after=')?page:g));await mount(h(EvidenceSection,{consumerId:cid}));
  await click(box.querySelector(`[data-node="commitment:${E}"]`));expect(box.querySelector('.evidence-panel')!.textContent).toContain('来源未显示');
  await click([...box.querySelectorAll('button')].find(b=>b.textContent==='载入更多')!);
  expect(urls.at(-1)).toBe(`/api/v1/workbench/consumers/${cid}/evidence-graph?kind=claim&after=cur-1`);
  expect(box.querySelector(`[data-node="claim:${'f'.repeat(64)}"]`)).not.toBeNull();expect(box.textContent).not.toContain('载入更多');
});

test('outside the real world the views say so (404), not as an error',async()=>{
  stub(()=>Response.json({error:'real_world_only'},{status:404}));await mount(h(EvidenceSection,{consumerId:cid}));
  expect(box.textContent).toContain('只在真实世界提供');
});

test('the trace: nine steps in order, none / skipped / restricted said as such, never a value',async()=>{
  stub(()=>Response.json(traceEx));await mount(h(MessageTrace,{messageId:'m-1'}));
  const rows=[...box.querySelectorAll('.trace-steps li')];
  expect(rows.map(r=>r.getAttribute('data-step'))).toEqual([...TRACE_STEPS]);
  expect(rows.map(r=>r.querySelector('.trace-name')!.textContent)).toEqual(TRACE_STEPS.map(s=>STEP_TEXT[s]));
  expect(rows[7].textContent).toContain('没有匹配记录：应当发生但没有记录');expect(rows[8].textContent).toContain('没有本体更新提案');
  expect(rows[1].textContent).toContain('不适用');expect(rows[4].textContent).toContain('无读取权限');expect(rows[8].querySelector('.trace-time')!.textContent).toBe('—');
});

test('the metrics card: cost owner only, claims as a raw visible count with the caveat, gate blocks by code',async()=>{
  stub(()=>Response.json(metricsEx));await mount(h(ConversationMetricsCard,{conversationId:metricsEx.conversation_id}));
  expect(box.textContent).toContain('仅负责人可见');expect(box.textContent).toContain('不等于事实条数');expect(box.textContent).not.toContain('事实数');
  expect(box.textContent).toContain('contact_restricted 2');expect(box.textContent).toContain('中位 1.2 秒');
  act(()=>root.unmount());root=createRoot(box);
  stub(()=>Response.json({...metricsEx,model_cost:{totals:[{currency:'CNY',amount:'12.3400',amount_unit:'major',entries:3}],runs:3,unpriced_entries:1},withheld:[]}));
  await mount(h(ConversationMetricsCard,{conversationId:metricsEx.conversation_id}));
  expect(box.textContent).toContain('12.3400 CNY；另有未定价 1 条');
});

test('a folded duplicate claim shows ×N on the node, the panel and the table with the hint; a missing count is 1',async()=>{
  const hint='同一说法被记录了 2 次，已合并显示；原始记录保留为证据';
  const g=evidenceGraph(graphEx);const claims=g.groups[0].items as {claim_id:string;duplicate_count:number}[];
  expect(claims.find(c=>c.claim_id===C)!.duplicate_count).toBe(2);expect(claims.find(c=>c.claim_id==='d'.repeat(64))!.duplicate_count).toBe(1);
  const bad=structuredClone(graphEx);bad.groups[0].items[0].duplicate_count=0;expect(()=>evidenceGraph(bad)).toThrow(WorkbenchError);
  stub(()=>Response.json(graphEx));await mount(h(EvidenceSection,{consumerId:cid}));
  const node=box.querySelector(`[data-node="claim:${C}"]`)!;
  expect(node.querySelector('.dup-badge')!.textContent).toBe(hint+'×2');expect(node.getAttribute('aria-label')).toContain(hint);
  expect(box.querySelectorAll('.dup-badge')).toHaveLength(1);
  await click(node);
  const mark=box.querySelector('.evidence-panel .dup-mark')!;expect(mark.textContent).toBe('×2');expect(mark.getAttribute('title')).toBe(hint);
  await click([...box.querySelectorAll('button')].find(b=>b.textContent==='表格')!);
  const marks=[...box.querySelectorAll('table .dup-mark')];expect(marks.map(m=>m.textContent)).toEqual(['×2']);expect(marks[0].getAttribute('title')).toBe(hint);
});
