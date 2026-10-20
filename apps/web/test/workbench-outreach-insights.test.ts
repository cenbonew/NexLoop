// Outreach slice 8: the owner's outreach metrics and evidence chain, parsed strictly and shown with Chinese labels (codes only in titles).
import {createElement as h} from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
import {expect,test} from 'vitest';
import {WorkbenchError,outreachEvidence,outreachEvidenceList,outreachMetrics} from '../src/workbench/api';
import {ChainBody,OutreachInsights,REFUSAL,SEND_STATE} from '../src/workbench/pages/OutreachInsights';

const metrics={computed_at:'2026-10-20T12:00:00+00:00',window_days:7,candidates:2,accepted_7d:3,pending_approval:1,approvals_7d:{approved:1,pending:1},
  sent_7d:2,fulfilled_7d:2,refused_7d:{frequency_capped:1,marketing_opted_out:1},by_purpose_7d:{commitment_followup:{accepted:2,sent:2,refused:0},marketing_general:{accepted:1,sent:0,refused:1}},
  frequency_capped_7d:1,marketing:{enabled:true,consent_mode:'opt_out',sent_7d:1,unsubscribes_7d:1,min_volume:20,unsubscribe_rate_7d:null,allowance_per_7d:1,consumers_at_allowance:1},
  consent:{granted_7d:1,withdrawn_7d:0,consumers_with_record:1,consumers_consented:1,consent_rate:1}};
const intent='outreach:'+'f'.repeat(64);
const chain={found:true,trigger:{origin_kind:'proactive',origin:'commitment',origin_ref:'commitment:synthetic-1',initiator:'human',run_id:null},
  request:{intent_id:intent,consumer_id:'a'.repeat(64),persona:'real',purpose_id:'commitment_followup',purpose_version:1,topic:'commitment_followup',frequency_class:'service',
    channel:'native.webchat',body_digest:'b'.repeat(64),body_chars:16,conversation_id:null,requested_at:'2026-10-20T11:00:00+00:00'},
  approval:{requested_at:'2026-10-20T11:00:00+00:00',expires_at:'2026-10-21T11:00:00+00:00',decision:'approved',decided_at:'2026-10-20T11:05:00+00:00',decided_by:'p2',requested_by:'p1'},
  gates:{stage:'refused',code:'frequency_capped',at:'2026-10-20T11:06:00+00:00',gates:[{gate:'contact_restricted',result:'pass',detail:null},{gate:'frequency',result:'refuse',detail:{last_24h:1}}]},
  dispatch:{state:'refused',fence:0,admitted_at:null,finished_at:'2026-10-20T11:06:00+00:00',provider_reference:null,provider:'loopback'},
  events:[{action:'nexloop.outreach.send.accepted',at:'2026-10-20T11:00:00+00:00'},{action:'nexloop.outreach.send.refused',at:'2026-10-20T11:06:00+00:00'}],
  responses:{optouts:[{kind:'unsubscribed',source:'refusal_rule',rule_version:2,rule_id:'unsubscribe',answers_this_send:true,at:'2026-10-20T11:10:00+00:00'}],consents:[],contact_restricted:false}};

test('parsers are strict: no body field, codes only, ratios in range',()=>{
  expect(outreachMetrics(metrics).marketing.unsubscribe_rate_7d).toBeNull();
  expect(()=>outreachMetrics({...metrics,marketing:{...metrics.marketing,unsubscribe_rate_7d:1.2}})).toThrow(WorkbenchError);
  expect(()=>outreachMetrics({...metrics,refused_7d:{'Bad Code':1}})).toThrow(WorkbenchError);
  expect(()=>outreachEvidence({...chain,request:{...chain.request,body:'您好'}})).toThrow(WorkbenchError);
  expect(outreachEvidence({found:false})).toEqual({found:false});
  expect(()=>outreachEvidenceList({synthetic_only:false,items:[{intent_id:'x'}]})).toThrow(WorkbenchError);
});

test('the chain shows every link in words, the codes only in titles',()=>{
  const html=renderToStaticMarkup(h(ChainBody,{e:outreachEvidence(chain)}));
  for(const part of ['触发','受理的请求','审批','派发闸门','派发结果','过程记录','客户的回应'])expect(html).toContain(`<h3>${part}</h3>`);
  expect(html).toContain('承诺');expect(html).toContain('已批准');expect(html).toContain('在派发时被拒');expect(html).toContain(`title="frequency_capped">${REFUSAL.frequency_capped}`);
  expect(html).toContain('联系频次');expect(html).toContain('退订营销');expect(html).toContain('回应的就是这条');expect(html).toContain('16 字');
  expect(html).toContain(SEND_STATE.refused);expect(html).not.toMatch(/>frequency_capped</);expect(html).not.toMatch(/>refused</);
  expect(renderToStaticMarkup(h(ChainBody,{e:{found:false}}))).toContain('找不到这条外发');
});

test('the metrics view: funnel, refusals by gate in words, the unsubscribe rate withheld below the minimum volume',()=>{
  const c=new QueryClient({defaultOptions:{queries:{retry:false,staleTime:Infinity}}});
  c.setQueryData(['workbench','outreach-metrics'],outreachMetrics(metrics));
  const html=renderToStaticMarkup(h(QueryClientProvider,{client:c},h(OutreachInsights)));
  expect(html).toContain('外发漏斗');expect(html).toContain('超过联系频次</span>：1');expect(html).toContain('客户已退订营销</span>：1');
  expect(html).toContain('不足 20 条营销发送，不计算');expect(html).toContain('已批准</span>：1');expect(html).toContain('承诺跟进');
  expect(html).not.toMatch(/>marketing_opted_out</);expect(html).toContain('演示（合成客户）');
});
