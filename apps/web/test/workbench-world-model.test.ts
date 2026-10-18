// Enterprise world model W1 (owner 2026-10-19 D1-D7): the read parser, the deterministic layout, the page's tables, graph and URL state.
import {createElement as h} from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
import {expect,test} from 'vitest';
import {readFileSync} from 'node:fs';
import {WorkbenchError,worldModel} from '../src/workbench/api';
import {NODE_H,NODE_W,hue,layout} from '../src/workbench/graph/layout';
import {WorldModel,worldQuery} from '../src/workbench/pages/WorldModel';
import {parseRoute,routePath,PAGES} from '../src/workbench/route';

const example=JSON.parse(readFileSync(new URL('../../../packages/contracts/examples/workbench-world-model.valid.json',import.meta.url),'utf8'));

test('the parser accepts the contract example and refuses a withheld property that carries more than its name',()=>{
  const m=worldModel(example);
  expect(m.object_types[0].properties[1]).toEqual({name:'spending_power',withheld:true});
  const leaky=structuredClone(example);leaky.object_types[0].properties[1].value_type='string';
  expect(()=>worldModel(leaky)).toThrow(WorkbenchError);
  const bad=structuredClone(example);bad.relation_types[0].cardinality='some';
  expect(()=>worldModel(bad)).toThrow(WorkbenchError);
});

test('layout: deterministic, every node inside the box, no two node boxes overlap (ring and force)',()=>{
  for(const n of [1,2,5,8,9,24,40]){
    const nodes=Array.from({length:n},(_,i)=>({id:`T${String(i).padStart(2,'0')}`,label:`T${i}`}));
    const edges=nodes.slice(1).map((x,i)=>({id:'e'+i,from:nodes[Math.floor(i/3)].id,to:x.id}));
    const a=layout(nodes,edges),b=layout([...nodes].reverse(),edges);
    expect(b).toEqual(a);
    for(const p of a.nodes){expect(p.x-NODE_W/2).toBeGreaterThanOrEqual(0);expect(p.y-NODE_H/2).toBeGreaterThanOrEqual(0);expect(p.x+NODE_W/2).toBeLessThanOrEqual(a.width);expect(p.y+NODE_H/2).toBeLessThanOrEqual(a.height);}
    for(let i=0;i<a.nodes.length;i++)for(let j=i+1;j<a.nodes.length;j++){
      const p=a.nodes[i],q=a.nodes[j];expect(Math.abs(p.x-q.x)>=NODE_W-1||Math.abs(p.y-q.y)>=NODE_H-1,`${n}: ${p.id}/${q.id}`).toBe(true);}
  }
  expect(hue('Consumer')).toBe(hue('Consumer'));expect(hue('Consumer')).toBeGreaterThanOrEqual(0);expect(hue('Consumer')).toBeLessThan(8);
});

test('URL state: tab, graph view and focus round-trip; graph only for Object Type and Link; renamed page',()=>{
  expect(PAGES.ontology).toBe('企业世界模型');
  const r=parseRoute('/workbench/ontology','?tab=relations&view=graph&focus=Consumer&x=1');
  expect(r.query).toEqual({tab:'relations',view:'graph',focus:'Consumer'});
  expect(routePath(r)).toBe('/workbench/ontology?tab=relations&view=graph&focus=Consumer');
  expect(worldQuery({tab:'actions',view:'graph'})).toEqual({tab:'actions',graph:false,focus:null});
  expect(worldQuery(undefined)).toEqual({tab:'object_types',graph:false,focus:null});
});

function render(query?:Record<string,string>){
  const c=new QueryClient({defaultOptions:{queries:{retry:false,staleTime:Infinity}}});
  c.setQueryData(['workbench','world-model'],worldModel(example));
  return renderToStaticMarkup(h(QueryClientProvider,{client:c},h(WorldModel,{query,onQuery:()=>{}})));
}

test('the page: counts, six tabs, tables by default, the restricted property by name only, no edit control',()=>{
  const html=render();
  expect(html).toContain('企业世界模型');expect(html).toContain('只读');
  for(const tab of ['Object Type','Property','Link','事件类型','Action','Function'])expect(html).toContain(`>${tab}</button>`);
  expect(html).toContain('aria-label="Object Type 2 个，查看"');expect(html).toContain('<strong style="display:block;font-size:22px">42</strong>');
  expect(html).toContain('aria-label="Object Type"');expect(html).not.toContain('<svg');
  expect(html).not.toMatch(/新建|编辑|删除/);
  const props=render({tab:'properties'});
  expect(props).toContain('spending_power');expect(props).toContain('data-code="withheld"');expect(props).toContain('负责人限制的属性组：只显示名称');
  const actions=render({tab:'actions'});
  expect(actions).toContain('nexloop.service.request');expect(actions).toContain('>有</span>');expect(actions).not.toContain('>表格</button>');
});

test('the graph view: one focusable button per type, labelled edges, the focused type panel beside it',()=>{
  const html=render({view:'graph',focus:'Consumer'});
  expect(html).toContain('<svg');expect(html).toContain('aria-label="对象类型关系图：2 个类型，1 条关系"');
  expect(html.match(/role="button" tabindex="0"/g)?.length).toBe(2);
  expect(html).toContain('consumer_conversation 1:N');expect(html).toContain('aria-label="Consumer 详情"');expect(html).toContain('→ Conversation：consumer_conversation（1:N）');
  expect(html).toContain('aria-selected="true" tabindex="0" class="tab tab-selected">关系图');
});
