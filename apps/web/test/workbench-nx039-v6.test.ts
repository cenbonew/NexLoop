// NX-039 V6 workbench (D39-7, D39-8, D39-10, D39-11, D39-14): the review queue (gates, L0/L1 scores, non-inferiority, class shown only),
// the three release buttons only for a role carrying the release scope's Actions, reasons required, the 7-day watch and its rollback
// hint (never automatic), the semantic_note preview, and every code as a Chinese label.
import {createElement as h} from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
import {expect,test} from 'vitest';
import {WorkbenchError,semanticReleases,semanticReview,type Me} from '../src/workbench/api';
import {NotePreview,SemanticReleaseSection,WatchBadge,gateSummary,niText,watchVersion} from '../src/workbench/pages/SemanticReleases';

const C='c'.repeat(8)+'-cccc-4ccc-8ccc-'+'c'.repeat(12);
const GATES=['references_valid','structure_valid','zero_overreach','zero_cross_tenant','zero_fact_pollution','no_forbidden_regression','semantic_regression',
  'cost_performance','semantic_conflict','index_migration_impact'];
const REVIEW={candidates:[{candidate_id:C,status:'accepted_for_release',end_reason:null,dimension:'semantic_content',change_kind:'alias',report_digest:'d'.repeat(64),
    level:'L0',conclusion:'accept',reason:null,change_class:'minor',hard_gates_version:'nx039-gates-1',
    gates:GATES.map((g,i)=>({gate:g,result:i===9?'fail':'pass'})),
    scores:{design:{cases:6,parent:0.5,candidate:0.83},validation:{cases:4,parent:0.5,candidate:0.75},ambiguity_rate:0.1,regressions:0,missing_cases:0},
    non_inferiority:{primary_metric:'coverage',margin:0.01,ties_accept:false},cases:10,cases_digest:'e'.repeat(64),published_version:'0.1.0',recorded_at:'2026-10-20T10:00:00Z'}],
  stops:[{dimension:'semantic_content',open:true,stopped:false,revision:0,changed_at:null}],
  watch:{active:1,regressing:1,releases:[{release_ref:'release:0.1.0',published_at:'2026-10-20T11:00:00Z',watch_until:'2026-10-27T11:00:00Z',accesses_before:40,accesses_after:30,
    miss_rate_before:0.1,miss_rate_after:0.15,ambiguity_rate_before:0.05,ambiguity_rate_after:0.05,judged:true,regressing:true,suggestion:'rollback'}]}};
const RELEASES={current:'0.1.0',releases:[{version:'0.1.0',reason:'publish',candidate_id:C,change_class:'minor',report_digest:'d'.repeat(64),rolled_back_to:null,rollback_report:null,
  owner_reason:'发布',recorded_at:'2026-10-20T11:00:00Z',items:[{kind:'semantic_note',ref:'eios:object_type:Product',body:'只指实物商品'}]},
  {version:'0.0.1',reason:'publish',candidate_id:C,change_class:'patch',report_digest:'d'.repeat(64),rolled_back_to:null,rollback_report:null,owner_reason:'首发',
    recorded_at:'2026-10-19T11:00:00Z',items:[]}],publishable:[{candidate_id:C,change_class:'patch',change_kind:'description',target_refs:['eios:object_type:Product'],term_hashes:1,parent_ok:true}]};
const me=(role:string,actions:string[]):Me=>({tenant_id:'t',principal_id:'p',role,actions} as unknown as Me);
const SCOPE=['eios:action:nexloop.semantic_release.publish:1','eios:action:nexloop.semantic_release.rollback:1','eios:action:nexloop.semantic_release.stop:1'];

function render(member:Me){
  const c=new QueryClient({defaultOptions:{queries:{retry:false,staleTime:Infinity}}});
  c.setQueryData(['workbench','me'],member);
  c.setQueryData(['workbench','semantic-releases'],semanticReleases(RELEASES));
  c.setQueryData(['workbench','semantic-review'],semanticReview(REVIEW));
  return renderToStaticMarkup(h(QueryClientProvider,{client:c},h(SemanticReleaseSection)));
}

test('parsing is strict: gates carry only gate and result, digests are 64 hex, rates are numbers',()=>{
  const v=semanticReview(REVIEW);
  expect(v.candidates[0].gates).toHaveLength(10);expect(v.watch.releases[0].suggestion).toBe('rollback');
  expect(()=>semanticReview({...REVIEW,candidates:[{...REVIEW.candidates[0],gates:[{gate:'g',result:'pass',evidence_ids:['run-1']}]}]})).toThrow(WorkbenchError);
  expect(()=>semanticReview({...REVIEW,candidates:[{...REVIEW.candidates[0],report_digest:'run-123'}]})).toThrow(WorkbenchError);
  expect(()=>semanticReview({...REVIEW,candidates:[{...REVIEW.candidates[0],scores:{...REVIEW.candidates[0].scores,ambiguity_rate:'10%'}}]})).toThrow(WorkbenchError);
});

test('the three buttons only for a role carrying the release scope; an operator sees no section at all',()=>{
  const owner=render(me('owner',SCOPE));
  expect(owner).toContain('评估与审核');expect(owner).toContain('>停止</button>');expect(owner).toContain('>发布</button>');expect(owner).toContain('>回退</button>');
  const operator=render(me('operator',['eios:action:nexloop.workbench.read:1']));
  expect(operator).toBe('');
  // Only the stop Action: the stop button, no publish or rollback form.
  const stopper=render(me('owner',[SCOPE[2]]));
  expect(stopper).toContain('>停止</button>');expect(stopper).not.toContain('>发布</button>');expect(stopper).not.toContain('>回退</button>');
});

test('reasons are required: every release button starts disabled until its reason is written',()=>{
  const html=render(me('owner',SCOPE));
  for(const title of ['停止','发布','回退']){
    const button=new RegExp(`<button type="submit"[^>]*disabled=""[^>]*>${title}</button>`);
    expect(html,title).toMatch(button);
  }
  expect(html).toContain('原因（必填）');
});

test('the report: gates as G labels with Chinese results, scores and non-inferiority in words, class shown and never an input',()=>{
  const html=render(me('owner',SCOPE));
  for(const label of ['G1 引用有效','G2 零越权','G3 零跨租户','G4 零事实污染','G5 无禁止的行为回退','G6 确定性语义回归','G7 成本与性能','G8 语义不冲突','G9 索引与迁移影响'])
    expect(html).toContain(label);
  expect(html).toContain('>可发布</span>');expect(html).toContain('>通过</span>');expect(html).toContain('>次要级</span>');expect(html).toContain('>未通过</span>');
  expect(html).toContain('title="由评估器 classify_change 评定，不能修改"');expect(html).not.toMatch(/name="change_class"|id="[^"]*class[^"]*"/);
  expect(gateSummary(semanticReview(REVIEW).candidates[0])).toBe('通过 9/10');
  expect(html).toContain('50.0% → 83.0%（6 个用例）');expect(niText(semanticReview(REVIEW).candidates[0])).toBe('主指标覆盖率；候选不低于基线 1 个百分点以内，持平不算通过');
  expect(html).toContain('10 个 · 集合摘要');
  // No raw code as visible text (the codes stay in titles and data attributes).
  for(const code of ['accepted_for_release','semantic_content','references_valid','minor','accept','pass'])expect(html).not.toMatch(new RegExp(`>${code}<`));
});

test('the watch: 观察中 for 7 days, 劣化 above 2 points warns and points to rollback, never automatic; the note preview at most 200',()=>{
  const v=semanticReview(REVIEW);
  expect(watchVersion(v.watch.releases[0])).toBe('0.1.0');
  const html=render(me('owner',SCOPE));
  expect(html).toContain('role="alert"');expect(html).toContain('劣化超过 2 个百分点，建议回退');expect(html).toContain('系统不会自动回退');
  expect(html).toContain('10.0% → 15.0%（+5.0 个百分点）');expect(html).toContain('>劣化，建议回退</span>');
  const calm=renderToStaticMarkup(h(WatchBadge,{watch:{...v.watch.releases[0],regressing:false,suggestion:null}}));
  expect(calm).toContain('观察中，未劣化');
  expect(renderToStaticMarkup(h(WatchBadge,{watch:{...v.watch.releases[0],regressing:false,judged:false,suggestion:null}}))).toContain('观察中，样本不足');
  expect(html).toContain('补充说明预览');expect(html).toContain('只指实物商品');
  const long=renderToStaticMarkup(h(NotePreview,{ref_:'eios:object_type:Product',text:'字'.repeat(250),counter:true}));
  expect(/<blockquote>([^<]*)<\/blockquote>/.exec(long)![1]).toBe('字'.repeat(200));expect(long).toContain('250/200 字');  // the body is cut at 200
});
