// World model W2 (owner 2026-10-19 D3; ADR-035): instances by type with a cursor, the object drawer and its depth-1 neighbourhood.
import {createElement as h} from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
import {expect,test} from 'vitest';
import {readFileSync} from 'node:fs';
import {WorkbenchError,objectNeighbors,objectPage,objectValues,worldModel} from '../src/workbench/api';
import {neighborhoodGraph,objectPage as pageFor,propertyColumns} from '../src/workbench/pages/WorldInstances';
import {WorldModel,worldQuery} from '../src/workbench/pages/WorldModel';
import {parseRoute,routePath} from '../src/workbench/route';

const json=(name:string)=>JSON.parse(readFileSync(new URL(`../../../packages/contracts/examples/${name}.valid.json`,import.meta.url),'utf8'));
const model=json('workbench-world-model'),page=json('workbench-object-page'),neighbors=json('workbench-object-neighbors');
const A='a'.repeat(64);

function render(query:Record<string,string>,seed:[unknown[],unknown][]=[]){  // the Object page (section objects)
  const c=new QueryClient({defaultOptions:{queries:{retry:false,staleTime:Infinity}}});
  c.setQueryData(['workbench','world-model'],worldModel(model));
  for(const [k,v] of seed)c.setQueryData(k,v);
  return renderToStaticMarkup(h(QueryClientProvider,{client:c},h(WorldModel,{section:'objects',query,go:()=>{}})));
}

test('parsers follow the contracts: identifiers only, depth 1, bad ids refused',()=>{
  expect(objectPage(page).invisible).toBe(1);expect(objectNeighbors(neighbors).groups[0].collapsed).toBe(5);
  const bad=structuredClone(page);bad.items[0].object_id='../x';expect(()=>objectPage(bad)).toThrow(WorkbenchError);
  const deep=structuredClone(neighbors);deep.depth=2;expect(()=>objectNeighbors(deep)).toThrow(WorkbenchError);
});

test('deep links: type, cursor and open object round-trip on the Object page; they only count there',()=>{
  const r=parseRoute('/workbench/ontology/objects',`?type=Consumer&after=${A}&object=${A}`);
  expect(r.section).toBe('objects');
  expect(routePath(r)).toBe(`/workbench/ontology/objects?type=Consumer&after=${A}&object=${A}`);
  expect(worldQuery(r.section,r.query)).toEqual({graph:false,focus:null,type:'Consumer',after:A,object:A,set:null,offset:0});
  expect(worldQuery('actions',{type:'Consumer',object:A})).toMatchObject({type:null,object:null});
  // A link of the earlier single page opens the same content in its section.
  const legacy=parseRoute('/workbench/ontology',`?tab=instances&type=Consumer&object=${A}`);
  expect(routePath(legacy)).toBe(`/workbench/ontology/objects?type=Consumer&object=${A}`);
});

test('instances: a chosen type lists ids with versions and times, counts the invisible ones, pages by cursor; no property shown',()=>{
  expect(render({})).toContain('暂无选中的 Object Type');
  const html=render({type:'Consumer'},[[['workbench','objects','Consumer',''],objectPage(page)]]);
  expect(html).toContain('<span class="term"><code class="term-id">Consumer</code></span> 共 7 个 Object · 本页显示 1 个');expect(html).toContain('本页不可见 1 个');expect(html).toContain('data-code="invisible"');
  expect(html).toContain('aaaaaaaa…aaaa');expect(html).toContain('>下一页</button>');expect(html).not.toContain('>上一页</button>');
  expect(html).toContain('<option value="Consumer" selected="">Consumer · 12 个</option>');
});

test('the drawer: full id, versions, a link to the content page where one exists, groups with invisible and collapsed said as they are',()=>{
  const html=render({type:'Consumer',object:A},[[['workbench','objects','Consumer',''],objectPage(page)],
    [['workbench','object-neighbors','Consumer',A],objectNeighbors(neighbors)]]);
  expect(html).toContain(`aria-label="Consumer aaaaaaaa…aaaa 详情"`);expect(html).toContain(A);expect(html).toContain('href="/workbench/consumers/'+A+'"');
  expect(html).toContain('← <span class="term"><code class="term-id">Goal</code></span> · Property（属性） <span class="term"><code class="term-id">consumer_id</code></span>');expect(html).toContain('不可见 25 个');expect(html).toContain('折叠 5 个');
  expect(html).toContain('→ <span class="term"><code class="term-id">Consumer</code></span> · Link（关系） <span class="term"><code class="term-id">knows</code></span>');expect(html).toContain('邻域图：');
  expect(pageFor('Goal',A)).toBeNull();expect(pageFor('Commitment',A)).toEqual({page:'commitments',id:A});
});

test('the neighbourhood graph: the centre, each listed neighbour, one counted node per group with hidden members',()=>{
  const g=neighborhoodGraph(objectNeighbors(neighbors));
  expect(g.nodes.map(n=>n.label)).toEqual(['Consumer aaaaaaaa…aaaa','Goal 另 30 个','Consumer bbbbbbbb…bbbb']);
  expect(g.nodes[1].sub).toBe('不可见 25 · 折叠 5');
  expect(g.edges.map(e=>[e.from.slice(0,9),e.to.slice(0,9),e.label])).toEqual([['~0','Consumer/','consumer_id'],['Consumer/','Consumer/','knows']]);
});

test('D-G1 (owner 2026-10-20): property values in the table and the drawer; withheld named with one badge; long values cut and expandable',()=>{
  const long='长'.repeat(80);
  const withValues=structuredClone(page);withValues.items[0].properties=[{name:'display_name',status:'ok',value:'示例客户',value_type:'text'},
    {name:'monthly_income',status:'withheld',reason:'restricted'},{name:'note',status:'ok',value:long,value_type:'text'},{name:'nickname',status:'missing'},
    {name:'referrer',status:'withheld',reason:'erasing'}];
  const parsed=objectPage(withValues);
  expect(propertyColumns(parsed.items)).toEqual(['display_name','monthly_income','note','nickname','referrer']);
  const html=render({type:'Consumer'},[[['workbench','objects','Consumer',''],parsed]]);
  expect(html).toContain('class="table-scroll"');expect(html).toContain('示例客户');
  expect(html.match(/data-code="withheld"/g)?.length).toBe(2);expect(html).toContain('>'+long.slice(0,48)+'…</summary>');
  expect(html).toContain('<details class="property-value">');expect(html).toContain('title="负责人已限制这一属性组"');
  // A value the server never sent cannot be shown: a withheld entry carries no value field even if one is forged.
  const forged=structuredClone(withValues);forged.items[0].properties[1].value='20000';expect(objectPage(forged).items[0].properties[1]).toEqual({name:'monthly_income',status:'withheld',reason:'restricted'});
  const bad=structuredClone(withValues);bad.items[0].properties[0].status='shown';expect(()=>objectPage(bad)).toThrow(WorkbenchError);
  // The drawer reads the object's values and lists every property, expanded.
  const values=objectValues(json('workbench-object-values'));
  const drawer=render({type:'Consumer',object:A},[[['workbench','objects','Consumer',''],parsed],[['workbench','object-neighbors','Consumer',A],objectNeighbors(neighbors)],
    [['workbench','object-values','Consumer',A],values]]);
  expect(drawer).toContain('<h3>属性</h3>');expect(drawer).toContain('<dt>display_name</dt>');expect(drawer).toContain('示例客户');expect(drawer).toContain('<dt>monthly_income</dt>');
});
