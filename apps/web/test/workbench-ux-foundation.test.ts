// UX audit S1 (docs/design/workbench-ux-audit.md B1-B4, B6): token contrast in both themes, the notice component, the danger tone
// and the reason a submit button is disabled; the semantic classes pages use all have a style.
import {readFileSync} from 'node:fs';
import {createElement as h,type ReactNode} from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
import {expect,test} from 'vitest';
import {OPERATION_ACTION,me} from '../src/workbench/api';
import {Form} from '../src/workbench/actions';
import {Notice} from '../src/workbench/kit';

const css=readFileSync(new URL('../src/styles.css',import.meta.url),'utf8');
function tokens(block:string):Record<string,string>{return Object.fromEntries([...block.matchAll(/--([a-z0-9-]+):(#[0-9a-f]{6})/gi)].map(m=>[m[1],m[2].toLowerCase()]));}
const light=tokens(css.slice(css.indexOf(':root{'),css.indexOf('@media (prefers-color-scheme:dark)')));
const darkStart=css.indexOf('@media (prefers-color-scheme:dark){:root{');
const dark={...light,...tokens(css.slice(darkStart,css.indexOf('}}',darkStart)))};
const lum=(hex:string)=>{const n=parseInt(hex.slice(1),16);return [(n>>16)&255,(n>>8)&255,n&255].map(v=>{v/=255;return v<=.03928?v/12.92:((v+.055)/1.055)**2.4;})
  .reduce((a,v,i)=>a+v*[.2126,.7152,.0722][i],0);};
const ratio=(a:string,b:string)=>{const x=lum(a),y=lum(b);return (Math.max(x,y)+.05)/(Math.min(x,y)+.05);};

test.each([['light',light],['dark',dark]] as const)('%s theme: focus ring and control edges reach 3:1, button text 4.5:1 (WCAG 1.4.11, 1.4.3)',(_,t)=>{
  for(const bg of ['surface','bg','surface-2'])expect(ratio(t.focus,t[bg]),`focus on ${bg}`).toBeGreaterThanOrEqual(3);
  for(const bg of ['surface','input-bg','surface-2'])expect(ratio(t['control-border'],t[bg]),`control edge on ${bg}`).toBeGreaterThanOrEqual(3);
  expect(ratio('#ffffff',t['danger-bg'])).toBeGreaterThanOrEqual(4.5);
  expect(ratio('#ffffff',t['danger-bg-hover'])).toBeGreaterThanOrEqual(4.5);
  expect(ratio(t['primary-text'],t['primary-bg'])).toBeGreaterThanOrEqual(4.5);
});

test('text inputs, selects and text areas use the control edge, not the card border',()=>{
  const rule=css.slice(css.indexOf('/* ==== UX audit S1'));
  expect(rule).toMatch(/input:not\(\[type=radio\]\):not\(\[type=checkbox\]\),select,textarea[^{]*\{border-color:var\(--control-border\)\}/);
});

test('every semantic class a page relies on has a style (they were plain text before)',()=>{
  for(const c of ['notice','notice-warning','notice-danger','impact-summary','one-time-secret','workbench-summary','refusal-reason','banner','disabled-reason','btn-danger','btn-secondary'])
    expect(css,`.${c}`).toMatch(new RegExp(`\\.${c}[\\s,{:>.]`));
  expect(css).toMatch(/\.workbench p\.warning/);expect(css).toMatch(/\.workbench dl\.kv\{display:grid/);
});

test('a notice: words with a decorative mark; a note when standing, an alert only for a live warning or danger',()=>{
  const standing=renderToStaticMarkup(h(Notice,{tone:'warning'},'已暂停主动联系'));
  expect(standing).toContain('role="note"');expect(standing).toContain('notice-warning');expect(standing).toContain('aria-hidden="true"');
  expect(renderToStaticMarkup(h(Notice,{tone:'danger',live:true},'x'))).toContain('role="alert"');
  expect(renderToStaticMarkup(h(Notice,{tone:'info',live:true},'x'))).toContain('role="status"');
  expect(renderToStaticMarkup(h(Notice,{title:'影响'},'只改这一条'))).toContain('<strong>影响</strong>');
});

const wrap=(node:ReactNode)=>renderToStaticMarkup(h(QueryClientProvider,{client:new QueryClient()},node));
const owner=me({principal_id:'synthetic-owner',role:'owner',actions:[OPERATION_ACTION.revoke_workbench_invitation,OPERATION_ACTION.decide_approval]});

test('a danger form: its submit carries the danger tone; an ordinary form stays primary',()=>{
  const danger=wrap(h(Form,{operation:'revoke_workbench_invitation',member:owner,title:'撤销邀请',tone:'danger',body:()=>({})},h('span')));
  expect(danger).toMatch(/<button type="submit" class="btn-danger"[^>]*>撤销邀请<\/button>/);
  const plain=wrap(h(Form,{operation:'decide_approval',member:owner,title:'批准',body:()=>({})},h('span')));
  expect(plain).toMatch(/<button type="submit">批准<\/button>/);
});

test('a disabled submit says why, tied to the button; an enabled one says nothing',()=>{
  const html=wrap(h(Form,{operation:'decide_approval',member:owner,title:'批准',disabled:true,disabledReason:'填写原因后可批准或拒绝。',body:()=>null},h('span')));
  const described=/<button type="submit" disabled="" aria-describedby="([^"]+)">/.exec(html);
  expect(described).not.toBeNull();
  expect(html).toContain(`<p class="disabled-reason" id="${described![1]}">填写原因后可批准或拒绝。</p>`);
  const enabled=wrap(h(Form,{operation:'decide_approval',member:owner,title:'批准',disabledReason:'填写原因后可批准或拒绝。',body:()=>({})},h('span')));
  expect(enabled).not.toContain('disabled-reason');expect(enabled).not.toContain('aria-describedby');
});
