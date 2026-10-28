import {createElement as h} from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
import {expect,test,vi} from 'vitest';
import {WorkbenchError,WorkbenchWriteError,me} from '../src/workbench/api';
import {DELIVER,apply,checkReason,idempotencyKey,plan,pythonJson,realDispatch} from '../src/workbench/realDispatch';
import {RealDispatchPanel} from '../src/workbench/RealDispatchPanel';
// The owner's real dispatch button (owner 2026-10-18): same plan and request keys as scripts/deploy/real_dispatch_switch.py.

const SEND='eios:action:nexloop.service.request:1';
const closed=realDispatch({world:'real',tenant:{enabled:false,data_mode:'real',revision:7},actions:[{action_type:DELIVER,enabled:false,revision:3}]});
const open=realDispatch({world:'real',tenant:{enabled:true,data_mode:'real',revision:8},actions:[{action_type:DELIVER,enabled:true,revision:4},{action_type:SEND,enabled:true,revision:5}]});

test('the state is parsed strictly (states and revisions only)',()=>{
  expect(realDispatch({world:'real',tenant:null,actions:[]})).toEqual({world:'real',tenant:null,actions:[]});
  expect(()=>realDispatch({world:'real',tenant:{enabled:'yes',data_mode:'real',revision:1},actions:[]})).toThrow(WorkbenchError);
  expect(()=>realDispatch({world:'real',tenant:null,actions:[{action_type:'not-a-resource',enabled:true,revision:1}]})).toThrow(WorkbenchError);
});

test('one click open: the tenant (data_mode real) then enterprise.deliver:1; close: the tenant and every open Action switch',()=>{
  expect(plan(closed,'open')).toEqual([
    {scope_kind:'tenant',enabled:true,action_type:null,data_mode:'real',from_revision:7},
    {scope_kind:'action_type',enabled:true,action_type:DELIVER,data_mode:null,from_revision:3}]);
  expect(plan(open,'open')).toEqual([]);
  expect(plan(open,'close').map(c=>[c.scope_kind,c.action_type,c.enabled,c.from_revision])).toEqual([['tenant',null,false,8],['action_type',DELIVER,false,4],['action_type',SEND,false,5]]);
  expect(plan(realDispatch({world:'real',tenant:null,actions:[]}),'open').map(c=>c.from_revision)).toEqual([0,0]);
});

test('the request keys equal the script\'s (Python json.dumps sort_keys, ASCII escapes)',async()=>{
  const [t,d]=plan(closed,'open');
  expect(await idempotencyKey(t,'演示开始：负责人打开')).toBe('switch-e7f0b8a695d7aba2c95085d00436404aa3c0a0073d1cf2e4');
  expect(await idempotencyKey(d,'演示开始：负责人打开')).toBe('switch-8fc058c3985aadf8a2c26f65246daa2aedd80a73877bb36d');
  const [ct,cd,cs]=plan(open,'close');
  const reason='demo end "quoted"\n';
  expect(await idempotencyKey(ct,reason)).toBe('switch-7e835c01d54623f5269f2ee11a41b7f284c1233bd15ffc83');
  expect(await idempotencyKey(cd,reason)).toBe('switch-9f3e14d496e8553023b84123a653354f20c1d18b773bd25f');
  expect(await idempotencyKey(cs,reason)).toBe('switch-6e3a38d8f4a824822bb363d328a5c7123896f5319e4f1486');
  expect(pythonJson({b:1,a:[true,null],c:'é😀'})).toBe('{"a": [true, null], "b": 1, "c": "\\u00e9\\ud83d\\ude00"}');
  expect(checkReason('')).not.toBeNull();expect(checkReason(' x')).not.toBeNull();expect(checkReason('x'.repeat(501))).not.toBeNull();expect(checkReason('演示')).toBeNull();
});

test('changes go out in order through set_real_dispatch; the first refused or unknown result stops the run (no retry)',async()=>{
  const act=vi.fn(async(_op:string,body:Record<string,unknown>)=>body.scope_kind==='tenant'?{control_revision:9}:Promise.reject(new WorkbenchWriteError('unavailable')));
  const results=await apply(plan(closed,'open'),'演示开始',act as never);
  expect(act.mock.calls.map(c=>c[0])).toEqual(['set_real_dispatch','set_real_dispatch']);
  expect(act.mock.calls[0][1]).toEqual({scope_kind:'tenant',enabled:true,action_type:null,data_mode:'real',reason:'演示开始'});   // no from_revision in the body
  expect(act.mock.calls[0][2]).toMatch(/^switch-[0-9a-f]{48}$/);
  expect(results.map(r=>r.ok?['ok',r.control_revision]:['fail',r.kind])).toEqual([['ok',9],['fail','unavailable']]);
  const refused=vi.fn(async()=>Promise.reject(new WorkbenchWriteError('forbidden')));
  expect((await apply(plan(closed,'open'),'x',refused as never)).length).toBe(1);expect(refused).toHaveBeenCalledTimes(1);
});

test('only a member whose role carries nexloop.real_dispatch.set gets the buttons; the others see the layers read-only',()=>{
  const render=(actions:string[])=>{const cache=new QueryClient();
    cache.setQueryData(['workbench','me'],me({principal_id:'synthetic-staff',role:actions.length?'owner':'operator',actions}));
    cache.setQueryData(['workbench','real-dispatch'],closed);
    return renderToStaticMarkup(h(QueryClientProvider,{client:cache},h(RealDispatchPanel)));};
  const owner=render(['eios:action:nexloop.real_dispatch.set:1']);
  expect(owner).toContain('开启租户层与交付 Action 两层');expect(owner).toContain('关闭租户层与全部 Action 层');
  expect(owner).toContain('部署层 REAL_DISPATCH_ENABLED');expect(owner).toContain('未知');expect(owner).toContain('需在 APP_HOST 部署层开启');
  expect(owner).toContain('版本 7');expect(owner).toContain('enterprise.deliver:1');
  const operator=render(['eios:action:Control.set:1']);
  expect(operator).not.toContain('开启租户层与交付 Action 两层');expect(operator).toContain('只读：只有负责人可以开关。');expect(operator).toContain('租户层');
});
