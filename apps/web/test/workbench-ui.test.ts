import {createElement as h} from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {expect,test} from 'vitest';
import {Announce,Empty,StatusBadge,Tabs,commitmentTone,intentTone,money,relative} from '../src/workbench/ui';
import {dispatchText} from '../src/workbench/pages/Actions';
import {crumbs,NAV_CHILDREN,NAV_GROUPS} from '../src/workbench/WorkbenchApp';
import {PAGES,routePath} from '../src/workbench/route';

test('money reads with two decimals; the stored precision stays available', ()=>{
  expect(money('12.34567891')).toBe('12.35');expect(money(100)).toBe('100.00');expect(money('n/a')).toBe('n/a');
});
test('relative time before and after now', ()=>{
  const now=Date.parse('2026-10-18T12:00:00Z');
  expect(relative('2026-10-18T11:59:30Z',now)).toBe('刚刚');expect(relative('2026-10-18 11:00:00+00',now)).toBe('1 小时前');
  expect(relative('2026-10-20T12:00:00Z',now)).toBe('2 天后');
});
test('a status badge has words and a symbol; the technical code only in the title', ()=>{
  const html=renderToStaticMarkup(h(StatusBadge,{text:'渠道已完成',tone:intentTone('fulfilled'),code:'fulfilled'}));
  expect(html).toContain('tone-ok');expect(html).toContain('title="fulfilled"');expect(html).toContain('✓');
  expect(html.replace(/title="[^"]*"|data-code="[^"]*"/g,'')).not.toContain('fulfilled');
  expect([intentTone('failed'),intentTone('unknown'),commitmentTone('breached'),commitmentTone('cancelled')]).toEqual(['fail','pending','fail','neutral']);
});
test('a finished intent needs no further dispatch; a failed one is not applicable', ()=>{
  const blocked={status:'ok' as const,prediction:{dispatchable:false,reason:'not_queued'}};
  expect(dispatchText(blocked,'fulfilled')).toBe('无需再次派发');expect(dispatchText(blocked,'confirmed')).toBe('无需再次派发');
  expect(dispatchText(blocked,'failed')).toContain('不适用');expect(dispatchText(blocked,'unknown')).toContain('不在派发队列');
});
test('each empty kind says what it is and what next', ()=>{
  for(const kind of ['none','filtered','not_enabled','forbidden','unavailable'] as const){
    const html=renderToStaticMarkup(h(Empty,{kind}));expect(html).toContain(`data-empty="${kind}"`);expect(html).toMatch(/empty-next">[^<]+</);}
});
test('tabs: one tab in the tab order, the selected one', ()=>{
  const html=renderToStaticMarkup(h(Tabs,{label:'范围',value:'b',onChange:()=>{},tabs:[{key:'a',label:'甲'},{key:'b',label:'乙'}]}));
  expect(html).toContain('aria-selected="false" tabindex="-1"');expect(html).toContain('aria-selected="true" tabindex="0"');
});
test('navigation: four groups, every page once, the next-stage page last; detail pages have breadcrumbs', ()=>{
  expect(NAV_GROUPS.map(g=>g.title)).toEqual(['运营','模型与资产','监控','管理']);
  const all=NAV_GROUPS.flatMap(g=>g.pages);expect([...all].sort()).toEqual(Object.keys(PAGES).sort());expect(all.slice(-1)).toEqual(['experiments']);expect(NAV_GROUPS[1].pages).toEqual(['ontology','data_assets','knowledge']);expect(NAV_GROUPS[3].pages).toEqual(['settings','personas','capabilities','experiments']);
  expect(crumbs({page:'consumers'})).toEqual([]);
  expect(crumbs({page:'consumers',id:'x',sub:'conversation'}).map(c=>c.label)).toEqual(['消费者','会话']);
  expect(crumbs({page:'commitments',id:'x'}).map(c=>c.label)).toEqual([PAGES.commitments,'承诺详情']);
});

test('a polite live region; tabs carry one when asked to announce', ()=>{
  expect(renderToStaticMarkup(h(Announce,{text:'失败：3 条'}))).toBe('<p class="sr-only" role="status" aria-live="polite" aria-atomic="true" data-announce="true">失败：3 条</p>');
  const html=renderToStaticMarkup(h(Tabs,{label:'范围',value:'a',onChange:()=>{},announce:'未结束：2 条',tabs:[{key:'a',label:'甲'},{key:'b',label:'乙'}]}));
  expect(html).toContain('aria-live="polite"');expect(html).toContain('未结束：2 条');
});

test('企业世界模型 is an expandable sidebar entry: one page per EIOS concept with its own path; the breadcrumb leads back to the summary',()=>{
  expect(NAV_CHILDREN.ontology!.map(c=>c.label)).toEqual(['Object Type','Object','Property','Link','Function','Action','Object Set']);
  expect(NAV_CHILDREN.ontology!.map(c=>routePath(c.route))).toEqual(['/workbench/ontology/object-types','/workbench/ontology/objects','/workbench/ontology/properties',
    '/workbench/ontology/links','/workbench/ontology/functions','/workbench/ontology/actions','/workbench/ontology/object-sets']);
  expect(Object.keys(NAV_CHILDREN)).toEqual(['ontology']);
  expect(crumbs({page:'ontology',section:'links'})).toEqual([{label:'企业世界模型',to:{page:'ontology'}},{label:'Link'}]);
  expect(crumbs({page:'ontology'})).toEqual([]);
});
