// @vitest-environment jsdom
// Owner 2026-10-30 (stage walkthrough of release 54): one layout per place. In a table cell or a panel every term is two lines - the
// identifier, then the gloss left-aligned below it, never broken inside (nowrap, an ellipsis with the full text in its title); in a
// title or a switch English（中文） stays on one line with the gloss moving as a whole. No cell centres a wrapped line.
import {createElement as h,act} from 'react';
import {createRoot,type Root} from 'react-dom/client';
import {readFileSync} from 'node:fs';
import {resolve} from 'node:path';
import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
import {afterEach,beforeEach,expect,test} from 'vitest';
import {worldModel} from '../src/workbench/api';
import {WorldModel} from '../src/workbench/pages/WorldModel';

(globalThis as {IS_REACT_ACT_ENVIRONMENT?:boolean}).IS_REACT_ACT_ENVIRONMENT=true;
const example=JSON.parse(readFileSync(resolve(__dirname,'../../../packages/contracts/examples/workbench-world-model-v2.valid.json'),'utf8'));
const css=readFileSync(resolve(__dirname,'../src/styles.css'),'utf8');
let root:Root;let box:HTMLDivElement;let style:HTMLStyleElement;
beforeEach(()=>{style=document.createElement('style');style.textContent=css;document.head.append(style);
  box=document.createElement('div');box.className='workbench';document.body.append(box);root=createRoot(box);});
afterEach(()=>{act(()=>root.unmount());box.remove();style.remove();});
const mount=(section:string,query?:Record<string,string>)=>{const c=new QueryClient({defaultOptions:{queries:{retry:false,staleTime:Infinity}}});
  c.setQueryData(['workbench','world-model'],worldModel(example));
  act(()=>root.render(h(QueryClientProvider,{client:c},h(WorldModel,{section:section as never,query,go:()=>{}}))));};

test.each(['object_types','properties','links','actions'])('%s: every glossed term in a cell is two lines, the gloss nowrap with its title, all left-aligned',section=>{
  mount(section);
  const terms=[...box.querySelectorAll<HTMLElement>('td .term')];expect(terms.length).toBeGreaterThan(0);
  const glossed=terms.filter(t=>t.querySelector('.term-gloss'));expect(glossed.length).toBeGreaterThan(0);
  for(const t of terms){
    expect(t.classList.contains('term-stack')).toBe(true);expect(t.firstElementChild!.classList.contains('term-id')).toBe(true);
    const s=getComputedStyle(t);expect(s.display).toBe('inline-flex');expect(s.flexDirection).toBe('column');expect(s.alignItems).toBe('flex-start');}
  for(const t of glossed){
    const g=t.querySelector<HTMLElement>(':scope>.term-gloss')!;expect(t.children.length).toBe(2);  // line 1 the identifier, line 2 the gloss
    expect(getComputedStyle(g).whiteSpace).toBe('nowrap');
    const text=g.querySelector<HTMLElement>('.term-gloss-text')!;expect(getComputedStyle(text).textOverflow).toBe('ellipsis');
    expect(`（${g.title}）`).toBe(text.textContent);}  // the full text on hover when cut
  // L6 I15: the identifier is never cut (consumer_conversation_a vs _b): it may wrap, and the whole term carries id（gloss） as its title.
  for(const t of terms){const id=t.querySelector<HTMLElement>(':scope>.term-id')!,s=getComputedStyle(id);
    expect(s.textOverflow).not.toBe('ellipsis');expect(s.overflow).not.toBe('hidden');expect(s.whiteSpace).toBe('normal');expect(s.overflowWrap).toBe('anywhere');
    const g=t.querySelector('.term-gloss')?.getAttribute('title');expect(t.title).toBe(g?`${id.textContent}（${g}）`:id.textContent);}
  for(const cell of box.querySelectorAll<HTMLElement>('td'))expect(getComputedStyle(cell).textAlign).not.toBe('center');
  for(const b of box.querySelectorAll<HTMLElement>('td button.link')){
    const s=getComputedStyle(b);expect(s.justifyContent).toBe('flex-start');expect(s.textAlign).toBe('left');}
});

test('titles, the Table / Graph switch and the detail panel title keep English（中文） on one line, the gloss unbroken',()=>{
  mount('object_types',{view:'graph',focus:'Consumer'});
  const inline=[box.querySelector('h1 .term')!,...box.querySelectorAll('.segmented .term'),box.querySelector('.world-panel h2 .term')!];
  for(const t of inline){
    expect(t.classList.contains('term-inline')).toBe(true);
    const g=t.querySelector<HTMLElement>('.term-gloss')!;expect(getComputedStyle(g).whiteSpace).toBe('nowrap');expect(getComputedStyle(g).display).toBe('inline-block');}
  expect(box.querySelector('h1')!.textContent).toBe('Object Type（对象类型）');
  // In the panel's lists (narrow) the terms are stacked like a cell.
  for(const t of box.querySelectorAll('.world-panel li .term'))expect(t.classList.contains('term-stack')).toBe(true);
});
