// World model W2 (owner 2026-10-19 D3; ADR-035): instances by type with a cursor, the object drawer and its depth-1 neighbourhood.
import {createElement as h} from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
import {expect,test} from 'vitest';
import {readFileSync} from 'node:fs';
import {WorkbenchError,objectNeighbors,objectPage,worldModel} from '../src/workbench/api';
import {neighborhoodGraph,objectPage as pageFor} from '../src/workbench/pages/WorldInstances';
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
  expect(worldQuery(r.section,r.query)).toEqual({graph:false,focus:null,type:'Consumer',after:A,object:A});
  expect(worldQuery('actions',{type:'Consumer',object:A})).toMatchObject({type:null,object:null});
  // A link of the earlier single page opens the same content in its section.
  const legacy=parseRoute('/workbench/ontology',`?tab=instances&type=Consumer&object=${A}`);
  expect(routePath(legacy)).toBe(`/workbench/ontology/objects?type=Consumer&object=${A}`);
});

test('instances: a chosen type lists ids with versions and times, counts the invisible ones, pages by cursor; no property shown',()=>{
  expect(render({})).toContain('选择一个 Object Type 查看 Object');
  const html=render({type:'Consumer'},[[['workbench','objects','Consumer',''],objectPage(page)]]);
  expect(html).toContain('Consumer 共 7 个 Object · 本页显示 1 个');expect(html).toContain('本页不可见 1 个');expect(html).toContain('data-code="invisible"');
  expect(html).toContain('aaaaaaaa…aaaa');expect(html).toContain('>下一页</button>');expect(html).not.toContain('>上一页</button>');
  expect(html).toContain('<option value="Consumer" selected="">Consumer（12 个）</option>');
});

test('the drawer: full id, versions, a link to the content page where one exists, groups with invisible and collapsed said as they are',()=>{
  const html=render({type:'Consumer',object:A},[[['workbench','objects','Consumer',''],objectPage(page)],
    [['workbench','object-neighbors','Consumer',A],objectNeighbors(neighbors)]]);
  expect(html).toContain(`aria-label="Consumer aaaaaaaa…aaaa 详情"`);expect(html).toContain(A);expect(html).toContain('href="/workbench/consumers/'+A+'"');
  expect(html).toContain('← Goal · Property consumer_id');expect(html).toContain('不可见 25 个');expect(html).toContain('折叠 5 个');
  expect(html).toContain('→ Consumer · Link knows');expect(html).toContain('邻域图：');
  expect(pageFor('Goal',A)).toBeNull();expect(pageFor('Commitment',A)).toEqual({page:'commitments',id:A});
});

test('the neighbourhood graph: the centre, each listed neighbour, one counted node per group with hidden members',()=>{
  const g=neighborhoodGraph(objectNeighbors(neighbors));
  expect(g.nodes.map(n=>n.label)).toEqual(['Consumer aaaaaaaa…aaaa','Goal 另 30 个','Consumer bbbbbbbb…bbbb']);
  expect(g.nodes[1].sub).toBe('不可见 25 · 折叠 5');
  expect(g.edges.map(e=>[e.from.slice(0,9),e.to.slice(0,9),e.label])).toEqual([['~0','Consumer/','consumer_id'],['Consumer/','Consumer/','knows']]);
});
