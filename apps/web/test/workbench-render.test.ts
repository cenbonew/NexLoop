import {createElement as h} from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {expect,test} from 'vitest';
import {Block,Failure} from '../src/workbench/pages/Page';
import {NotEnabled} from '../src/workbench/pages/Settings';

test('a forbidden or unavailable block renders its label, never the data renderer (no "0" for missing data)',()=>{
  const forbidden=renderToStaticMarkup(h(Block<{restricted:number}>,{title:'联系限制',section:{status:'forbidden',data:null},children:d=>`受限 ${d.restricted}`}));
  expect(forbidden).toContain('无权限查看此区块');expect(forbidden).not.toContain('受限 ');
  const unavailable=renderToStaticMarkup(h(Block<{restricted:number}>,{title:'联系限制',section:{status:'unavailable',data:null},children:d=>`受限 ${d.restricted}`}));
  expect(unavailable).toContain('不代表数量为零');expect(unavailable).not.toContain('受限 0');
  expect(renderToStaticMarkup(h(Block<{restricted:number}>,{title:'联系限制',section:{status:'ok',data:{restricted:0}},children:d=>`受限 ${d.restricted}`}))).toContain('受限 0');
});

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
