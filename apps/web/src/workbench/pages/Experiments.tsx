/**
 * 实验空间, slice 1 (owner 2026-10-28): the owner's experiments, read only, from the experiments view (0370). A list (world, stage,
 * status, start and stop, stop reason) and one experiment's detail: strategies, guardrails and thresholds, budget and spend, allocation
 * counts, the latest frozen report's conclusion and digest, the twin and its calibration, and for a budget drill the notice that its
 * result does not represent the experiment's effect. Counts only: no customer appears here. Codes stay in titles, words on the page.
 */
import {useState} from 'react';
import {useQuery} from '@tanstack/react-query';
import {readExperiments,type Experiment} from '../api';
import {DataTable} from '../DataTable';
import {Page} from './Page';
import {StatusBadge,Time,type Tone} from '../ui';
import {Card,IdText,KeyValue,Notice} from '../kit';

export const STAGE_TEXT:Record<Experiment['stage'],string>={screening:'孪生筛选',shadow:'影子',online:'线上'};
export const STATUS_TEXT:Record<Experiment['status'],string>={registered:'已登记，未开始',started:'进行中',stopped:'已停止'};
const STATUS_TONE:Record<Experiment['status'],Tone>={registered:'neutral',started:'pending',stopped:'neutral'};
export const WORLD_TEXT:Record<string,string>={real:'真实',simulation:'仿真',shadow:'影子',test:'测试'};
export const GUARD_TEXT:Record<string,string>={budget_exhausted:'预算用尽',ended:'到达结束时间',purpose_not_allowed:'沟通目的不再允许',
  reconciliation_anomaly:'对账异常超过阈值',refusal_rate:'拒绝或退订率超过阈值',twin_withdrawn:'孪生版本已撤回'};
export const GUARDRAIL_TEXT:Record<string,string>={refusal_or_unsubscribe_rate_delta:'拒绝或退订率比对照组高出',reconciliation_anomalies:'对账异常数上限'};
export const CALIBRATION_TEXT:Record<string,string>={passed:'已校准',failed:'校准未通过',uncalibrated:'未校准'};
export const TWIN_STATUS_TEXT:Record<string,string>={published:'已发布',retired:'已退役',draft:'草稿'};
export const CONCLUSION_TEXT:Record<string,string>={reported:'已出结论',not_reported:'样本不足，暂无结论'};
/** The drill's notice, as the owner wants it worded (NX-043 item 10): the server's notice stays in the title. */
export const DRILL_TEXT='预算演练，孪生未校准，结论不代表实验效果';

/** The stop reason in words: the guard's code mapped (code in the title), an owner's own reason as written. */
export function stopReason(x:Pick<Experiment,'stop_reason'|'stopped_by_guard'>):{text:string;code?:string}|null{
  if(!x.stop_reason)return null;
  if(x.stopped_by_guard){const code=x.stop_reason.replace(/^guardrail: /,'');return {text:'护栏：'+(GUARD_TEXT[code]??'其他原因'),code};}
  return {text:'负责人：'+x.stop_reason};
}
const cny=(v:number)=>`¥${v.toFixed(2)}`;
const strategy=(s:Experiment['candidate'])=><span title={`${s.strategy_id} v${s.version}`}>{s.strategy_id} · 第 {s.version} 版</span>;
const world=(x:Experiment)=>x.world_kind?WORLD_TEXT[x.world_kind]??'其他':'未知';

export function Experiments({title}:{title:string}){
  const query=useQuery({queryKey:['workbench','experiments'],queryFn:readExperiments});
  return <Page title={title} query={query} empty={rows=>rows.length===0}
    lead="负责人的实验（只读）：登记、开始与停止、护栏、预算和报告摘要。只显示计数，不显示任何客户。"
    forbidden={<p>实验空间只对负责人开放。</p>}
    announce={rows=>`实验：${rows.length} 个`}>{rows=><Body rows={rows}/>}</Page>;
}

function Body({rows}:{rows:Experiment[]}){
  const [open,setOpen]=useState<string|null>(null);
  const chosen=rows.find(x=>x.experiment_id===open)??null;
  return <>
    <DataTable label="实验列表" rows={rows} rowKey={x=>x.experiment_id} columns={[
      {label:'实验',main:true,cell:x=><button type="button" className="link" aria-expanded={open===x.experiment_id}
        onClick={()=>setOpen(open===x.experiment_id?null:x.experiment_id)}>{x.hypothesis??x.experiment_id}</button>},
      {label:'世界',cell:x=><span title={x.world}>{world(x)}</span>},
      {label:'阶段',cell:x=><span title={x.stage}>{STAGE_TEXT[x.stage]}{x.budget_drill?<> <StatusBadge text="预算演练" tone="limited" code="budget_drill"/></>:null}</span>},
      {label:'状态',cell:x=><StatusBadge text={STATUS_TEXT[x.status]} tone={STATUS_TONE[x.status]} code={x.status}/>},
      {label:'开始',cell:x=><Time value={x.started_at}/>},
      {label:'停止',cell:x=><Time value={x.stopped_at}/>},
      {label:'停止原因',cell:x=>{const r=stopReason(x);return r?<span title={r.code}>{r.text}</span>:'—';}}]}/>
    {chosen?<Detail x={chosen}/>:<p className="note">选择一个实验查看详情。</p>}
  </>;
}

export function Detail({x}:{x:Experiment}){
  const r=stopReason(x);
  return <section aria-label="实验详情" className="experiment-detail">
    {x.budget_drill?<Notice tone="warning" title="预算演练"><span title={x.notice??undefined}>{DRILL_TEXT}</span></Notice>:null}
    <Card title={x.hypothesis??x.experiment_id} badge={<StatusBadge text={STATUS_TEXT[x.status]} tone={STATUS_TONE[x.status]} code={x.status}/>}>
      <KeyValue label="登记" items={[
        {key:'编号',value:<IdText value={x.experiment_id} head={24}/>},
        {key:'世界',value:<>{world(x)} <IdText value={x.world}/></>},
        {key:'阶段',value:STAGE_TEXT[x.stage]},
        {key:'候选策略',value:strategy(x.candidate)},{key:'对照策略',value:strategy(x.control)},
        {key:'主要指标',value:x.primary_metric?<span title={x.primary_metric.key}>{x.primary_metric.key}（{x.primary_metric.direction==='increase'?'越高越好':'越低越好'}）</span>:'—'},
        {key:'计划样本',value:x.sample_size?`候选 ${x.sample_size.candidate}，对照 ${x.sample_size.control}`:'—'},
        {key:'登记时间',value:<Time value={x.registered_at}/>},{key:'结束时间',value:<Time value={x.ends_at}/>},
        {key:'开始',value:<Time value={x.started_at}/>},{key:'停止',value:<Time value={x.stopped_at}/>},
        {key:'停止原因',value:r?<span title={r.code}>{r.text}</span>:'—'},
        {key:'登记摘要',value:<IdText value={x.registration_digest}/>}]}/>
    </Card>
    <Card title="护栏">
      {Object.keys(x.guardrails).length?<KeyValue label="护栏阈值" items={Object.entries(x.guardrails).sort().map(([k,v])=>({key:GUARDRAIL_TEXT[k]??'其他护栏',value:<span title={k}>{v}</span>}))}/>
        :<p className="note">没有登记额外护栏；预算和对账异常护栏始终生效。</p>}
    </Card>
    <Card title="预算与分配">
      <KeyValue label="预算" items={[
        {key:'模型费用',value:`已用 ${cny(x.spent.model_cny)} / 上限 ${cny(x.budget.model_cny)}`},
        {key:'激励费用',value:`已用 ${cny(x.spent.incentive_cny)} / 上限 ${cny(x.budget.incentive_cny)}`},
        {key:'已分配客户',value:`候选 ${x.allocation.candidate}，对照 ${x.allocation.control}，排除 ${x.allocation.excluded}`}]}/>
    </Card>
    <Card title="最新报告">
      {x.report?<KeyValue label="报告摘要" items={[
        {key:'结论',value:<span title={x.report.conclusion??undefined}>{x.report.conclusion?CONCLUSION_TEXT[x.report.conclusion]??'其他':'—'}</span>},
        {key:'截至',value:<Time value={x.report.as_of}/>},{key:'成熟样本',value:`候选 ${x.report.candidate_matured}，对照 ${x.report.control_matured}`},
        {key:'报告摘要值',value:<IdText value={x.report.report_digest}/>}]}/>
        :<p className="note">还没有冻结的报告。</p>}
      {x.budget_drill&&x.report?<p className="note">{DRILL_TEXT}。</p>:null}
    </Card>
    <Card title="客户孪生">
      {x.twin?<KeyValue label="孪生" items={[
        {key:'版本',value:<span title={x.twin.twin_id}>{x.twin.twin_id} · 第 {x.twin.version} 版</span>},
        {key:'状态',value:<span title={x.twin.status??undefined}>{x.twin.status?TWIN_STATUS_TEXT[x.twin.status]??'其他':'未知'}</span>},
        {key:'校准',value:<span title={x.twin.calibration??undefined}>{x.twin.calibration?CALIBRATION_TEXT[x.twin.calibration]??'其他':'未知'}</span>}]}/>
        :<p className="note">这个实验不使用孪生。</p>}
    </Card>
  </section>;
}
