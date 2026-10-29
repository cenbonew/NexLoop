// Object Set (ADR-036; capability gap D-G2): strict parsing, the definitions table, the preview (readable Objects only, restricted
// Properties as 受限, unreadable Objects counted), the withheld set, and the deep link of the selected set and its page.
import {createElement as h} from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
import {asV2} from './world-model-v2';
import {expect,test} from 'vitest';
import {WorkbenchError,objectSetPage,objectSets,worldModel} from '../src/workbench/api';
import {conditionText} from '../src/workbench/pages/WorldObjectSets';
import {WorldModel,worldQuery} from '../src/workbench/pages/WorldModel';
import {parseRoute,routePath} from '../src/workbench/route';
import {readFileSync} from 'node:fs';

const model=asV2(JSON.parse(readFileSync(new URL('../../../packages/contracts/examples/workbench-world-model.valid.json',import.meta.url),'utf8')));
const A='a'.repeat(64),B='b'.repeat(64);
const SETS={object_sets:[{stable_name:'tennis.players',version:2,versions:2,object_type:'Consumer',object_type_version:97,
  filters:[{property:'sport',operator:'eq',value:'网球'},{property:'level',operator:'in',value:[3,4]}],sort:[{property:'level',direction:'desc'}],
  projection:['sport','income'],definition_digest:'c'.repeat(64),published_by:'p',published_at:'2026-10-20T10:00:00Z',reason:'网球中高级客户'}]};
const PAGE={stable_name:'tennis.players',version:2,object_type:'Consumer',status:'ok',total:3,invisible:1,projection:['sport','income'],next_offset:null,
  items:[{object_id:A,values:{sport:'网球'},withheld:['income']},{object_id:B,values:{sport:'网球',income:null},withheld:[]}]};

function render(query:Record<string,string>,seed:[unknown[],unknown][]){
  const c=new QueryClient({defaultOptions:{queries:{retry:false,staleTime:Infinity}}});
  c.setQueryData(['workbench','world-model'],worldModel(model));
  for(const [k,v] of seed)c.setQueryData(k,v);
  return renderToStaticMarkup(h(QueryClientProvider,{client:c},h(WorldModel,{section:'object_sets',query,go:()=>{}})));
}

test('parsers are strict: known operators and directions, scalars only, a withheld page carries nothing',()=>{
  const sets=objectSets(SETS);
  expect(sets[0].filters.map(conditionText)).toEqual(['sport 等于 “网球”','level 属于 3、4']);
  const bad=structuredClone(SETS);(bad.object_sets[0].filters[0] as {operator:string}).operator='like';
  expect(()=>objectSets(bad)).toThrow(WorkbenchError);
  const nested=structuredClone(SETS);(nested.object_sets[0].filters[0] as {value:unknown}).value={x:1};
  expect(()=>objectSets(nested)).toThrow(WorkbenchError);
  expect(objectSetPage(PAGE).items[0].withheld).toEqual(['income']);
  expect(objectSetPage({stable_name:'s',version:1,object_type:'Consumer',status:'withheld',items:[],total:null})).toMatchObject({items:[],total:null,invisible:0});
  expect(()=>objectSetPage({...PAGE,status:'partial'})).toThrow(WorkbenchError);
});

test('the page lists the sets and previews the selected one: restricted Properties as 受限, unreadable Objects counted only',()=>{
  const list=render({},[[['workbench','object-sets'],objectSets(SETS)]]);
  expect(list).toContain('tennis.players');expect(list).toContain('sport 等于 “网球”；level 属于 3、4');expect(list).toContain('共 2 个版本');
  expect(list).toContain('选择一个 Object Set 预览');
  const preview=render({set:'tennis.players'},[[['workbench','object-sets'],objectSets(SETS)],
    [['workbench','object-set-members','tennis.players',2,0],objectSetPage(PAGE)]]);
  expect(preview).toContain('符合条件 3 个');expect(preview).toContain('本页不可见 1 个');
  expect(preview).toContain('受限');expect(preview).toContain('aaaaaaaa…aaaa');
  expect(preview.replace(/(title|aria-label|data-id)="[^"]*"/g,'')).not.toContain(A);  // the whole id only in attributes (title, label, copy), never in the text
  const held=render({set:'tennis.players'},[[['workbench','object-sets'],objectSets(SETS)],
    [['workbench','object-set-members','tennis.players',2,0],objectSetPage({stable_name:'tennis.players',version:2,object_type:'Consumer',status:'withheld',items:[],total:null})]]);
  expect(held).toContain('无权查看这个 Object Set 的预览');expect(held).not.toContain('符合条件');
  expect(render({},[[['workbench','object-sets'],[]]])).toContain('暂无 Object Set');
});

test('deep link: the selected set and its page offset round-trip under /workbench/ontology/object-sets',()=>{
  const r=parseRoute('/workbench/ontology/object-sets','?set=tennis.players&offset=25&x=1');
  expect(r).toEqual({page:'ontology',section:'object_sets',query:{set:'tennis.players',offset:'25'}});
  expect(routePath(r)).toBe('/workbench/ontology/object-sets?set=tennis.players&offset=25');
  expect(worldQuery(r.section,r.query)).toMatchObject({set:'tennis.players',offset:25});
  expect(parseRoute('/workbench/ontology/object-sets','?set=Bad&offset=5000').query).toBeUndefined();
});
