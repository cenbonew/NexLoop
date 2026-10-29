// @vitest-environment jsdom
// UX audit S2 (docs/design/workbench-ux-audit.md B7, B14, C9): the tab title names the page (WCAG 2.4.2), an in-app navigation moves
// focus to the new page's title (2.4.3), a skip link leads past the sidebar (2.4.1), the narrow-screen menu takes focus and closes
// with Escape, and a disabled primary button stays readable. The workbench is mounted for real against a stubbed API.
import {readFileSync} from 'node:fs';
import {join} from 'node:path';
import {createElement as h} from 'react';
import {act} from 'react';
import {createRoot,type Root} from 'react-dom/client';
import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
import {afterEach,beforeEach,expect,test,vi} from 'vitest';
import {WorkbenchApp,pageTitle} from '../src/workbench/WorkbenchApp';

(globalThis as {IS_REACT_ACT_ENVIRONMENT?:boolean}).IS_REACT_ACT_ENVIRONMENT=true;
const json=(status:number,body:unknown)=>new Response(JSON.stringify(body),{status,headers:{'Content-Type':'application/json'}});
const hex='a'.repeat(64);

test('the tab title: page, then the detail it shows, then the application; signed out it is the sign-in',()=>{
  expect(pageTitle({page:'overview'})).toBe('总览 · 负责人工作台 · NexLoop');
  expect(pageTitle({page:'ontology',section:'object_types'})).toBe('Object Type · 企业世界模型 · 负责人工作台 · NexLoop');
  expect(pageTitle({page:'ontology',section:'object_types'},null,{'Object Type':'对象类型'})).toBe('Object Type（对象类型） · 企业世界模型 · 负责人工作台 · NexLoop');
  expect(pageTitle({page:'consumers',id:hex,sub:'conversation'},{consumerId:hex,label:'客户 甲'})).toBe('会话 · 客户 甲 · 客户 · 负责人工作台 · NexLoop');
  expect(pageTitle({page:'commitments',id:hex})).toBe('承诺详情 · 承诺 / 交付 · 负责人工作台 · NexLoop');
  expect(pageTitle(null)).toBe('登录 · 负责人工作台 · NexLoop');
});

let root:Root;let box:HTMLDivElement;
beforeEach(()=>{
  window.history.replaceState(null,'','/workbench/overview');
  vi.stubGlobal('fetch',vi.fn(async(path:string)=>path==='/api/v1/workbench/auth/session'
    ?json(200,{authenticated:true,tenant_id:'synthetic-tenant',principal_id:'synthetic-owner',restricted:false}):json(503,{})));
  box=document.createElement('div');document.body.append(box);root=createRoot(box);
});
afterEach(()=>{act(()=>root.unmount());box.remove();vi.unstubAllGlobals();});
const settle=async()=>{for(let i=0;i<8;i++)await act(async()=>{await new Promise(r=>setTimeout(r,20));});};
async function mount(){act(()=>root.render(h(QueryClientProvider,{client:new QueryClient({defaultOptions:{queries:{retry:false}}})},h(WorkbenchApp,{slots:{}}))));await settle();}

test('signed in: the title names the page, the skip link comes first and targets the content',async()=>{
  await mount();
  expect(document.title).toBe('总览 · 负责人工作台 · NexLoop');
  const first=box.querySelector('a,button,[tabindex]:not([tabindex="-1"])') as HTMLAnchorElement;
  expect(first.textContent).toBe('跳到主要内容');expect(first.getAttribute('href')).toBe('#workbench-content');
  const main=box.querySelector('main#workbench-content');
  expect(main?.getAttribute('tabindex')).toBe('-1');
});

test('a sidebar navigation changes the title and moves focus to the new page title',async()=>{
  await mount();
  const link=[...box.querySelectorAll<HTMLAnchorElement>('#workbench-nav a')].find(a=>a.textContent==='联系限制')!;
  link.focus();
  await act(async()=>{link.click();});await settle();
  expect(window.location.pathname).toBe('/workbench/contact');
  expect(document.title).toBe('联系限制 · 负责人工作台 · NexLoop');
  expect(document.activeElement?.tagName).toBe('H1');
  expect(document.activeElement?.textContent).toBe('联系限制');
  expect(document.activeElement?.getAttribute('tabindex')).toBe('-1');
});

test('the narrow-screen menu: opening focuses its first link, Escape closes it and returns to the button',async()=>{
  await mount();
  const toggle=box.querySelector<HTMLButtonElement>('.workbench-menu-toggle')!;
  await act(async()=>{toggle.click();});await settle();
  expect(toggle.getAttribute('aria-expanded')).toBe('true');
  expect(document.activeElement).toBe(box.querySelector('#workbench-nav a'));
  await act(async()=>{document.activeElement!.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}));});
  expect(toggle.getAttribute('aria-expanded')).toBe('false');
  expect(box.querySelector('#workbench-nav')?.className).not.toContain('open');
  expect(document.activeElement).toBe(toggle);
});

test('a disabled primary or danger button is neutral and opaque, not a faded colour; the time zone note moves to the footer on narrow screens',()=>{
  const css=readFileSync(join(import.meta.dirname,'../src/styles.css'),'utf8');
  const rule=/button\[type=submit\]:disabled[^{]*button\.btn-danger:disabled[^{]*\{([^}]*)\}/.exec(css);
  expect(rule?.[1]).toContain('background:var(--neutral-soft)');expect(rule?.[1]).toContain('color:var(--neutral-text)');expect(rule?.[1]).toContain('opacity:1');
  expect(css).toMatch(/@media \(max-width:1023px\)\{[^@]*\.workbench \.brand-tz\{display:none\}\.workbench \.footer-tz\{display:inline\}/);
  expect(css).toMatch(/\.skip-link:focus\{top:/);
});

test('the skip link moves focus to the page title without touching the address (no hash, no history entry)',async()=>{
  await mount();
  const before=window.history.length;
  const skip=box.querySelector<HTMLAnchorElement>('.skip-link')!;
  await act(async()=>{skip.click();});
  expect(document.activeElement?.tagName).toBe('H1');
  expect(window.location.hash).toBe('');expect(window.history.length).toBe(before);
});
