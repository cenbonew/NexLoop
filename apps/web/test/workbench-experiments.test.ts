// Experiment space slice 1 (0370): the owner's experiments parsed strictly and shown as a list and one detail; states, the drill notice.
import {createElement as h} from 'react';
import {readFileSync} from 'node:fs';
import {renderToStaticMarkup} from 'react-dom/server';
import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
import {expect,test} from 'vitest';
import {WorkbenchError,experiments} from '../src/workbench/api';
import {DRILL_TEXT,Detail,Experiments,stopReason} from '../src/workbench/pages/Experiments';

const digest='a'.repeat(64);
const drill={experiment_id:'budget.drill',world:'sim:00000000-0000-4000-8000-000000000001',world_kind:'simulation',stage:'screening',
  hypothesis:'预算耗尽后新的运行被拒',candidate:{strategy_id:'followup.later',version:1},control:{strategy_id:'followup.default',version:1},
  primary_metric:{key:'reply_within_24h',direction:'increase'},guardrails:{refusal_or_unsubscribe_rate_delta:0.02},sample_size:{candidate:20,control:20},
  budget:{model_cny:1,incentive_cny:0},ends_at:'2026-10-28T12:00:00+00:00',registration_digest:digest,registered_at:'2026-10-28T11:00:00+00:00',
  status:'stopped',started_at:'2026-10-28T11:01:00+00:00',stopped_at:'2026-10-28T11:05:00+00:00',stop_reason:'guardrail: budget_exhausted',stopped_by_guard:true,
  spent:{model_cny:1,incentive_cny:0},allocation:{candidate:0,control:0,excluded:0},report:null,budget_drill:true,
  notice:"budget drill: the twin is not calibrated; the result does not represent the experiment's effect",
  twin:{twin_id:'trial.service_consumer',version:1,status:'published',calibration:'uncalibrated'}};
const plain={...drill,experiment_id:'plain.screening',hypothesis:'回访延后到 48 小时',status:'registered',started_at:null,stopped_at:null,stop_reason:null,
  stopped_by_guard:false,budget_drill:false,notice:null,spent:{model_cny:0,incentive_cny:0}};

function render(body?:unknown,error?:WorkbenchError){
  const c=new QueryClient({defaultOptions:{queries:{retry:false,retryOnMount:false,staleTime:Infinity}}});
  if(body!==undefined)c.setQueryData(['workbench','experiments'],experiments(body));
  if(error)c.getQueryCache().build(c,{queryKey:['workbench','experiments'],retryOnMount:false}).setState({status:'error',error,fetchStatus:'idle',errorUpdatedAt:Date.now()});
  return renderToStaticMarkup(h(QueryClientProvider,{client:c},h(Experiments,{title:'实验空间'})));
}

test('the parser is exact: an unknown field, an unknown stage or status, a bad digest or a negative count is refused',()=>{
  expect(experiments({experiments:[drill,plain]}).map(x=>x.experiment_id)).toEqual(['budget.drill','plain.screening']);
  for(const bad of [{...drill,consumer_id:'x'},{...drill,stage:'pilot'},{...drill,status:'paused'},{...drill,registration_digest:'xyz'},
    {...drill,allocation:{candidate:-1,control:0,excluded:0}},{...drill,spent:{model_cny:'1',incentive_cny:0}}])
    expect(()=>experiments({experiments:[bad]})).toThrow(WorkbenchError);
  expect(()=>experiments({experiments:[],extra:1})).toThrow(WorkbenchError);
});

test('loading, empty and failure are their own states, never an empty list shown as data',()=>{
  expect(render()).toContain('正在加载');
  const empty=render({experiments:[]});expect(empty).toContain('暂无记录');expect(empty).not.toContain('实验列表');
  const failed=render(undefined,new WorkbenchError('unavailable'));expect(failed).toContain('role="alert"');expect(failed).toContain('重试');
  const forbidden=render(undefined,new WorkbenchError('forbidden'));expect(forbidden).toContain('实验空间只对负责人开放');expect(forbidden).not.toContain('实验列表');
});

test('the list shows world, stage, status, times and the stop reason in words, the drill marked; codes only in titles',()=>{
  const html=render({experiments:[drill,plain]});
  expect(html).toContain('aria-label="实验列表"');expect(html).toContain('预算耗尽后新的运行被拒');expect(html).toContain('仿真');expect(html).toContain('孪生筛选');
  expect(html).toContain('已停止');expect(html).toContain('已登记，未开始');expect(html).toContain('护栏：预算用尽');expect(html).toContain('title="budget_exhausted"');
  expect(html).toContain('预算演练');expect(html).not.toMatch(/>guardrail: /);expect(html).not.toMatch(/>budget_exhausted</);expect(html).toContain('选择一个实验查看详情');
  expect(html).not.toMatch(/<form|<textarea/);  // slice 1 is read only
});

test('a drill detail carries the notice; strategies, guardrails, budget and spend, allocation and the twin in words',()=>{
  const [d]=experiments({experiments:[drill]});
  const html=renderToStaticMarkup(h(Detail,{x:d}));
  expect(html).toContain(DRILL_TEXT);expect(DRILL_TEXT).toBe('预算演练，孪生未校准，结论不代表实验效果');
  expect(html).toContain('followup.later · 第 1 版');expect(html).toContain('拒绝或退订率比对照组高出');expect(html).toContain('0.02');
  expect(html).toContain('已用 ¥1.00 / 上限 ¥1.00');expect(html).toContain('候选 0，对照 0，排除 0');expect(html).toContain('未校准');expect(html).toContain('还没有冻结的报告');
  const [p]=experiments({experiments:[{...plain,report:{as_of:'2026-10-28T00:00:00+00:00',created_at:'2026-10-28T00:01:00+00:00',report_digest:digest,
    conclusion:'not_reported',reason:'insufficient_sample',candidate_matured:3,control_matured:4}}]});
  const other=renderToStaticMarkup(h(Detail,{x:p}));
  expect(other).not.toContain(DRILL_TEXT);expect(other).toContain('样本不足，暂无结论');expect(other).toContain('候选 3，对照 4');
});

test('an owner stop keeps the owner reason; an unknown guard code is not shown raw',()=>{
  expect(stopReason({stop_reason:'负责人手动停止',stopped_by_guard:false})).toEqual({text:'负责人：负责人手动停止'});
  expect(stopReason({stop_reason:'guardrail: new_code',stopped_by_guard:true})).toEqual({text:'护栏：其他原因',code:'new_code'});
  expect(stopReason({stop_reason:null,stopped_by_guard:false})).toBeNull();
});

test('batch 52: the lead matches the page - an owner registers experiments here, so it does not say read only',()=>{
  const html=render({experiments:[]});
  expect(html).not.toContain('（只读）');expect(html).toContain('在这里登记新实验');
});

// Stage walkthrough (release 53): the experiments list came back with all three contract guardrails and the page refused it as invalid.
const stageShaped={...plain,experiment_id:'stage.shaped',budget_drill:false,world_kind:'simulation',
  guardrails:{refusal_or_unsubscribe_rate_delta:0.02,reconciliation_anomalies:0,stop_on_budget_exhausted:true},
  twin:{twin_id:'trial.service_consumer',version:1,status:'published',calibration:'uncalibrated'}};
test('stage shape: 25 keys with the three contract guardrails parse; each guardrail is checked by its own type and unknown ones are refused',()=>{
  expect(Object.keys(stageShaped)).toHaveLength(25);
  const [x]=experiments({experiments:[stageShaped]});
  expect(x.guardrails).toEqual({refusal_or_unsubscribe_rate_delta:0.02,reconciliation_anomalies:0,stop_on_budget_exhausted:true});
  expect(experiments({experiments:[{...stageShaped,guardrails:{}}]})[0].guardrails).toEqual({});
  for(const g of [{stop_on_budget_exhausted:false},{stop_on_budget_exhausted:'true'},{stop_on_budget_exhausted:1},{reconciliation_anomalies:1},
    {reconciliation_anomalies:false},{refusal_or_unsubscribe_rate_delta:0},{refusal_or_unsubscribe_rate_delta:0.03},{refusal_or_unsubscribe_rate_delta:true},
    {max_contacts:3},{refusal_or_unsubscribe_rate_delta:0.01,other:true}])
    expect(()=>experiments({experiments:[{...stageShaped,guardrails:g}]})).toThrow(WorkbenchError);
});

test('the detail names the three guardrails in words; the boolean one reads 预算用尽即停止',()=>{
  const [x]=experiments({experiments:[stageShaped]});
  const html=renderToStaticMarkup(h(Detail,{x}));
  expect(html).toContain('拒绝或退订率比对照组高出');expect(html).toContain('0.02（即停止）');
  expect(html).toContain('对账异常');expect(html).toContain('出现任何一条即停止');
  expect(html).toContain('预算用尽即停止');expect(html).not.toMatch(/>true<|>stop_on_budget_exhausted<|>reconciliation_anomalies</);
  const list=render({experiments:[stageShaped,drill]});expect(list).toContain('aria-label="实验列表"');expect(list).not.toContain('服务器响应格式不符');
});

// L6 S1: driven by the contract itself - examples/experiment-registration.full-guardrails.valid.json carries all three guardrails of
// nexloop-experiment-registration/1 (stop_on_budget_exhausted true among them); the row takes the example's registration as it is.
test('contract-driven: the full-guardrails registration example parses with every guardrail; the parser accepts exactly the schema keys',()=>{
  const read=(p:string)=>JSON.parse(readFileSync(new URL('../../../packages/contracts/'+p,import.meta.url),'utf8'));
  const schema=read('experiment-registration.schema.json'),example=read('examples/experiment-registration.full-guardrails.valid.json');
  expect(Object.keys(example.guardrails).sort()).toEqual(Object.keys(schema.properties.guardrails.properties).sort());
  expect(example.guardrails.stop_on_budget_exhausted).toBe(true);
  const registered=Object.fromEntries(Object.entries(example).filter(([k])=>k in stageShaped));
  const [x]=experiments({experiments:[{...stageShaped,...registered,twin:stageShaped.twin}]});
  expect(x.guardrails).toEqual(example.guardrails);expect(x.experiment_id).toBe(example.experiment_id);expect(x.hypothesis).toBe(example.hypothesis);
  expect(()=>experiments({experiments:[{...stageShaped,...registered,twin:stageShaped.twin,guardrails:{...example.guardrails,not_in_contract:true}}]})).toThrow(WorkbenchError);
  const html=renderToStaticMarkup(h(Detail,{x}));expect(html).toContain('预算用尽即停止');expect(html).toContain('出现任何一条即停止');
});
