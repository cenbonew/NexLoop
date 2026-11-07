// Budget health (0422) on the 运行状态 page: one row per kind (model, evolution first) with its period end in Beijing time, days left and
// the share used. Unconfigured, expired and recent budget refusals are red with the renewal hint; ending within 7 days is yellow. No block
// (an older backend) is 无数据, never 正常; a malformed block is 格式无法识别 and never fails the other cards (AT-045).
import {createElement as h,type ReactNode} from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
import {expect,test} from 'vitest';
import {budgetHealth,metrics} from '../src/workbench/api';
import {BUDGET_RENEW,BudgetHealthCard,Operations,remainingText} from '../src/workbench/pages/Observability';

const DAY=86400;
const kind=(over:Record<string,unknown>={})=>({configured:1,period_end:'2026-12-01T00:00:00+00:00',expires_in_seconds:20*DAY,expired:0,ratio:0.4512,...over});
const block=(model:Record<string,unknown>={},evolution:Record<string,unknown>={},counts:Record<string,unknown>={})=>({
  kinds:{evolution:kind(evolution),model:kind(model)},required:['model','evolution'],expiring_7d:0,expired:0,unconfigured:0,exhausted:0,exhausted_refusals_1h:0,...counts});
const card=(v:unknown)=>renderToStaticMarkup(h(BudgetHealthCard,{health:budgetHealth(v)}));
const state=(html:string)=>html.match(/data-budget="([a-z_]+)"/)![1];
const metricsBody={tenant_id:'t',world:'real',computed_at:'2026-11-07T09:05:00+00:00',refusals:{last_5m:{},last_hour:{NXC01:2}},
  effects:{by_state:{},unknown:{count:0,oldest_age_seconds:null,over_24h:0}},replies:{pending:{count:0,oldest_age_seconds:null},escalations_last_hour:{},escalations_total:0},
  commitments:{exceptions:{},exceptions_last_hour:{}},queues:{},feeds:{},extraction:{pending_messages:0,oldest_pending_seconds:null},
  guard:{status:'ok',window_seconds:300,samples:1,by_service:{'runtime-guard':{sum:{calls:100,timeouts:3},max:{p95_ms:1700,max_ms:2300}}},timeout_rate:0.03,p95_ms_max:1700},
  host:{status:'unavailable'},pool:{status:'unavailable'},connections:{total:12},backup:{status:'unavailable'}};
const page=(m:unknown)=>{const c=new QueryClient({defaultOptions:{queries:{retry:false,staleTime:Infinity}}});c.setQueryData(['workbench','metrics'],metrics(m));
  return renderToStaticMarkup(h(QueryClientProvider,{client:c},h(Operations,{}) as ReactNode));};

test('normal: model and evolution first, Beijing time, days left and share used; no red, no yellow',()=>{
  const p=budgetHealth(block());expect(p!.status==='ok'&&p!.kinds.map(k=>k.kind)).toEqual(['model','evolution']);
  const html=card(block());expect(state(html)).toBe('ok');
  expect(html).toContain('模型');expect(html).toContain('演进');expect(html).toContain('剩余 20 天');expect(html).toContain('45.1%');
  expect(html).toMatch(/title="北京时间 [^"]+"/);expect(html).not.toContain(BUDGET_RENEW);expect(html).toContain('有效');
});

test('ending within 7 days is yellow without the renewal hint',()=>{
  const html=card(block({expires_in_seconds:3*DAY},{},{expiring_7d:1}));
  expect(state(html)).toBe('yellow');expect(html).toContain('7 天内到期');expect(html).toContain('tone-limited');expect(html).not.toContain(BUDGET_RENEW);
});

test('expired, unconfigured and recent refusals are red with the renewal hint',()=>{
  const expired=card(block({expires_in_seconds:-2*DAY-5,expired:1},{},{expired:1}));
  expect(state(expired)).toBe('red');expect(expired).toContain('已过期 2 天');expect(expired).toContain(BUDGET_RENEW);expect(expired).toContain('tone-fail');
  const unconfigured=card(block({},{configured:0,period_end:null,expires_in_seconds:null,ratio:null},{unconfigured:1}));
  expect(state(unconfigured)).toBe('red');expect(unconfigured).toContain('未配置');expect(unconfigured).toContain(BUDGET_RENEW);
  const refused=card(block({},{},{exhausted_refusals_1h:3}));
  expect(state(refused)).toBe('red');expect(refused).toContain('近 1 小时有 3 次预算拒绝');expect(refused).toContain(BUDGET_RENEW);
  expect(remainingText({kind:'model',configured:true,period_end:'x',expires_in_seconds:3600,expired:false,ratio:null})).toBe('不到 1 天');
});

test('no block is 无数据 (never 正常 or 有效); a malformed block is 格式无法识别',()=>{
  const none=card(undefined);expect(state(none)).toBe('no_data');expect(none).toContain('无数据');expect(none).not.toContain('有效');expect(none).not.toContain('正常');
  for(const bad of [{...block(),required:['evolution']},{...block(),required:['model','evolution','extra']},{...block(),kinds:{model:kind()}},block({configured:2}),block({ratio:'45%'}),block({period_end:null}),'x']){
    expect(budgetHealth(bad)).toEqual({status:'invalid'});expect(state(card(bad))).toBe('invalid');}
});

test('a malformed block leaves the other operations cards as usual; an older snapshot says 无数据',()=>{
  const bad=page({...metricsBody,budget_health:{kinds:{model:'x'}}});
  expect(bad).toContain('data-budget="invalid"');expect(bad).toContain('超时率 3.00%');expect(bad).toContain('备份记录暂不可用');
  expect(bad).toContain('<span title="NXC01">已暂停</span>');expect(bad).toContain('data-gate="no_data"');
  expect(page(metricsBody)).toContain('data-budget="no_data"');
  expect(page({...metricsBody,budget_health:block()})).toContain('data-budget="ok"');
});

test('W1: required is the server\'s list; only model is required on the client, a model-only tenant renders normally',()=>{
  const only={kinds:{model:kind()},required:['model'],expiring_7d:0,expired:0,unconfigured:0,exhausted:0,exhausted_refusals_1h:0};
  const p=budgetHealth(only);expect(p!.status).toBe('ok');expect(p!.status==='ok'&&p!.kinds.map(k=>k.kind)).toEqual(['model']);
  const html=card(only);expect(state(html)).toBe('ok');expect(html).not.toContain('格式无法识别');expect(html).toContain('模型');expect(html).not.toContain('演进');
  expect(budgetHealth({...only,required:['model','evolution']})).toEqual({status:'invalid'});  // a required kind missing from kinds
});

test('W2: a budget used up inside its period is red 已用完 with the renewal hint, never 有效',()=>{
  for(const ratio of [1,1.25]){
    const html=card(block({ratio},{},{exhausted:1}));
    expect(state(html)).toBe('red');expect(html).toContain('已用完');expect(html).toContain(BUDGET_RENEW);expect(html).toContain('data-code="exhausted"');
  }
  expect(state(card(block({ratio:0.9999})))).toBe('ok');
});
