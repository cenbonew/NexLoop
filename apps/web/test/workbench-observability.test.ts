import {createElement as h,type ReactNode} from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
import {afterEach,expect,test,vi} from 'vitest';
import {BACKUP_UNAVAILABLE_TEXT,EVALUATOR_STALE_TEXT,PROCESS_UNAVAILABLE_TEXT,WorkbenchError,alerts,humanActions,me,metrics,offers,type Me} from '../src/workbench/api';
import {DONE_TEXT,GOVERNED_SLOTS,SilenceForm,governedWrite,silenceUntil} from '../src/workbench/actions';
import {Alerts,Audit,Operations} from '../src/workbench/pages/Observability';
import {parseRoute} from '../src/workbench/route';
import {when} from '../src/workbench/pages/Consumers';

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
  expect(silenced).toMatch(/已静默至 <time dateTime="2026-10-11T09:00:00.000Z" title="北京时间 [^"]+">[^<]+<span class="time-rel">（[^）]+）<\/span><\/time>（仍在评估和记录）/);   // batch 8: <time datetime>, Beijing + relative
});

test('operations: unavailable process sections and backups are labelled, never zero; refusals carry their meaning',()=>{
  const m=metrics(metricsBody);
  expect(m.host).toEqual({status:'unavailable'});expect(m.guard.status==='ok'&&m.guard.timeout_rate).toBe(0.03);
  const html=render(client([[['workbench','metrics'],m]]),h(Operations,{}));
  expect(html).toContain('样本暂不可用：最近 5 分钟没有样本');expect(html).toContain('未加载，不代表数量为零。');expect(html).toContain('超时率 3.00%');expect(html).toContain('<span title="NXC01">已暂停</span>：<span class="num">2</span>');
  expect(html).toContain('备份记录暂不可用：还没有任何已记录的数据库备份');expect(html).toContain('这不代表没有备份。');void PROCESS_UNAVAILABLE_TEXT;void BACKUP_UNAVAILABLE_TEXT;expect(html).not.toContain('运行中</th></tr></thead><tbody><tr><td>');
  expect(()=>metrics({...metricsBody,guard:{status:'ok',by_service:{x:{sum:{calls:'many'},max:{}}}}})).toThrow(WorkbenchError);
});

test('operations: a feed that splits due and scheduled counts only the due items and names the scheduled ones apart',()=>{
  const m=metrics({...metricsBody,feeds:{'commitment-monitor':{pending:10,due:2,scheduled:8,dead_lettered:0,dead_lettered_last_hour:0,
    oldest_pending_seconds:1200,oldest_due_seconds:1200,max_pending_attempts:1}}});
  const html=render(client([[['workbench','metrics'],m]]),h(Operations,{}));
  expect(html).toContain('：待处理 2 个（另有 8 个定时未到期），');expect(html).toContain('超过告警阈值 10 分钟');
  const unsplit=render(client([[['workbench','metrics'],metrics({...metricsBody,feeds:{'recall-instance':{pending:5,dead_lettered:0,dead_lettered_last_hour:0,oldest_pending_seconds:30}}})]]),h(Operations,{}));
  expect(unsplit).toContain('：待处理 5 个，');expect(unsplit).not.toContain('定时未到期');
});

test('operations: the backup block shows numbers and states, “无” for a metric without an observation, and parses strictly',()=>{
  const backup={status:'ok',age_seconds:1800,full_backup_age_seconds:86400,repo_backup_age_seconds:{repo1:1800},verify_failed:0,wal_last_archived_age_seconds:30,
    wal_archive_failing:1,journal_export_lag_seconds:0,drill_age_seconds:3600};  // no file snapshot, no drill RPO/RTO observed
  const m=metrics({...metricsBody,backup});
  expect(m.backup.status==='ok'&&m.backup.file_backup_age_seconds).toBe(null);
  const html=render(client([[['workbench','metrics'],m]]),h(Operations,{}));
  expect(html).toContain('<div class="kv-row"><dt>最近一次已校验的数据库备份</dt><dd>30 分钟</dd></div>');expect(html).toContain('<div class="kv-row"><dt>最近一次文件快照</dt><dd>无</dd></div>');
  expect(html).toContain('<div class="kv-row"><dt>最近一次演练的 RPO</dt><dd>无</dd></div>');expect(html).toContain('<div class="kv-row"><dt>恢复日志导出延迟</dt><dd>0 秒</dd></div>');
  expect(html).toContain('<div class="kv-row"><dt>最旧的未备份文件对象（D6）</dt><dd>无</dd></div>');  // absent: everything covered
  const unprotected=metrics({...metricsBody,backup:{...backup,file_backup_unprotected_seconds:7200}});
  expect(unprotected.backup.status==='ok'&&unprotected.backup.file_backup_unprotected_seconds).toBe(7200);
  expect(html).toMatch(/<dt>最近一次备份校验<\/dt><dd><span class="status-badge tone-ok"[^>]*>.*?通过<\/span><\/dd>/);expect(html).toMatch(/<dt>WAL 归档<\/dt><dd><span class="status-badge tone-fail"[^>]*>.*?出现失败<\/span><\/dd>/);
  expect(html).toContain('主仓库 30 分钟');expect(html).toContain('需要处理');
  expect(html).not.toContain('备份记录暂不可用');
  for(const bad of [{...backup,age_seconds:'1800'},{...backup,age_seconds:-1},{...backup,status:'stale'},{...backup,repo_backup_age_seconds:{repo1:null}}])
    expect(()=>metrics({...metricsBody,backup:bad})).toThrow(WorkbenchError);
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

test('overview backlog block renders the NX-030 shape: feed name, pending count and oldest wait; empty queues read “无”, never blank',async()=>{
  const {overview}=await import('../src/workbench/api');
  const {Overview}=await import('../src/workbench/pages/Overview');
  const unavailable={status:'unavailable',data:null};
  const body={goals:unavailable,commitments:unavailable,actions:unavailable,contact:unavailable,takeovers:unavailable,commercial:unavailable,
    backlog:{status:'ok',data:{queues:{},feeds:{'recall-instance':{pending:5,dead_lettered:0,dead_lettered_last_hour:0,oldest_pending_seconds:12578}},
      extraction:{pending_messages:2,oldest_pending_seconds:12578}}}};
  const html=render(client([[['workbench','overview'],overview(body)]]),h(Overview,{onOpen:()=>{}}));
  const block=html.slice(html.indexOf('aria-label="队列积压"'));
  expect(block).toContain('实例召回：待处理 5，死信 0，最久等待 3 小时');expect(block).not.toContain('recall-instance');
  // Feed backlog fix: a feed that splits due and scheduled counts only the due items as pending and names the scheduled ones apart.
  const split={...body,backlog:{status:'ok',data:{queues:{},feeds:{'commitment-monitor':{pending:10,due:0,scheduled:10,dead_lettered:0,dead_lettered_last_hour:0,
    oldest_pending_seconds:null,oldest_due_seconds:null,max_pending_attempts:0}},extraction:{pending_messages:0,oldest_pending_seconds:null}}}};
  const splitBlock=render(client([[['workbench','overview'],overview(split)]]),h(Overview,{onOpen:()=>{}}));
  expect(splitBlock).toContain('：待处理 0（另有 10 个定时未到期），死信 0，');
  expect(block).toContain('队列</h3><div class="state state-empty state-compact" role="status" data-state="empty"><p class="state-title"><span class="state-mark" aria-hidden="true">○</span>暂无排队中的任务</p>');
  expect(block).toContain('待提取消息 2 条，最久等待 3 小时');
  // A malformed backlog is refused, never rendered as an empty success.
  expect(()=>overview({...body,backlog:{status:'ok',data:{queues:{},feeds:{x:{pending:-1}},extraction:{pending_messages:0,oldest_pending_seconds:null}}}})).toThrow(WorkbenchError);
});

test('overview commercial block: unavailable today is labelled; when served, budgets and 24 h costs render and empties read “无”',async()=>{
  const {overview}=await import('../src/workbench/api');
  const {Overview}=await import('../src/workbench/pages/Overview');
  const unavailable={status:'unavailable',data:null};
  const base={goals:unavailable,commitments:unavailable,actions:unavailable,contact:unavailable,takeovers:unavailable,backlog:unavailable};
  const off=render(client([[['workbench','overview'],overview({...base,commercial:unavailable})]]),h(Overview,{onOpen:()=>{}}));
  const offBlock=off.slice(off.indexOf('aria-label="商业事件与费用"'));
  expect(offBlock).toContain('data-state="unavailable"');expect(offBlock).toContain('商业事件与费用暂不可用');expect(offBlock).toContain('未加载，不代表数量为零。');
  const served={status:'ok',data:{budgets:{model_tokens:{limit:'100',unit:'CNY',consumed:'12.5',ratio:0.125}},
    last_24h:[{cost_kind:'model',data_mode:'real',currency:'CNY',amount:'12.50',entries:3,unpriced:1}]}};
  const on=render(client([[['workbench','overview'],overview({...base,commercial:served})]]),h(Overview,{onOpen:()=>{}}));
  const block=on.slice(on.indexOf('aria-label="商业事件与费用"'));
  expect(block).toContain('未知状态：已用 <span class="num" title="12.5">12.50</span> / <span class="num" title="100">100.00</span> CNY（12.5%）');   // two decimals, full value in the title (F15)
  expect(block).toContain('<span class="num" title="12.50">12.50</span> CNY，3 条');expect(block).toContain('其中 1 条未定价');
  const empty=render(client([[['workbench','overview'],overview({...base,commercial:{status:'ok',data:{budgets:{},last_24h:[]}}})]]),h(Overview,{onOpen:()=>{}}));
  expect(empty).toContain('暂无预算：还没有设置');expect(empty).toContain('暂无费用记录：最近 24 小时');
  expect(()=>overview({...base,commercial:{status:'ok',data:{budgets:{},last_24h:[{...served.data.last_24h[0],amount:'12,5'}]}}})).toThrow(WorkbenchError);
});
