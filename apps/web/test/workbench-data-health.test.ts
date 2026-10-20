// D-G5 data health: the owner's read (0242) parsed strictly and rendered one card per flow; reasons as words, codes only in titles.
import {createElement as h} from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
import {readFileSync} from 'node:fs';
import {expect,test} from 'vitest';
import {WorkbenchError,dataHealth} from '../src/workbench/api';
import {DataHealth,REASON_TEXT} from '../src/workbench/pages/DataHealth';
import {ALERT_INFO} from '../src/workbench/pages/alertInfo';
import {NAV_GROUPS} from '../src/workbench/WorkbenchApp';
import {PAGES} from '../src/workbench/route';

const ok={status:'ok',reasons:[]};
const clean={computed_at:'2026-10-20T11:00:00+00:00',attention:0,
  extraction:{...ok,feed_pending:0,oldest_pending_seconds:0,jobs:{succeeded:4},dead_lettered:0,failed_24h:0,finished_24h:4,failure_ratio_24h:0},
  recall:{...ok,active_profile:'synthetic-model@3',entries:2,missing_embeddings:0,other_profile_vectors:0,coverage_ratio:1,last_backfill_at:null},
  reflow:{...ok,pending:0,waiting:0,oldest_seconds:0},
  ingestion:{...ok,outbox_pending:0,commercial_receipts_24h:{},commercial_rejected_24h:0,commercial_rejected_ratio_24h:null,commercial_late_24h:0,commercial_max_latency_seconds_24h:0},
  artifacts:{...ok,orphans:0,retention_passed:0,stuck_deleting:0},
  erasure:{...ok,open:0,oldest_open_seconds:0,waiting_unknown_effects:0,purge_items_retrying:0,completed_with_holds:0}};
const faulty={...clean,attention:2,
  extraction:{...clean.extraction,status:'attention',reasons:['dead_letters'],dead_lettered:1,jobs:{dead_lettered:1}},
  recall:{...clean.recall,status:'attention',reasons:['missing_embeddings'],missing_embeddings:1,coverage_ratio:0.5}};
function render(body?:unknown){
  const c=new QueryClient({defaultOptions:{queries:{retry:false,staleTime:Infinity}}});
  if(body!==undefined)c.setQueryData(['workbench','data-health'],dataHealth(body));
  return renderToStaticMarkup(h(QueryClientProvider,{client:c},h(DataHealth)));
}

test('the parser is exact: unknown blocks, negative counts, ratios out of range, an ok status with reasons are refused',()=>{
  expect(dataHealth(clean).attention).toBe(0);
  for(const bad of [{...clean,extra:{}},{...clean,artifacts:{...clean.artifacts,orphans:-1}},{...clean,recall:{...clean.recall,coverage_ratio:1.5}},
    {...clean,erasure:{...clean.erasure,reasons:['purge_retrying']}},{...clean,reflow:{...clean.reflow,status:'bad'}},{...clean,ingestion:{...clean.ingestion,reasons:['Bad Code']}}])
    expect(()=>dataHealth(bad)).toThrow(WorkbenchError);
});

test('one card per flow with a badge; reasons in words with the code in the title; no raw code as text',()=>{
  const html=render(faulty);
  for(const title of ['Claim 提取','召回索引与回填','审核重流','接入','Artifact','删除传播'])expect(html).toContain(`aria-label="${title}"`);
  expect(html.match(/data-status="attention"/g)?.length).toBe(2);expect(html.match(/data-status="ok"/g)?.length).toBe(4);
  expect(html).toContain('需关注（1）');expect(html).toContain('共 <strong>2</strong> 项需关注');
  expect(html).toContain(`title="dead_letters">${REASON_TEXT.dead_letters}`);expect(html).toContain('50.0%');expect(html).toContain('死信 1');
  expect(html).not.toMatch(/>dead_letters</);expect(html).not.toMatch(/<form|<textarea/);
  expect(render(clean)).toContain('各项正常');
});

test('before the read arrives nothing is shown as a count (the shared page states cover failure as "暂时无法读取")',()=>{
  const html=render(undefined);
  expect(html).toContain('正在加载');expect(html).not.toContain('各项正常');expect(html).not.toContain('aria-label="Claim 提取"');
});

test('the page sits in 模型与资产 after 数据资产, and every v6 data health rule has a business name on it',()=>{
  expect(PAGES.data_health).toBe('数据健康');
  expect(NAV_GROUPS.find(g=>g.title==='模型与资产')?.pages).toEqual(['ontology','data_assets','data_health','knowledge']);
  const rules=JSON.parse(readFileSync(new URL('../../../deploy/configuration/alert-rules.v6.json',import.meta.url),'utf8')).rules as {rule_id:string}[];
  const ours=rules.filter(r=>r.rule_id.startsWith('data_health_'));
  expect(ours.length).toBe(10);for(const r of ours)expect(ALERT_INFO[r.rule_id]?.page).toBe('data_health');
});
