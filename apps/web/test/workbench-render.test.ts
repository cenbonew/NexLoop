import {createElement as h} from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {expect,test} from 'vitest';
import {Failure} from '../src/workbench/pages/Page';
import {NotEnabled} from '../src/workbench/pages/Settings';

test('each failure kind has its own alert text; only unavailable offers a retry',()=>{
  for(const kind of ['unauthenticated','forbidden','not_found','invalid_request','invalid'] as const){
    const html=renderToStaticMarkup(h(Failure,{kind,onRetry:()=>{}}));expect(html).toContain('role="alert"');expect(html).not.toContain('重试');
  }
  expect(renderToStaticMarkup(h(Failure,{kind:'unavailable',onRetry:()=>{}}))).toContain('重试');
});

test('ontology/experiments pages are labelled not enabled and offer nothing to click',()=>{
  const html=renderToStaticMarkup(h(NotEnabled,{title:'本体 / 演进'}));
  expect(html).toContain('未启用');expect(html).not.toContain('<button');
});
