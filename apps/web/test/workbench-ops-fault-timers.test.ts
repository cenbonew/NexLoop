// The 运行状态 page for drills and patrols. ADR-045 (0423): a live test-Consumer fault switch is a 演练中 banner with the remaining count
// and the injections of the hour; off is 未开启; no block is 无数据 (never taken for off); malformed is 格式无法识别. 0424: watched timers no
// host reports, listed as role:timer in yellow. Neither block failing to parse takes the other cards down (AT-045).
import {createElement as h,type ReactNode} from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
import {afterEach,expect,test,vi} from 'vitest';
import {metrics,syntheticFault,timersNotReported} from '../src/workbench/api';
import {HostHealthCard,Operations,SyntheticFaultBanner,minutesLeft} from '../src/workbench/pages/Observability';

const metricsBody={tenant_id:'t',world:'real',computed_at:'2026-11-08T09:05:00+00:00',refusals:{last_5m:{},last_hour:{NXC01:2}},
  effects:{by_state:{},unknown:{count:0,oldest_age_seconds:null,over_24h:0}},replies:{pending:{count:0,oldest_age_seconds:null},escalations_last_hour:{},escalations_total:0},
  commitments:{exceptions:{},exceptions_last_hour:{}},queues:{},feeds:{},extraction:{pending_messages:0,oldest_pending_seconds:null},
  guard:{status:'ok',window_seconds:300,samples:1,by_service:{'runtime-guard':{sum:{calls:100,timeouts:3},max:{p95_ms:1700,max_ms:2300}}},timeout_rate:0.03,p95_ms_max:1700},
  host:{status:'unavailable'},pool:{status:'unavailable'},connections:{total:12},backup:{status:'unavailable'}};
const page=(m:unknown)=>{const c=new QueryClient({defaultOptions:{queries:{retry:false,staleTime:Infinity}}});c.setQueryData(['workbench','metrics'],metrics(m));
  return renderToStaticMarkup(h(QueryClientProvider,{client:c},h(Operations,{}) as ReactNode));};
const banner=(v:unknown)=>renderToStaticMarkup(h(SyntheticFaultBanner,{fault:syntheticFault(v)}));
const host=(v:unknown)=>renderToStaticMarkup(h(HostHealthCard,{timers:timersNotReported(v)}));
const attr=(html:string,name:string)=>html.match(new RegExp(`data-${name}="([a-z_]+)"`))![1];

test('fault switch: live is a 演练中 banner with remaining and injections; off 未开启; absent 无数据 (not off); malformed 格式无法识别',()=>{
  const live=banner({active:1,remaining:7,injected_last_hour:3});
  expect(attr(live,'fault')).toBe('active');expect(live).toContain('演练中');expect(live).toContain('剩余 <span class="num">7</span> 次');
  expect(live).toContain('已注入 <span class="num">3</span> 次');expect(live).toContain('role="status"');
  const off=banner({active:0,remaining:0,injected_last_hour:0});expect(attr(off,'fault')).toBe('off');expect(off).toContain('未开启');expect(off).not.toContain('演练中');
  const none=banner(undefined);expect(attr(none,'fault')).toBe('no_data');expect(none).toContain('无数据');expect(none).not.toContain('未开启');
  for(const bad of [{active:2,remaining:1,injected_last_hour:0},{active:1,remaining:-1,injected_last_hour:0},{active:1},'x'])
    expect(attr(banner(bad),'fault')).toBe('invalid');
});

test('timers: names listed in yellow when any; none said as such; absent fields 无数据; malformed or a count off its list 格式无法识别',()=>{
  const missing=host({timer_success_age_seconds:{},timers_not_reported:2,timers_not_reported_names:['app_host:nexloop-backup','data_host:nexloop-restic-check']});
  expect(attr(missing,'timers')).toBe('missing');expect(missing).toContain('app_host:nexloop-backup');expect(missing).toContain('data_host:nexloop-restic-check');
  expect(missing).toContain('tone-limited');expect(missing).toContain('2 个定时任务未上报');
  const ok=host({timers_not_reported:0,timers_not_reported_names:[]});expect(attr(ok,'timers')).toBe('ok');expect(ok).not.toContain('tone-limited');
  expect(attr(host(undefined),'timers')).toBe('no_data');expect(attr(host({timer_success_age_seconds:{}}),'timers')).toBe('no_data');
  for(const bad of [{timers_not_reported:2,timers_not_reported_names:['a:b']},{timers_not_reported:'2',timers_not_reported_names:[]},{timers_not_reported:1,timers_not_reported_names:[3]}])
    expect(attr(host(bad),'timers')).toBe('invalid');
});

test('on the page: both blocks shown; malformed blocks leave the other cards as usual; an older snapshot says 无数据 for both',()=>{
  const full=page({...metricsBody,synthetic_fault:{active:1,remaining:5,injected_last_hour:1},host_health:{timers_not_reported:1,timers_not_reported_names:['app_host:nexloop-backup']}});
  expect(full).toContain('data-fault="active"');expect(full).toContain('data-timers="missing"');
  const bad=page({...metricsBody,synthetic_fault:{active:'yes'},host_health:{timers_not_reported:3,timers_not_reported_names:[]}});
  expect(bad).toContain('data-fault="invalid"');expect(bad).toContain('data-timers="invalid"');
  expect(bad).toContain('超时率 3.00%');expect(bad).toContain('备份记录暂不可用');expect(bad).toContain('<span title="NXC01">已暂停</span>');
  expect(bad).toContain('data-gate="no_data"');expect(bad).toContain('data-budget="no_data"');
  const old=page(metricsBody);expect(old).toContain('data-fault="no_data"');expect(old).toContain('data-timers="no_data"');expect(old).not.toContain('演练中');
});

afterEach(()=>vi.useRealTimers());
test('0427: a live switch shows its end in Beijing time with the minutes left and its member count',()=>{
  vi.useFakeTimers();vi.setSystemTime(new Date('2026-11-08T02:17:30Z'));
  const live=banner({active:1,remaining:4,injected_last_hour:1,expires_at:'2026-11-08T03:00:00Z',members:3});
  expect(attr(live,'fault')).toBe('active');expect(live).toContain('名单 <span class="num">3</span> 人');
  expect(live).toMatch(/<time dateTime="2026-11-08T03:00:00.000Z" title="北京时间 [^"]*11:00[^"]*">/);expect(live).toContain('（还剩 43 分钟）');
  expect(live).not.toContain('快照未提供');
  expect(minutesLeft('2026-11-08T03:00:00Z',Date.parse('2026-11-08T03:00:01Z'))).toBe('已到期');
});

test('0427: an older backend keeps 快照未提供; off with nulls is 未开启; null with active=1 or one field alone is malformed',()=>{
  expect(banner({active:1,remaining:4,injected_last_hour:1})).toContain('到期时间和名单人数快照未提供');
  expect(attr(banner({active:0,remaining:0,injected_last_hour:0,expires_at:null,members:null}),'fault')).toBe('off');
  for(const bad of [{expires_at:null,members:null},{expires_at:'2026-11-08T03:00:00Z',members:null},{expires_at:null,members:2},
    {expires_at:'2026-11-08T03:00:00Z'},{members:2},{expires_at:'soon',members:2},{expires_at:'2026-11-08T03:00:00Z',members:-1}])
    expect(attr(banner({active:1,remaining:4,injected_last_hour:1,...bad}),'fault')).toBe('invalid');
  const page_=page({...metricsBody,synthetic_fault:{active:1,remaining:4,injected_last_hour:0,expires_at:null,members:null}});
  expect(page_).toContain('data-fault="invalid"');expect(page_).toContain('超时率 3.00%');expect(page_).toContain('备份记录暂不可用');
});
