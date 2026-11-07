// 0419: the review queue's candidate category. owner_directed_schema is labelled 负责人指定 with what it is (not induced from failures,
// evidence is the snapshot's property use); an unknown or malformed category shows its raw text with 未识别类别 and never fails the read;
// a read without category (the current backend) shows no category and the rest of the queue as before.
import {createElement as h} from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
import {expect,test} from 'vitest';
import {evoCategory,semanticReview} from '../src/workbench/api';
import {OWNER_DIRECTED_NOTE,ReviewBody} from '../src/workbench/pages/SemanticReleases';

const C='c'.repeat(8)+'-cccc-4ccc-8ccc-'+'c'.repeat(12),D='d'.repeat(8)+'-dddd-4ddd-8ddd-'+'d'.repeat(12);
const candidate=(id:string,over:Record<string,unknown>)=>({candidate_id:id,status:'evaluated',end_reason:null,dimension:'ontology_schema',change_kind:'add_optional_property',
  report_digest:'d'.repeat(64),level:'L1',conclusion:'accept',reason:null,change_class:'minor',hard_gates_version:'nx040-gates-1',gates:[{gate:'structure_valid',result:'pass'}],
  scores:{design:{cases:2,parent:0.5,candidate:1},validation:{cases:2,parent:0.5,candidate:1},ambiguity_rate:0,regressions:0,missing_cases:0},
  non_inferiority:null,cases:4,cases_digest:'e'.repeat(64),published_version:null,recorded_at:'2026-11-07T08:00:00Z',...over});
const review=(...cs:unknown[])=>({candidates:cs,stops:[{dimension:'ontology_schema',open:true,stopped:false,revision:0,changed_at:null}],watch:{active:0,regressing:0,releases:[]}});
const html=(v:unknown)=>renderToStaticMarkup(h(QueryClientProvider,{client:new QueryClient()},h(ReviewBody,{review:semanticReview(v)})));

test('parser: known categories, an unknown or malformed one kept raw and marked, absent is null',()=>{
  expect(evoCategory('owner_directed_schema')).toEqual({code:'owner_directed_schema',known:true});
  expect(evoCategory('retrieval_miss')).toEqual({code:'retrieval_miss',known:true});
  expect(evoCategory('owner_hint')).toEqual({code:'owner_hint',known:false});expect(evoCategory(7)).toEqual({code:'7',known:false});
  expect(evoCategory(undefined)).toBeNull();expect(evoCategory(null)).toBeNull();
});

test('owner_directed_schema is labelled 负责人指定 in the queue and its detail says what it is',()=>{
  const out=html(review(candidate(C,{category:'owner_directed_schema'})));
  expect(out).toContain('data-category="owner_directed_schema"');expect(out).toContain('负责人指定');
  expect(out).toContain(OWNER_DIRECTED_NOTE);expect(out).toContain('候选类别');
  expect(OWNER_DIRECTED_NOTE).toBe('负责人指定加的属性，不是从失败中归纳的候选；证据是快照里的属性使用。人工审核和发布许可照常，门槛不变');
  expect(out).toContain(`title="${OWNER_DIRECTED_NOTE}"`);
});

test('an unknown category shows its raw text with 未识别类别; the other rows and the stop switch render as usual',()=>{
  const out=html(review(candidate(C,{category:'owner_hint'}),candidate(D,{category:'retrieval_miss',dimension:'semantic_content'})));
  expect(out).toContain('data-category="unknown"');expect(out).toContain('owner_hint（未识别类别）');
  expect(out).toContain('检索未命中');expect(out).not.toContain(OWNER_DIRECTED_NOTE);
  expect(out).toContain('演进停止开关');expect(out).toContain('运行中');expect(out.match(/report-detail/g)).toHaveLength(2);
});

test('the current backend sends no category: no category shown, the queue unchanged',()=>{
  const out=html(review(candidate(C,{})));
  expect(semanticReview(review(candidate(C,{}))).candidates[0].category).toBeNull();
  expect(out).not.toContain('data-category');
  // the candidate cell is the same markup as before the category (no extra whitespace node)
  expect(out).not.toMatch(/<\/span> <\/td>|<\/code> <\/td>/);expect(out).not.toContain('候选类别');expect(out).toContain('report-detail');
});
