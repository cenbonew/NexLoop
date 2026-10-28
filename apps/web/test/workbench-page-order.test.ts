// Web pages batch 6 (owner walkthrough 2026-10-18): F02 facts before operations, F06 explicit scope, F07 alerts in business words
// with units, thresholds and a place to go, F11 audit objects as type + short ID.
import {createElement as h,type ReactNode} from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
import {expect,test} from 'vitest';
import {me,type Alerts as AlertsData} from '../src/workbench/api';
import {GOVERNED_SLOTS,controlImpact} from '../src/workbench/actions';
import {WorkbenchPage} from '../src/workbench/WorkbenchApp';
import {outcomeText} from '../src/workbench/RealDispatchPanel';
import {DELIVER} from '../src/workbench/realDispatch';
import {sinceText,valueText,ALERT_INFO} from '../src/workbench/pages/alertInfo';

const id=(c:string)=>c.repeat(64);
const OWNER=me({principal_id:'synthetic-staff-owner',role:'owner',actions:['nexloop.workbench.read','Control.set','Commitment.cancel','Commitment.extend','Commitment.attest',
  'Commitment.mark_communication','nexloop.service.query_request','nexloop.plan.request_reevaluation','nexloop.alert.silence'].map(a=>`eios:action:${a}:1`)});
function page(seed:[unknown[],unknown][],route:Parameters<typeof WorkbenchPage>[0]['route']){
  const c=new QueryClient({defaultOptions:{queries:{retry:false,staleTime:Infinity}}});
  for(const [key,value] of [[['workbench','me'],OWNER] as [unknown[],unknown],...seed])c.setQueryData(key,value);
  return renderToStaticMarkup(h(QueryClientProvider,{client:c},h(WorkbenchPage,{route,go:()=>{},slots:GOVERNED_SLOTS})) as ReactNode);
}
const before=(html:string,a:string,b:string)=>{const i=html.indexOf(a),j=html.indexOf(b);expect(i,a).toBeGreaterThanOrEqual(0);expect(j,b).toBeGreaterThanOrEqual(0);return i<j;};

test('F02: commitment detail reads body → state → evidence → operations; each operation has its own reason and object',()=>{
  const view={commitment_id:id('1'),consumer_id:id('2'),revision:1,status:'open',properties:{status:'open',predicate:'周五前寄出发票'},quote:'我周五前把发票寄给您',quote_status:'ok',
    origin:'claim',evidence:{requested:[],delivered:[],customer_confirmed:[],problem_resolved:'unavailable'},events:[],exceptions:[]};
  const html=page([[['workbench','commitment',id('1')],view]],{page:'commitments',id:id('1')});
  expect(before(html,'承诺正文','当前状态')&&before(html,'当前状态','<h2>证据')&&before(html,'<h2>证据','对这条承诺的操作')).toBe(true);
  expect(html).not.toContain('各项操作共用');
  for(const op of ['cancel_commitment','extend_commitment','attest_commitment','mark_commitment_communication'])expect(html).toContain(`id="${op}-reason"`);
  expect(html.match(/对象：承诺 “周五前寄出发票”/g)?.length).toBe(4);
});

test('F02: no task, no form: zero pending Actions, no plans, nothing firing',()=>{
  const actions=page([[['workbench','actions',''],{intents:[],unknown:{count:0,oldest_age_seconds:null}}],[['workbench','actions','unknown'],{intents:[],unknown:{count:0,oldest_age_seconds:null}}]],{page:'actions'});
  expect(actions).toContain('当前没有待核对的 Action，无需处理');expect(actions).not.toContain('<textarea');
  const plans=page([[['workbench','plans',''],[]]],{page:'plans'});
  expect(plans).toContain('计划在目标发布后');expect(plans).not.toContain('<textarea');expect(plans).not.toContain('aria-label="手动复评"');
  const alerts:AlertsData={firing:[],events:[],silences:[],evaluator:{last_evaluated_at:null,rules_version:5,stale:false}};
  const html=page([[['workbench','alerts'],alerts]],{page:'alerts'});
  expect(html).toContain('暂无触发中的告警');expect(html).not.toContain('<textarea');
});

test('F06: pause/resume has no default scope or operation and says its impact; dispatch buttons name their layers',()=>{
  const goals=page([[['workbench','goals'],{goals:[],control:{revision:3,scopes:[],events:[]},budgets:[]}]],{page:'goals'});
  expect(goals).toContain('当前没有已发布目标');expect(before(goals,'控制事件','暂停 / 恢复')).toBe(true);
  expect(goals).toContain('<option value="" selected="">请选择范围</option>');expect(goals).not.toContain('checked=""');expect(goals).toContain('请先选择范围和操作');
  expect(controlImpact('tenant','*',true)).toBe('将暂停 整个租户（所有客户） 的主动联系：之后的派发会被拒绝，来信必回不受影响。');
  expect(controlImpact('consumer','',true)).toBeNull();
  // Batch 52: a customer hash in the impact is an IdText with its copy button, never a hand-cut 'xxxx…'.
  const hex='d'.repeat(64);const said=renderToStaticMarkup(h('p',null,controlImpact('consumer',hex,true)));
  expect(said).toMatch(new RegExp(`^<p>将暂停 客户 <span class="id-text" data-id="${hex}"><code[^>]*>dddddddd…dddd</code><button type="button" class="id-copy"[^>]*>⧉</button></span> 的主动联系：`));
  expect(controlImpact('role','support',false)).toBe('将恢复 角色 support 的主动联系：暂停期间被拒的意图不会重放，计划将重新评估。');expect(controlImpact('','*',true)).toBeNull();
  const tenant={enabled:true,data_mode:'real',revision:2};
  expect(outcomeText({world:'real',tenant,actions:[{action_type:DELIVER,enabled:true,revision:1}]}).dispatchable).toBe('unconfirmed');
  expect(outcomeText({world:'real',tenant,actions:[]}).text).toBe('真实交付：不可派发（交付 Action 层未开启）。');
});

test('F07: a firing alert in business words, with unit, threshold, duration and where to go; firing before the silence form',()=>{
  const now=Date.parse('2026-10-18T10:00:00Z');
  expect(valueText(ALERT_INFO.feed_backlog_age,'171178')).toBe('48 小时（171178 秒）');
  expect(sinceText('2026-10-18T08:00:00+00:00',now)).toBe('2 小时');
  const alerts:AlertsData={firing:[{dedupe_key:'feed_backlog_age:commitment-monitor',rule_id:'feed_backlog_age',severity:'warning',selector:'commitment-monitor',value:'171178',
    first_fired_at:'2026-10-17T14:33:00+00:00',last_fired_at:'2026-10-18T09:59:00+00:00',fire_count:3,silenced_until:null}],
    events:[{event_id:9,rule_id:'feed_backlog_age',severity:'warning',kind:'firing',selector:'commitment-monitor',value:'171178',threshold:'600',recorded_at:'2026-10-18T09:59:00+00:00'}],
    silences:[],evaluator:{last_evaluated_at:'2026-10-18T09:59:00+00:00',rules_version:5,stale:false}};
  const html=page([[['workbench','alerts'],alerts]],{page:'alerts'});
  expect(html).toContain('后台 feed 积压');expect(html).toContain('<code class="tech-name alert-code">feed_backlog_age</code>');expect(html).toContain('48 小时（171178 秒）');
  expect(html).toContain('阈值 10 分钟（600 秒）');expect(html).toContain('href="/workbench/operations"');expect(html).toContain('commitment-monitor');
  expect(before(html,'正在触发的告警','静默告警')).toBe(true);
});

test('F11: audit objects as type + short ID (full value in the title, copy button), members by role + alias, no wrapping time',()=>{
  const items=[{category:'read',action:'read.contact',principal_id:'synthetic-staff-owner-0123456789',role:'owner',target_kind:'consumer',target_ref:id('a'),intent_id:null,occurred_at:'2026-10-18T09:00:00+00:00'}];
  const html=page([[['workbench','human-actions'],items]],{page:'audit'});
  // C1: the kit's IdText (type in words, head…tail, the full value in the title, expandable, a copy button).
  expect(html).toMatch(/<span class="id-kind">[^<a-z]+<\/span>/);expect(html).toContain(` ${id('a')}"`);expect(html).toContain('aaaaaaaa…aaaa');expect(html).not.toContain(`>${id('a')}<`);
  expect(html).toContain('复制');expect(html).toMatch(/负责人 · <span class="id-text" data-id="synthetic-staff-owner-0123456789"><code[^>]*>synthe…6789<\/code><button type="button" class="id-copy"/);expect(html).toMatch(/<time dateTime="[^"]+" title="北京时间 [^"]+">2026/);expect(html).toMatch(/title="read" data-code="read">.*?读取<\/span>/);
});
