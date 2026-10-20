// World model W3 (owner 2026-10-19 D1-D7): the capability catalog page on L6's read (packages/contracts/workbench-capabilities.schema.json).
import {createElement as h} from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
import {expect,test} from 'vitest';
import {readFileSync} from 'node:fs';
import {WorkbenchError,capabilities} from '../src/workbench/api';
import {Capabilities} from '../src/workbench/pages/Capabilities';
import {outcomeText} from '../src/workbench/RealDispatchPanel';
import {realDispatch} from '../src/workbench/realDispatch';
import {PAGES,parseRoute,routePath} from '../src/workbench/route';

const example=JSON.parse(readFileSync(new URL('../../../packages/contracts/examples/workbench-capabilities.valid.json',import.meta.url),'utf8'));
function render(query?:Record<string,string>,body:unknown=example){
  const c=new QueryClient({defaultOptions:{queries:{retry:false,staleTime:Infinity}}});
  c.setQueryData(['workbench','capabilities'],capabilities(body));
  return renderToStaticMarkup(h(QueryClientProvider,{client:c},h(Capabilities,{query,onQuery:()=>{}})));
}

test('the parser accepts the contract example and refuses unknown categories or trust levels',()=>{
  const c=capabilities(example);expect(c.actions[1].effect_category).toBeNull();expect(c.channels[0].trust).toBe('server');
  const bad=structuredClone(example);bad.actions[0].effect_category='marketing';expect(()=>capabilities(bad)).toThrow(WorkbenchError);
  const trust=structuredClone(example);trust.channels[0].trust='trusted';expect(()=>capabilities(trust)).toThrow(WorkbenchError);
});

test('route and page name; tabs live in the URL',()=>{
  expect(PAGES.capabilities).toBe('能力目录');
  expect(parseRoute('/workbench/capabilities','?tab=dispatch&x=1').query).toEqual({tab:'dispatch'});
  expect(routePath({page:'capabilities',query:{tab:'actions'}})).toBe('/workbench/capabilities?tab=actions');
});

test('the catalog: summary, registry, Actions with effect categories (undeclared = closed), channels and routes, nothing to edit',()=>{
  const html=render();
  expect(html).toContain('能力目录');expect(html).toContain('受治理能力 2 项 · 业务 Action 2 个（生效 2）');expect(html).toContain('1 个生效 Action 未声明效应类别（关闭）');
  expect(html).toContain('goals.version.publish');expect(html).toContain('仅人类');
  const actions=render({tab:'actions'});
  expect(actions).toContain('data-code="customer_contact"');expect(actions).toContain('未声明（按客户联系处理，关闭）');expect(actions).toContain('v3');
  const channels=render({tab:'channels'});
  expect(channels).toContain('nexloop.api');expect(channels).toContain('nexloop.service.request:1');expect(channels).toContain('部署配置');
  for(const html of [render(),actions,channels])expect(html).not.toMatch(/<form|<textarea|开启租户层/);
});

test('real dispatch: the same parser, layers and outcome as the settings page, read only with a link to settings',()=>{
  const html=render({tab:'dispatch'});
  const expected=outcomeText(realDispatch(example.real_dispatch));
  expect(html).toContain(expected.text);expect(html).toContain(`data-dispatchable="${expected.dispatchable}"`);
  expect(html).toContain('真实派发三层开关');expect(html).toContain('href="/workbench/settings"');expect(html).not.toContain('<button type="button" onClick');
  const broken=structuredClone(example);broken.real_dispatch.world=7;
  expect(render({tab:'dispatch'},broken)).toContain('真实派发开关暂不可用');
});
