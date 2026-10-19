// World model W4 (owner 2026-10-19): the data assets page on the owner's metrics read (L6 0224 data_assets, 0216 candidates).
import {createElement as h} from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
import {expect,test} from 'vitest';
import {WorkbenchError,artifactPage,metrics} from '../src/workbench/api';
import {readFileSync} from 'node:fs';
import {bytes} from '../src/workbench/pages/DataAssets';
import {DataAssets} from '../src/workbench/pages/DataAssets';
import {PAGES} from '../src/workbench/route';

const base={refusals:{last_5m:{},last_hour:{}},effects:{by_state:{},unknown:{count:0,oldest_age_seconds:null,over_24h:0}},
  replies:{pending:{count:0,oldest_age_seconds:null},escalations_last_hour:{},escalations_total:0},commitments:{exceptions:{},exceptions_last_hour:{}},
  queues:{},feeds:{},extraction:{pending_messages:0,oldest_pending_seconds:null},guard:{status:'unavailable'},host:{status:'unavailable'},pool:{status:'unavailable'},
  connections:{total:3},backup:{status:'unavailable'},computed_at:'2026-10-19T09:00:00+00:00'};
const assets={claims:{total:3,by_epistemic_kind:{commitment:1,preference:2},by_resolution_state:{resolved:1,unresolved:2}},
  extraction_runs:{total:1,last_at:'2026-10-19T08:00:00+00:00'},extraction_jobs:{succeeded:4},
  recall:{entries:{definition:1,instance:1},profiles:[{profile_id:'synthetic-model@3',dimension:3,created_at:'2026-10-19T07:00:00+00:00',vectors:1}],
    backfills:[{profile_id:'synthetic-model@3',runs:2,first_at:'2026-10-19T07:00:00+00:00',last_at:'2026-10-19T07:30:00+00:00',last_queued:7,last_definition_types:3}]},
  embedding_budget:{status:'unavailable',reason:'not_recorded'}};
const artifacts=JSON.parse(readFileSync(new URL('../../../packages/contracts/examples/workbench-artifacts.valid.json',import.meta.url),'utf8'));
function render(body:unknown,list:unknown=artifacts){
  const c=new QueryClient({defaultOptions:{queries:{retry:false,staleTime:Infinity}}});
  c.setQueryData(['workbench','metrics'],metrics(body));
  c.setQueryData(['workbench','artifacts',''],artifactPage(list));
  return renderToStaticMarkup(h(QueryClientProvider,{client:c},h(DataAssets)));
}

test('the parser: the blocks are optional (an older backend gives null), malformed counts are refused',()=>{
  expect(metrics(base).data_assets).toBeNull();expect(metrics(base).candidates).toBeNull();
  expect(metrics({...base,data_assets:assets}).data_assets?.recall.profiles[0].vectors).toBe(1);
  expect(()=>metrics({...base,data_assets:{...assets,claims:{...assets.claims,total:-1}}})).toThrow(WorkbenchError);
  expect(PAGES.data_assets).toBe('数据资产');
});

test('every section with counts; the embedding budget and the Artifact list said as not available, never 0; no message text',()=>{
  const html=render({...base,data_assets:assets,candidates:{staged:2,merge_configuration_active:false,staged_without_configuration:2}});
  expect(html).toContain('<strong>3</strong> 条 Claim');expect(html).toContain('偏好（preference）：2');expect(html).toContain('完成（succeeded）：4');
  expect(html).toContain('synthetic-model@3');expect(html).toContain('定义（definition）：1');
  expect(html).toContain('data-code="not_recorded"');expect(html).toContain('不表示花费为 0');
  expect(html).toContain('data-code="merge_configuration_missing"');expect(html).toContain('其中 2 个因没有合并配置而等待');
  expect(html).toContain('没有任何已记录的数据库备份');expect(html).not.toMatch(/<form|<textarea/);
});

test('missing blocks are "不可用", not zero',()=>{
  const html=render(base);
  expect(html).toContain('Claim 与提取的统计不可用');expect(html).toContain('召回索引的统计不可用');expect(html).toContain('合并配置状态不可用');
  expect(html).toContain('Embedding 预算不可用');expect(html).not.toContain('<strong>0</strong> 条 Claim');
});

test('the Artifact list (L6 0225): metadata only, hidden ones counted, a cursor to the next page; bad rows refused',()=>{
  const html=render(base);
  expect(html).toContain('共 <strong>3</strong> 个 Artifact · 本页 1 个');expect(html).toContain('隐藏 1 个（所属客户正在删除）');
  expect(html).toContain('aaaaaaaa…');expect(html).toContain('消息上下文');expect(html).toContain('1.2 KB');expect(html).toContain('bbbbbbbbbbbb');
  expect(html).toContain('href="/workbench/consumers/'+'c'.repeat(64)+'"');expect(html).toContain('>下一页</button>');expect(html).not.toContain('>上一页</button>');
  expect(html).not.toContain('Artifact 列表不可用');
  expect(render(base,{items:[],total:0,hidden:0,next_before:null})).toContain('还没有 Artifact');
  const bad=structuredClone(artifacts);bad.items[0].purpose='raw_upload';expect(()=>artifactPage(bad)).toThrow(WorkbenchError);
  const content=structuredClone(artifacts);content.items[0].sha256_prefix='b'.repeat(64);expect(()=>artifactPage(content)).toThrow(WorkbenchError);
  expect([bytes(512),bytes(2048),bytes(3*1024*1024)]).toEqual(['512 B','2.0 KB','3.0 MB']);
});
