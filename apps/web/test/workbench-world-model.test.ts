// Enterprise world model W1 (owner 2026-10-19 D1-D7): the read parser, the deterministic layout, the page's tables, graph and URL state.
import {createElement as h} from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
import {expect,test} from 'vitest';
import {readFileSync} from 'node:fs';
import {WorkbenchError,worldModel} from '../src/workbench/api';
import {GAP_X,NODE_H,NODE_W,gridColumns,hue,layout,selfLoop} from '../src/workbench/graph/layout';
import {LABEL_PX,MIN_LABEL_PX,MIN_SCALE,SchemaGraph,fitScale} from '../src/workbench/graph/SchemaGraph';
import {WorldModel,worldQuery} from '../src/workbench/pages/WorldModel';
import {parseRoute,routePath,PAGES} from '../src/workbench/route';

const example=JSON.parse(readFileSync(new URL('../../../packages/contracts/examples/workbench-world-model-v2.valid.json',import.meta.url),'utf8'));

test('the parser accepts the contract example and refuses a withheld property that carries more than its name',()=>{
  const m=worldModel(example);
  expect(m.object_types[0].properties[1]).toEqual({name:'spending_power',withheld:true,gloss:null,gloss_source:null});
  const leaky=structuredClone(example);leaky.object_types[0].properties[1].value_type='string';
  expect(()=>worldModel(leaky)).toThrow(WorkbenchError);
  const bad=structuredClone(example);bad.relation_types[0].cardinality='some';
  expect(()=>worldModel(bad)).toThrow(WorkbenchError);
});

test('layout: deterministic, every node inside the box, no two node boxes overlap (grid placement around the most connected type)',()=>{
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

test('URL state: one path per concept, the Graph view and focus round-trip on Object Type only; the group keeps its name',()=>{
  expect(PAGES.ontology).toBe('企业世界模型');
  const r=parseRoute('/workbench/ontology/object-types','?view=graph&focus=Consumer&x=1');
  expect(r).toEqual({page:'ontology',section:'object_types',query:{view:'graph',focus:'Consumer'}});
  expect(routePath(r)).toBe('/workbench/ontology/object-types?view=graph&focus=Consumer');
  for(const [path,section] of [['object-types','object_types'],['objects','objects'],['properties','properties'],['links','links'],['functions','functions'],['actions','actions']])
    expect(parseRoute('/workbench/ontology/'+path).section).toBe(section);
  expect(parseRoute('/workbench/ontology')).toEqual({page:'ontology'});
  expect(parseRoute('/workbench/ontology/links','?view=graph').query).toBeUndefined();  // the view belongs to Object Type
  expect(parseRoute('/workbench/ontology/object-sets')).toEqual({page:'ontology',section:'object_sets'});  // ADR-036
  expect(parseRoute('/workbench/ontology/object-views')).toEqual({page:'ontology'});  // no page without NexLoop data
  expect(worldQuery('actions',{view:'graph'})).toEqual({graph:false,focus:null,type:null,after:null,object:null,set:null,offset:0});
  expect(worldQuery('object_types',undefined)).toEqual({graph:false,focus:null,type:null,after:null,object:null,set:null,offset:0});
  // Links of the earlier single page land on the concept they showed.
  expect(routePath(parseRoute('/workbench/ontology','?tab=relations&view=graph&focus=Consumer'))).toBe('/workbench/ontology/links');
  expect(routePath(parseRoute('/workbench/ontology','?view=graph&focus=Consumer'))).toBe('/workbench/ontology/object-types?view=graph&focus=Consumer');
});

function render(query?:Record<string,string>,section?:string){
  const c=new QueryClient({defaultOptions:{queries:{retry:false,staleTime:Infinity}}});
  c.setQueryData(['workbench','world-model'],worldModel(example));
  return renderToStaticMarkup(h(QueryClientProvider,{client:c},h(WorldModel,{section:section as never,query,go:()=>{}})));
}

test('the summary: one card per concept in EIOS order with its count, the Event Types, EIOS terms, no edit control',()=>{
  const html=render();
  expect(html).toContain('企业世界模型');expect(html).toContain('只读');
  const cards=[...html.matchAll(/aria-label="([A-Za-z ]+)（[^）]+）(?: · real)? (\d+)，查看"/g)].map(m=>m[1]);
  expect(cards).toEqual(['Object Type','Object','Property','Link','Function','Action']);
  expect(html).toContain('aria-label="Object Type（对象类型） 2，查看"');expect(html).toContain('<strong class="metric-value">42</strong>');
  expect(html).toContain('<h2 id="world-event-types"><span class="term term-inline"><span class="term-id term-word">Event Type</span><span class="term-gloss"><span class="term-gloss-text">（事件类型）</span></span></span></h2>');
  expect(html).toContain('<span class="metric-label">Event Type（事件类型）</span><strong class="metric-value">0</strong>');expect(html).toContain('暂无已发布的 Event Type');expect(html).not.toContain('<svg');
  expect(html).not.toMatch(/新建|编辑|删除/);
  // Owner 2026-10-29: the EIOS English terms stay, each with its Chinese in full-width parentheses (it replaced the English-only rule).
  for(const page of [html,render(undefined,'object_types'),render(undefined,'properties'),render(undefined,'links'),render(undefined,'actions'),render(undefined,'functions')]){
    expect(page).toMatch(/<span class="term-gloss-text">（[^）<]+）/);expect(page).not.toMatch(/（）|实例|关系类型/);}
  expect(html).not.toContain('aria-label="查看方式"');
});

test('each concept page: its EIOS title, its own table; Table / Graph only under the Object Type title',()=>{
  const types=render(undefined,'object_types');
  const term=(id:string,gloss:string)=>`<span class="term term-inline"><span class="term-id term-word">${id}</span><span class="term-gloss"><span class="term-gloss-text">（${gloss}）</span></span></span>`;
  expect(types).toContain(`<h1 id="workbench-page-title">${term('Object Type','对象类型')}</h1>`);
  expect(types).toMatch(/<\/header><div class="workbench-data"[^>]*><div class="segmented" role="group" aria-label="查看方式">/);
  expect(types).toContain(`<button type="button" aria-pressed="true">${term('Table','表格')}</button><button type="button" aria-pressed="false">${term('Graph','关系图')}</button></div>`);
  expect(types).toContain('aria-label="Object Type"');expect(types).not.toContain('<svg');
  const props=render(undefined,'properties');
  expect(props).toContain(`<h1 id="workbench-page-title">${term('Property','属性')}</h1>`);expect(props).not.toContain('查看方式');
  expect(props).toContain('spending_power');expect(props).toContain('data-code="withheld"');expect(props).toContain('负责人限制的属性组：只显示名称');
  const actions=render(undefined,'actions');
  expect(actions).toContain('nexloop.service.request');expect(actions).toContain('>有</span>');expect(actions).not.toContain('查看方式');
  expect(render(undefined,'links')).toContain('consumer_conversation');
});

test('the graph view: one focusable button per type, labelled edges, the focused type panel beside it',()=>{
  const html=render({view:'graph',focus:'Consumer'},'object_types');
  expect(html).toContain('<svg');expect(html).toContain('aria-label="Object Type 关系图：2 个 Object Type，1 条 Link"');
  expect(html.match(/role="button" tabindex="0"/g)?.length).toBe(2);
  expect(html).toContain('consumer_conversation 1:N');expect(html).toContain('aria-label="Consumer（客户） 详情"');expect(html).toContain('<li class="panel-link"><span class="panel-link-dir" aria-label="指向">→</span><span class="panel-link-type"><span class="term term-stack" title="Conversation"><code class="term-id">Conversation</code></span></span><span class="panel-link-card">1:N</span><span class="panel-link-name"><span class="term term-stack" title="consumer_conversation"><code class="term-id">consumer_conversation</code></span></span></li>');
  expect(html).toContain('<div class="world-graph-frame"><div class="world-graph world-graph-panel">');
  expect(html).toContain('<button type="button" aria-pressed="true"><span class="term term-inline"><span class="term-id term-word">Graph</span><span class="term-gloss"><span class="term-gloss-text">（关系图）</span></span></span></button>');expect(html).not.toContain('data-empty-edges');
});

test('a relation from a type to itself: both ends on the node border (outside the box) and an arrowhead like any edge',()=>{
  const loop=selfLoop(200,100);
  for(const p of [loop.start,loop.end]){const inside=Math.abs(p.x-200)<NODE_W/2&&Math.abs(p.y-100)<NODE_H/2;expect(inside).toBe(false);expect(p.x).toBeGreaterThanOrEqual(200+NODE_W/2);}
  expect(loop.start.y).toBeLessThan(loop.end.y);expect(loop.d.startsWith(`M${loop.start.x},${loop.start.y} `)&&loop.d.endsWith(` ${loop.end.x},${loop.end.y}`)).toBe(true);
  const html=renderToStaticMarkup(h(SchemaGraph,{nodes:[{id:'Consumer',label:'Consumer'}],edges:[{id:'referred_by',from:'Consumer',to:'Consumer',label:'referred_by N:1'}],
    focus:null,onFocus:()=>{},label:'g'}));
  expect(html).toMatch(/data-self-loop="referred_by"><path d="M[^"]+" fill="none" stroke="currentColor" stroke-width="2" marker-end="url\(#schema-arrow\)"/);
  expect(html).toContain('referred_by N:1');
});


const types=(n:number)=>Array.from({length:n},(_,i)=>({id:`Type${String(i).padStart(2,'0')}`,label:`Type${i}`,sub:'v1 · 3 Property · 0 Object'}));

test('the graph without any Link: a grid filling the canvas (not a ring), and it says so',()=>{
  const nodes=types(20);
  const l=layout(nodes,[],960/560);
  const rows=new Set(l.nodes.map(p=>p.y)),cols=new Set(l.nodes.map(p=>p.x));
  expect(cols.size).toBe(gridColumns(20,960/560));expect(rows.size).toBe(Math.ceil(20/cols.size));
  expect(Math.abs(Math.log((l.width/l.height)/(960/560)))).toBeLessThan(0.35);  // the picture has the canvas's shape
  expect([...cols].sort((a,b)=>a-b)[1]-[...cols].sort((a,b)=>a-b)[0]).toBe(NODE_W+GAP_X);
  const html=renderToStaticMarkup(h(SchemaGraph,{nodes,edges:[],focus:null,onFocus:()=>{},label:'g',emptyEdgesText:'暂无已发布的 Link'}));
  expect(html).toContain('data-layout="grid"');expect(html).toContain('暂无已发布的 Link');expect(html).toContain('data-edges="0"');
  expect(html.match(/role="button" tabindex="0"/g)?.length).toBe(20);  // every node still a keyboard button
  expect(Number(/data-label-px="([\d.]+)"/.exec(html)![1])).toBeGreaterThanOrEqual(MIN_LABEL_PX);
});

test('the graph with Links: fitted to the canvas, node labels never below the floor, edges drawn and visible',()=>{
  for(const n of [3,12,40]){
    const nodes=types(n);const edges=nodes.slice(1).map((x,i)=>({id:'l'+i,from:nodes[Math.floor(i/2)].id,to:x.id,label:'link N:1'}));
    const html=renderToStaticMarkup(h(SchemaGraph,{nodes,edges,focus:null,onFocus:()=>{},label:'g',emptyEdgesText:'暂无已发布的 Link'}));
    expect(html).toContain('data-layout="graph"');expect(html).not.toContain('暂无已发布的 Link');
    expect(html).toContain(`data-edges="${edges.length}"`);expect(html.match(/data-edge="/g)?.length).toBe(edges.length);
    const px=Number(/data-label-px="([\d.]+)"/.exec(html)![1]),scale=Number(/data-scale="([\d.]+)"/.exec(html)![1]);
    expect(px).toBeGreaterThanOrEqual(MIN_LABEL_PX);expect(scale).toBeGreaterThanOrEqual(MIN_SCALE-1e-3);
    expect(html).toContain(`font-size="${LABEL_PX}"`);
    const l=layout(nodes,edges,960/560);expect(scale).toBeCloseTo(fitScale(l.width,l.height,960),3);  // opens fitted
  }
  // Fit: a small picture is enlarged to the canvas, a large one stops at the label floor (and pans instead).
  expect(fitScale(400,200,960)).toBeGreaterThan(1);expect(fitScale(20000,8000,960)).toBe(MIN_SCALE);
});

test('batch 52: the graph view says below 640 px that the table reads better, with a switch; the table view has no hint',()=>{
  const graph=render({view:'graph'},'object_types');
  expect(graph).toMatch(/<p class="note narrow-only" data-hint="narrow-graph">窄屏建议用表格：[^<]*<button type="button" class="link">切换到表格<\/button><\/p>/);
  expect(graph).toContain('<svg');  // the graph is still there
  expect(render(undefined,'object_types')).not.toContain('narrow-graph');
  const css=readFileSync(new URL('../src/styles.css',import.meta.url),'utf8');
  expect(css).toContain('.workbench .narrow-only{display:none}');expect(css).toContain('@media (max-width:639px){.workbench .narrow-only{display:block}}');
});
