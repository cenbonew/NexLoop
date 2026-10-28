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

// Batch 52: hand-made shortening outside <code> too - an ID cut with slice(0,n) plus '…', or shorten()/shortId() in text (strings,
// templates). Text excerpts (a message body, a quote, a graph label) are cut on purpose and listed with the reason;
// an <option> is a control of its own (the select is the tab stop) and the copy-id helper carries its own button.
const EXCERPTS:Record<string,string>={
  'pages/Consumers.tsx':'a message body excerpt with 展开全文',
  'pages/Commitments.tsx':'a quoted commitment text',
  'graph/SchemaGraph.tsx':'a node label (the full name is in the node title and the table view)',
  'ui.tsx':'CopyId: the head…tail sits next to its own copy button',
  'kit.tsx':'IdText itself',
};
/** A landmark's accessible name (aria-label) and a graph node's label name a thing whose full ID is shown with IdText nearby. */
const NAMING=/aria-label=\{|\blabel:`/;
test('no hand-made shortened ID in text: slice(0,n)+… and shorten() appear only inside IdText, a control, an option or an excerpt',()=>{
  const all=[...files,join(SRC,'graph/SchemaGraph.tsx')];const offenders:string[]=[];let seen=0;
  for(const file of all){const rel=file.split('/workbench/')[1];const text=readFileSync(file,'utf8');
    for(const m of text.matchAll(/\.slice\(0, ?\d+\)\s*(?:\+\s*'…'|\}…|…)|\b(?:shorten|shortId|shortenId)\(/g)){
      const at=m.index!;const line=text.slice(text.lastIndexOf('\n',at)+1,text.indexOf('\n',at));
      if(/^\s*(?:export )?(?:const|function) (?:shorten|shortId)\b/.test(line))continue;  // the definition
      seen++;
      const inOption=text.slice(0,at).lastIndexOf('<option')>text.slice(0,at).lastIndexOf('</option>');
      if(insideControl(text,at)||inOption||EXCERPTS[rel]||NAMING.test(line))continue;
      offenders.push(`${rel}:${text.slice(0,at).split('\n').length}`);}}
  expect(offenders).toEqual([]);expect(seen).toBeGreaterThan(0);
});

test('IdText: a shortened ID carries a copy button whose title is the full value; the code itself is not a tab stop',()=>{
  const hex='a'.repeat(64);
  const html=renderToStaticMarkup(h(IdText,{value:hex,kind:'客户'}));
  expect(html).toContain(`title="复制完整 ID：${hex}"`);expect(html).toContain('class="id-copy"');expect(html).not.toMatch(/<code[^>]*tabindex/);
});
