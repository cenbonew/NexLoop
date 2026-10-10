import {createElement as h,type ReactNode} from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
import {afterEach,expect,test,vi} from 'vitest';
import {EVALUATOR_STALE_TEXT,PROCESS_UNAVAILABLE_TEXT,WorkbenchError,alerts,humanActions,me,metrics,offers,type Me} from '../src/workbench/api';
import {DONE_TEXT,GOVERNED_SLOTS,SilenceForm,governedWrite,silenceUntil} from '../src/workbench/actions';
import {Alerts,Audit,Operations} from '../src/workbench/pages/Observability';
import {parseRoute} from '../src/workbench/route';

const owner:Me=me({principal_id:'synthetic-owner',role:'owner',actions:['eios:action:nexloop.workbench.read:1','eios:action:nexloop.alert.silence:1']});
const operator:Me=me({principal_id:'synthetic-operator',role:'operator',actions:['eios:action:nexloop.workbench.read:1']});
const firing={dedupe_key:'queue_dead_letter:operations',rule_id:'queue_dead_letter',severity:'critical',selector:'operations',value:'2',first_fired_at:'2026-10-10T09:00:00+00:00',
  last_fired_at:'2026-10-10T09:05:00+00:00',fire_count:1,silenced_until:null};
const alertsBody=(stale:boolean,silenced:string|null=null)=>({real_world_only:true,firing:[{...firing,silenced_until:silenced}],
  events:[{event_id:7,rule_id:'queue_dead_letter',severity:'critical',kind:'firing',selector:'operations',value:'2',threshold:'0',recorded_at:'2026-10-10T09:00:00+00:00'}],
  silences:silenced?[{rule_id:'queue_dead_letter',selector:'operations',until:silenced}]:[],
  evaluator:{last_evaluated_at:'2026-10-10T09:05:00+00:00',rules_version:1,stale}});
const metricsBody={tenant_id:'t',world:'real',computed_at:'2026-10-10T09:05:00+00:00',refusals:{last_5m:{},last_hour:{NXC01:2}},
  effects:{by_state:{unknown:1},unknown:{count:1,oldest_age_seconds:1200,over_24h:0}},replies:{pending:{count:0,oldest_age_seconds:null},escalations_last_hour:{},escalations_total:0},
  commitments:{exceptions:{breached:1},exceptions_last_hour:{}},queues:{operations:{pending:1,retry_wait:0,running:0,dead_lettered:2,dead_lettered_last_hour:2,oldest_pending_seconds:30}},
  feeds:{},extraction:{pending_messages:0,oldest_pending_seconds:null},guard:{status:'ok',window_seconds:300,samples:1,by_service:{'runtime-guard':{sum:{calls:100,timeouts:3},max:{p95_ms:1700,max_ms:2300}}},timeout_rate:0.03,p95_ms_max:1700},
  host:{status:'unavailable'},pool:{status:'unavailable'},connections:{total:12},cost:{budgets:{},last_24h:[]},backup:{status:'unavailable'},evaluator:{stale:false}};
function client(seed:[unknown[],unknown][]){const c=new QueryClient({defaultOptions:{queries:{retry:false,staleTime:Infinity}}});for(const [k,v] of seed)c.setQueryData(k,v);return c;}
const render=(c:QueryClient,node:ReactNode)=>renderToStaticMarkup(h(QueryClientProvider,{client:c},node));
afterEach(()=>vi.unstubAllGlobals());

test('alerts parse strictly; a stale evaluator is announced and a silenced alert is marked, still listed',()=>{
  expect(alerts(alertsBody(false)).firing[0].severity).toBe('critical');
  expect(()=>alerts({...alertsBody(false),real_world_only:false})).toThrow(WorkbenchError);
  expect(()=>alerts({...alertsBody(false),firing:[{...firing,severity:'info'}]})).toThrow(WorkbenchError);
  const stale=render(client([[['workbench','alerts'],alerts(alertsBody(true))]]),h(Alerts,{}));
  expect(stale).toContain(EVALUATOR_STALE_TEXT);expect(stale).toContain('queue_dead_letter');
  const silenced=render(client([[['workbench','alerts'],alerts(alertsBody(false,'2026-10-11T09:00:00+00:00'))]]),h(Alerts,{}));
  expect(silenced).toContain('已静默至 2026-10-11T09:00:00+00:00（仍在评估和记录）');
});

test('operations: unavailable process sections and backups are labelled, never zero; refusals carry their meaning',()=>{
  const m=metrics(metricsBody);
  expect(m.host).toEqual({status:'unavailable'});expect(m.guard.status==='ok'&&m.guard.timeout_rate).toBe(0.03);
  const html=render(client([[['workbench','metrics'],m]]),h(Operations,{}));
  expect(html).toContain(PROCESS_UNAVAILABLE_TEXT);expect(html).toContain('超时率 3.00%');expect(html).toContain('已暂停（NXC01）：2');
  expect(html).toContain('不代表没有备份');expect(html).not.toContain('运行中</th></tr></thead><tbody><tr><td>');
  expect(()=>metrics({...metricsBody,guard:{status:'ok',by_service:{x:{sum:{calls:'many'},max:{}}}}})).toThrow(WorkbenchError);
});

test('audit: owner-only page shows IDs, kinds and times; a forbidden read is labelled',async()=>{
  const items=humanActions({items:[{category:'action',action:'alert.silence',principal_id:'synthetic-owner',target_kind:'alert_rule',target_ref:'queue_dead_letter',intent_id:'i',occurred_at:'2026-10-10T09:06:00+00:00'},
    {category:'read',action:'read.conversation',principal_id:'synthetic-owner',role:'owner',target_kind:'message',target_ref:'eios:object:Message/'+'a'.repeat(64),intent_id:null,occurred_at:'2026-10-10T09:07:00+00:00'}]});
  const html=render(client([[['workbench','human-actions'],items]]),h(Audit,{}));
  expect(html).toContain('alert.silence');expect(html).toContain('读取');
  expect(()=>humanActions({items:[{category:'other'}]})).toThrow(WorkbenchError);
});

test('silencing is offered to the owner only, sends the chosen rule and selector with a bounded end, and is routed',async()=>{
  expect(offers(owner,'silence_alert')).toBe(true);expect(offers(operator,'silence_alert')).toBe(false);
  const c=client([[['workbench','alerts'],alerts(alertsBody(false))]]);
  expect(render(c,h(SilenceForm,{member:operator}))).toBe('');
  const form=render(c,h(SilenceForm,{member:owner}));expect(form).toContain('静默告警');expect(form).toContain('“告警系统不可用”不能静默');
  expect(silenceUntil(500,Date.parse('2026-10-10T00:00:00Z'))).toBe('2026-10-17T00:00:00Z');
  expect(silenceUntil(0,Date.parse('2026-10-10T00:00:00Z'))).toBe('2026-10-10T01:00:00Z');
  const writes:{url:string;body:unknown}[]=[];
  vi.stubGlobal('fetch',vi.fn(async(url:string,init?:RequestInit)=>{
    if(url==='/api/v1/workbench/auth/csrf')return new Response(JSON.stringify({authenticated:true,csrf_token:'t'}),{status:200});
    writes.push({url,body:JSON.parse(String(init?.body))});return new Response(JSON.stringify({silenced:true}),{status:200});}));
  const body={rule_id:'queue_dead_letter',selector:'operations',until:silenceUntil(24),reason:'已知故障处理中'};
  await governedWrite(new QueryClient(),'silence_alert',body,{current:null});
  expect(writes).toEqual([{url:'/api/v1/workbench/actions/silence_alert',body}]);
  expect(DONE_TEXT.silence_alert).toContain('仍会评估和记录');
  expect(typeof GOVERNED_SLOTS.alerts).toBe('function');
  expect(parseRoute('/workbench/alerts')).toEqual({page:'alerts'});expect(parseRoute('/workbench/audit')).toEqual({page:'audit'});
});
