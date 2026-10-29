// Owner 2026-10-29: every English identifier of the enterprise world model with its Chinese gloss - Consumer（客户）. Glosses below are
// entries of the manual glossary v1 (world-model-glossary.v1); the API shape (gloss, gloss_source manual|machine|null) is L2's.
import {createElement as h} from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {readFileSync} from 'node:fs';
import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
import {expect,test} from 'vitest';
import {WORLD_SECTION_NAMES,WorkbenchError,worldModel} from '../src/workbench/api';
import {WorldModel} from '../src/workbench/pages/WorldModel';
import {MACHINE_HINT,Term,glossMatches,glossText,sectionText} from '../src/workbench/Gloss';

const example=JSON.parse(readFileSync(new URL('../../../packages/contracts/examples/workbench-world-model-v2.valid.json',import.meta.url),'utf8'));
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

test('v2, strict (L6 S5): both keys on every identifier; null/null none, ""/manual a clear, text with manual or machine; anything else refused',()=>{
  const m=worldModel(glossed);
  expect(m.object_types[0]).toMatchObject({name:'Consumer',gloss:'客户',gloss_source:'manual'});
  expect(m.object_types[0].properties[1]).toEqual({name:'spending_power',withheld:true,gloss:null,gloss_source:null});
  expect(m.relation_types[0]).toMatchObject({gloss:'客户的会话',inverse_gloss:null});expect(m.actions[0]).toMatchObject({gloss:'提交服务请求',gloss_source:'machine'});
  expect(worldModel(example).object_types[1]).toMatchObject({name:'Conversation',gloss:null,gloss_source:null});
  const refused=(change:(x:typeof glossed)=>void)=>{const x=structuredClone(glossed);change(x);expect(()=>worldModel(x)).toThrow(WorkbenchError);};
  refused(x=>{x.object_types[0].gloss_source='guess';});refused(x=>{delete x.object_types[0].gloss;});refused(x=>{delete x.actions[0].gloss_source;});
  refused(x=>{delete x.relation_types[0].inverse_gloss;});refused(x=>{x.object_types[0].gloss_source=null;});  // text without a source
  refused(x=>{x.object_types[0].gloss=null;});  // a source without text
  refused(x=>{x.actions[0].gloss='';});  // "" is only the owner's (manual) clear, never a machine result
  refused(x=>{x.object_types[0].gloss='客'.repeat(25);});refused(x=>{delete x.glossary;});refused(x=>{x.glossary.sections.Scope='权限范围';});
  refused(x=>{x.glossary.extra=1;});refused(x=>{x.object_types[0].properties[1].note='x';});
  refused(x=>{x.relation_types[0].inverse_gloss='反向';x.relation_types[0].inverse_gloss_source='manual';});  // no inverse name, no inverse gloss
  expect(worldModel({...structuredClone(glossed),glossary:{manifest_version:0,sections:{}}}).glossary).toEqual({manifest_version:0,sections:{}});
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
  // S4: section names come from the server only; without them the English stands alone.
  expect(sectionText('Link',example.glossary.sections)).toBe('Link（关系）');expect(sectionText('Link',undefined)).toBe('Link');expect(sectionText('Link',{})).toBe('Link');
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
  const unglossed=structuredClone(example);unglossed.object_types[0].gloss=null;unglossed.object_types[0].gloss_source=null;
  const plain=render('object_types',{view:'graph'},unglossed);expect(plain).not.toContain('schema-node-gloss');expect(plain).toContain('v3 · 2 Property');
  expect(plain).not.toMatch(/（）/);
  // The Object page's type picker (plain text in an option): the gloss, then the count.
  expect(render('objects')).toContain('>Consumer（客户） · 12 个</option>');
});

test('search finds an identifier by its English or its Chinese; each list has its search box',()=>{
  expect(glossMatches('客户','Consumer','客户')).toBe(true);expect(glossMatches('consum','Consumer','客户')).toBe(true);
  expect(glossMatches('会话','Consumer','客户')).toBe(false);expect(glossMatches('','Consumer',null)).toBe(true);expect(glossMatches('客户','Consumer',null)).toBe(false);
  for(const s of ['object_types','properties','links','actions'])expect(render(s)).toContain('（英文或中文）</span><input type="search"');
});

// L2 shape (docs/design/world-model-glossary.md §4): a top-level glossary block; "" with manual is the owner's deliberate clear.
test('L2 shape: the glossary block gives the section names; a manual "" is a clear (English only), a machine "" is nothing',()=>{
  const v2={...structuredClone(glossed),glossary:{manifest_version:1,sections:{'Object Type':'对象类别',Property:'属性',Table:''} as Record<string,string>}};
  v2.object_types[1].gloss='';v2.object_types[1].gloss_source='manual';
  const m=worldModel(v2);
  expect(m.glossary).toEqual({manifest_version:1,sections:{'Object Type':'对象类别',Property:'属性'}});
  expect(m.object_types[1]).toMatchObject({gloss:null,gloss_source:'manual'});
  const html=render('object_types',undefined,v2);
  expect(html).toContain('<span class="term-id term-word">Object Type</span><span class="term-gloss">（对象类别）</span>');  // the server's name wins
  expect(html).toContain('<code class="term-id">Conversation</code></span></button>');  // cleared: English only, no parentheses
  expect(html).toContain('<span class="term"><span class="term-id term-word">Table</span></span>');  // a cleared section name likewise
  expect(html).toContain('<th scope="col" class="cell-main">Object Type（对象类别）</th>');  // headers from the server too
  const bad=structuredClone(v2);bad.glossary.manifest_version=-1;expect(()=>worldModel(bad)).toThrow(WorkbenchError);
});

// L6 S7/S8: the frontend follows L2's v2 contract itself (packages/contracts/workbench-world-model-v2.schema.json and its example).
const contract=JSON.parse(readFileSync(new URL('../../../packages/contracts/workbench-world-model-v2.schema.json',import.meta.url),'utf8'));
test('S7: WORLD_SECTION_NAMES is the v2 schema\'s glossary.sections enum, word for word and in order; the length limit is the same',()=>{
  const sections=contract.properties.glossary.properties.sections;
  expect([...WORLD_SECTION_NAMES]).toEqual(sections.propertyNames.enum);
  expect(sections.additionalProperties.maxLength).toBe(24);
});

test('S8: the v2 contract example parses as it is - every identifier, the inverse names and all 14 section names',()=>{
  const m=worldModel(example);
  expect(Object.keys(m.glossary.sections).sort()).toEqual([...WORLD_SECTION_NAMES].sort());
  expect(m.glossary).toEqual({manifest_version:example.glossary.manifest_version,sections:example.glossary.sections});
  const pairs=(xs:{gloss:string|null;gloss_source:string|null}[])=>xs.map(x=>[x.gloss,x.gloss_source]);
  expect(pairs(m.object_types)).toEqual(pairs(example.object_types));
  expect(pairs(m.object_types.flatMap(t=>t.properties))).toEqual(pairs(example.object_types.flatMap((t:{properties:unknown[]})=>t.properties)));
  expect(pairs(m.actions)).toEqual(pairs(example.actions));expect(pairs(m.functions)).toEqual(pairs(example.functions));
  expect(pairs(m.event_types)).toEqual(pairs(example.event_types));
  expect(m.relation_types.map(r=>[r.gloss,r.gloss_source,r.inverse_gloss,r.inverse_gloss_source]))
    .toEqual(example.relation_types.map((r:Record<string,unknown>)=>[r.gloss,r.gloss_source,r.inverse_gloss,r.inverse_gloss_source]));
  // Each example field the contract adds is required by the frontend too: dropping one is refused.
  for(const drop of [(x:typeof example)=>{delete x.glossary;},(x:typeof example)=>{delete x.object_types[0].gloss_source;},
    (x:typeof example)=>{delete x.relation_types[0].inverse_gloss;},(x:typeof example)=>{x.glossary.sections.Scope='权限范围';}]){
    const x=structuredClone(example);drop(x);expect(()=>worldModel(x)).toThrow(WorkbenchError);}
});
