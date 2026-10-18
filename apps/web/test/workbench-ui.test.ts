import {createElement as h} from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {expect,test} from 'vitest';
import {Empty,StatusBadge,Tabs,commitmentTone,intentTone,money,relative} from '../src/workbench/ui';
import {dispatchText} from '../src/workbench/pages/Actions';
import {crumbs,NAV_GROUPS} from '../src/workbench/WorkbenchApp';
import {PAGES} from '../src/workbench/route';

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
test('navigation: three groups, every page once, the next-stage pages last; detail pages have breadcrumbs', ()=>{
  expect(NAV_GROUPS.map(g=>g.title)).toEqual(['运营','监控','管理']);
  const all=NAV_GROUPS.flatMap(g=>g.pages);expect([...all].sort()).toEqual(Object.keys(PAGES).sort());expect(all.slice(-2)).toEqual(['ontology','experiments']);
  expect(crumbs({page:'consumers'})).toEqual([]);
  expect(crumbs({page:'consumers',id:'x',sub:'conversation'}).map(c=>c.label)).toEqual(['消费者','会话']);
  expect(crumbs({page:'commitments',id:'x'}).map(c=>c.label)).toEqual([PAGES.commitments,'承诺详情']);
});
