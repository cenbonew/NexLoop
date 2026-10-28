// L6 S1 on S8: a shortened ID is no longer a tab stop, so its copy button is how a keyboard or screen reader user reaches the full value.
// Every place in the workbench that shows an ID shortened (the kit's IdText, or a hand-made head…tail) either carries the copy button or
// sits inside a link or button (which is then the tab stop). The source is walked page by page; IdText itself is rendered.
import {readFileSync,readdirSync} from 'node:fs';
import {join} from 'node:path';
import {createElement as h} from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {expect,test} from 'vitest';
import {IdText} from '../src/workbench/kit';

const SRC=join(import.meta.dirname,'../src/workbench');
const files=[...readdirSync(SRC).filter(f=>f.endsWith('.tsx')).map(f=>join(SRC,f)),...readdirSync(join(SRC,'pages')).filter(f=>f.endsWith('.tsx')).map(f=>join(SRC,'pages',f))];
/** Whether the JSX of the file before `at` has an open <a or <button that is not yet closed (tags span lines). */
function insideControl(text:string,at:number):boolean{
  const before=text.slice(0,at);
  for(const tag of ['a','button']){const open=[...before.matchAll(new RegExp(`<${tag}[\\s>]`,'g'))].length,close=[...before.matchAll(new RegExp(`</${tag}>`,'g'))].length;if(open>close)return true;}
  return false;
}

test('every IdText without its copy button sits inside a link or button',()=>{
  const offenders:string[]=[];let checked=0;
  for(const file of files){const text=readFileSync(file,'utf8');
    for(const m of text.matchAll(/<IdText\b[^>]*copy=\{false\}[^>]*\/>/g)){checked++;if(!insideControl(text,m.index!))offenders.push(`${file.split('/workbench/')[1]}:${text.slice(0,m.index).split('\n').length}`);}}
  expect(offenders).toEqual([]);expect(checked).toBeGreaterThan(0);
});

test('no hand-made shortened ID outside a link or button (they use IdText, which brings the copy button)',()=>{
  const offenders:string[]=[];
  for(const file of files){const text=readFileSync(file,'utf8');
    for(const m of text.matchAll(/<code[^>]*>\{(shortId|shortenId|shorten)\(/g))if(!insideControl(text,m.index!))offenders.push(`${file.split('/workbench/')[1]}:${text.slice(0,m.index).split('\n').length}`);}
  expect(offenders).toEqual([]);
});

test('IdText: a shortened ID carries a copy button whose title is the full value; the code itself is not a tab stop',()=>{
  const hex='a'.repeat(64);
  const html=renderToStaticMarkup(h(IdText,{value:hex,kind:'客户'}));
  expect(html).toContain(`title="复制完整 ID：${hex}"`);expect(html).toContain('class="id-copy"');expect(html).not.toMatch(/<code[^>]*tabindex/);
});
