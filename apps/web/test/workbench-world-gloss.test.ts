// Owner 2026-10-29: every English identifier of the enterprise world model with its Chinese gloss - Consumer（客户）. Glosses below are
// entries of the manual glossary v1 (world-model-glossary.v1); the API shape (gloss, gloss_source manual|machine|null) is L2's.
import {createElement as h} from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {readFileSync} from 'node:fs';
import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
import {expect,test} from 'vitest';
import {WorkbenchError,worldModel} from '../src/workbench/api';
import {WorldModel} from '../src/workbench/pages/WorldModel';
import {MACHINE_HINT,SECTION_GLOSS,Term,glossMatches,glossText} from '../src/workbench/Gloss';

const example=JSON.parse(readFileSync(new URL('../../../packages/contracts/examples/workbench-world-model.valid.json',import.meta.url),'utf8'));
const css=readFileSync(new URL('../src/styles.css',import.meta.url),'utf8');
// Glossary v1 entries for the contract example's identifiers; spending_power (withheld) has none; the action is a machine gloss here.
const glossed=structuredClone(example);
glossed.object_types[0].gloss='客户';glossed.object_types[0].gloss_source='manual';
glossed.object_types[1].gloss='会话';glossed.object_types[1].gloss_source='manual';
glossed.object_types[0].properties[0].gloss='显示名';glossed.object_types[0].properties[0].gloss_source='manual';
glossed.object_types[0].properties[1].gloss=null;glossed.object_types[0].properties[1].gloss_source=null;
glossed.relation_types[0].gloss='客户的会话';glossed.relation_types[0].gloss_source='manual';
glossed.actions[0].gloss='提交服务请求';glossed.actions[0].gloss_source='machine';
const render=(section?:string,query?:Record<string,string>,body=glossed)=>{const c=new QueryClient({defaultOptions:{queries:{retry:false,staleTime:Infinity}}});
  c.setQueryData(['workbench','world-model'],worldModel(body));
  return renderToStaticMarkup(h(QueryClientProvider,{client:c},h(WorldModel,{section:section as never,query,go:()=>{}})));};
const term=(id:string,gloss:string)=>`<code class="term-id">${id}</code><span class="term-gloss">（${gloss}）`;

test('the parser reads gloss and gloss_source on every identifier; absent, null or empty is none; an unknown source is refused',()=>{
  const m=worldModel(glossed);
  expect(m.object_types[0]).toMatchObject({name:'Consumer',gloss:'客户',gloss_source:'manual'});
  expect(m.object_types[0].properties[1]).toEqual({name:'spending_power',withheld:true,gloss:null,gloss_source:null});
  expect(m.relation_types[0]).toMatchObject({gloss:'客户的会话',inverse_gloss:null});expect(m.actions[0]).toMatchObject({gloss:'提交服务请求',gloss_source:'machine'});
  expect(worldModel(example).object_types[0]).toMatchObject({gloss:null,gloss_source:null});
  const blank=structuredClone(glossed);blank.object_types[0].gloss='  ';expect(worldModel(blank).object_types[0]).toMatchObject({gloss:null,gloss_source:null});
  const bad=structuredClone(glossed);bad.object_types[0].gloss_source='guess';expect(()=>worldModel(bad)).toThrow(WorkbenchError);
  const leaky=structuredClone(glossed);leaky.object_types[0].properties[1].value_type='string';expect(()=>worldModel(leaky)).toThrow(WorkbenchError);
});

test('Term: the identifier in code, the full-width gloss as secondary text, no empty parentheses, a 机翻 mark for a machine gloss',()=>{
  expect(renderToStaticMarkup(h(Term,{id:'Consumer',gloss:'客户'}))).toBe(`<span class="term">${term('Consumer','客户')}</span></span>`);
  expect(renderToStaticMarkup(h(Term,{id:'spending_power',gloss:null}))).toBe('<span class="term"><code class="term-id">spending_power</code></span>');
  expect(renderToStaticMarkup(h(Term,{id:'x',gloss:''}))).not.toContain('（');
  expect(renderToStaticMarkup(h(Term,{id:'nexloop.budget.loosen',gloss:'放宽预算',source:'machine'})))
    .toContain(`（放宽预算）<small class="term-machine" title="${MACHINE_HINT}">机翻</small>`);
  expect(MACHINE_HINT).toBe('由模型翻译，负责人可改正');
  expect(glossText('Consumer','客户')).toBe('Consumer（客户）');expect(glossText('Consumer',null)).toBe('Consumer');
  expect(SECTION_GLOSS).toMatchObject({'Object Type':'对象类型',Property:'属性',Link:'关系',Function:'函数',Action:'动作','Object Set':'对象集','Event Type':'事件类型',Table:'表格',Graph:'关系图'});
});

test('typography: the identifier never breaks, the gloss is secondary and may wrap, the 机翻 mark is at least 12px',()=>{
  expect(css).toMatch(/\.term-id\{white-space:nowrap\}/);
  expect(css).toMatch(/\.term-gloss\{[^}]*color:var\(--muted\)[^}]*overflow-wrap:anywhere/);
  expect(css).toMatch(/\.term-gloss\{font-family:inherit;font-weight:400/);
  expect(css).toContain('[aria-pressed="true"] .term-gloss,[aria-current="page"] .term-gloss{color:inherit');  // readable on a selected control
  const mark=/\.term-machine\{([^}]*)\}/.exec(css)![1];expect(Number(/font-size:(\d+)px/.exec(mark)![1])).toBeGreaterThanOrEqual(12);
});

test('every list, the type graph and its panel show the glosses; a missing one shows the identifier alone',()=>{
  const types=render('object_types');expect(types).toContain(term('Consumer','客户'));expect(types).toContain(term('display_name','显示名'));
  expect(types).toContain('aria-label="查看 Consumer（客户） 的 12 个 Object"');
  const props=render('properties');expect(props).toContain(term('display_name','显示名'));expect(props).toContain('<code class="term-id">spending_power</code></span>');
  expect(props).not.toContain('（）');
  const links=render('links');expect(links).toContain(term('consumer_conversation','客户的会话'));expect(links).toContain(term('Conversation','会话'));
  const actions=render('actions');expect(actions).toContain(`${term('nexloop.service.request','提交服务请求')}<small class="term-machine"`);
  const graph=render('object_types',{view:'graph',focus:'Consumer'});
  expect(graph).toContain('class="schema-node-gloss">（客户）</text>');expect(graph).toContain('aria-label="Consumer（客户），v3 · 2 Property · 12 Object，已选中"');
  expect(graph).toContain('<title>Consumer（客户） · v3 · 2 Property · 12 Object</title>');expect(graph).toContain('consumer_conversation（客户的会话） 1:N');
  expect(graph).toContain('aria-label="Consumer（客户） 详情"');expect(graph).toContain(`<h2><span class="term">${term('Consumer','客户')}</span></span></h2>`);
  // Without glosses the node keeps its details line and nothing shows empty parentheses.
  const plain=render('object_types',{view:'graph'},example);expect(plain).not.toContain('schema-node-gloss');expect(plain).toContain('v3 · 2 Property');
  expect(plain).not.toMatch(/（）/);
  // The Object page's type picker (plain text in an option): the gloss, then the count.
  expect(render('objects')).toContain('>Consumer（客户） · 12 个</option>');
});

test('search finds an identifier by its English or its Chinese; each list has its search box',()=>{
  expect(glossMatches('客户','Consumer','客户')).toBe(true);expect(glossMatches('consum','Consumer','客户')).toBe(true);
  expect(glossMatches('会话','Consumer','客户')).toBe(false);expect(glossMatches('','Consumer',null)).toBe(true);expect(glossMatches('客户','Consumer',null)).toBe(false);
  for(const s of ['object_types','properties','links','actions'])expect(render(s)).toContain('（英文或中文）</span><input type="search"');
});
