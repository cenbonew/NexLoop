/**
 * 实验空间, slice 1 (owner 2026-10-28): the owner's experiments, read only, from the experiments view (0370). A list (world, stage,
 * status, start and stop, stop reason) and one experiment's detail: strategies, guardrails and thresholds, budget and spend, allocation
 * counts, the latest frozen report's conclusion and digest, the twin and its calibration, and for a budget drill the notice that its
 * result does not represent the experiment's effect. Counts only: no customer appears here. Codes stay in titles, words on the page.
 * Slice 2: the owner registers, starts and stops an experiment through the governed Actions (register_experiment, start_experiment,
 * stop_experiment), each confirmed in place (ConfirmedForm); stopping needs the owner's own reason. An unknown result is reconciled
 * first: every read is refetched after a write, and a control is offered only while the experiment is still in the state it acts on
 * (start while registered, stop while started; registration while its id is not listed), so a write that did take effect closes its
 * control instead of offering a retry; otherwise the panel keeps 用同一请求重试 (the same Idempotency-Key).
 */
import {useState} from 'react';
import {useQuery} from '@tanstack/react-query';
import {readExperiments,type Experiment} from '../api';
import {DataTable} from '../DataTable';
import {Page} from './Page';
import {StatusBadge,Time,type Tone} from '../ui';
import {Card,ChoiceGroup,FormField,IdText,KeyValue,Notice} from '../kit';
import {ConfirmedForm,WithMember} from '../actions';
import {offers,type Me} from '../api';

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
    actions={<WithMember>{member=><Register member={member} listed={query.data?.map(x=>x.experiment_id)??null}/>}</WithMember>}
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
    {chosen?<><Detail x={chosen}/><WithMember>{member=><Operations key={chosen.experiment_id} member={member} x={chosen}/>}</WithMember></>:<p className="note">选择一个实验查看详情。</p>}
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

/** Start while registered, stop while started: the control follows the read state (an unknown result that took effect closes it). */
export function Operations({member,x}:{member:Me;x:Experiment}){
  const unknown='如果结果未知：列表会自动刷新，先看这个实验的状态；状态已经变了说明已生效，这里的按钮会消失，不要重试。';
  if(x.status==='registered')return <div className="experiment-operations" aria-label="实验操作">
    <ConfirmedForm key={'start:'+x.experiment_id} operation="start_experiment" member={member} trigger="开始实验" title="确认开始实验" withReason={false}
      impact={<>影响：从现在起按登记分配客户（{world(x)}世界，{STAGE_TEXT[x.stage]}）；护栏和预算会自动停止它。{x.budget_drill?DRILL_TEXT+'。':''}{unknown}</>}
      body={()=>({experiment_id:x.experiment_id})}/></div>;
  if(x.status==='started')return <div className="experiment-operations" aria-label="实验操作">
    <ConfirmedForm key={'stop:'+x.experiment_id} operation="stop_experiment" member={member} trigger="停止实验" title="确认停止实验" tone="danger" reasonLabel="停止原因"
      impact={<>影响：立即停止，所有客户回到对照策略；已发生的花费和分配记录保留，停止后不能重新开始这个实验。{unknown}</>}
      body={reason=>({experiment_id:x.experiment_id,reason})}/></div>;
  return null;
}

export type Draft={experiment_id:string;stage:Experiment['stage'];world:string;hypothesis:string;candidate:string;candidate_version:string;control:string;control_version:string;
  metric:string;direction:'increase'|'decrease';refusal_delta:string;sample_candidate:string;sample_control:string;window_days:string;model_cny:string;incentive_cny:string;
  ends_at:string;twin:string;twin_version:string;seed:string;budget_drill:boolean};
export const EMPTY_DRAFT:Draft={experiment_id:'',stage:'screening',world:'',hypothesis:'',candidate:'',candidate_version:'1',control:'',control_version:'1',metric:'',direction:'increase',
  refusal_delta:'0.02',sample_candidate:'',sample_control:'',window_days:'14',model_cny:'',incentive_cny:'0',ends_at:'',twin:'',twin_version:'1',seed:'',budget_drill:false};
const SLUG=/^[a-z][a-z0-9_.-]{0,63}$/;
const int=(v:string)=>/^[1-9][0-9]{0,8}$/.test(v)?Number(v):null;
const dec=(v:string)=>/^[0-9]+(\.[0-9]+)?$/.test(v)?Number(v):null;
/** The registration from the draft (nexloop-experiment-registration/1), or what is still missing. The server checks it all again. */
export function registrationOf(d:Draft):{registration:Record<string,unknown>}|{missing:string}{
  if(!SLUG.test(d.experiment_id))return {missing:'实验编号（小写字母开头，可含数字 . _ -）'};
  if(!d.world.trim())return {missing:'世界'};
  if(!d.hypothesis.trim())return {missing:'假设'};
  if(!SLUG.test(d.candidate)||!int(d.candidate_version)||!SLUG.test(d.control)||!int(d.control_version))return {missing:'候选和对照策略及版本'};
  if(!d.metric.trim())return {missing:'主要指标'};
  const delta=dec(d.refusal_delta),sc=int(d.sample_candidate),sk=int(d.sample_control),w=int(d.window_days),m=dec(d.model_cny),i=dec(d.incentive_cny),seed=int(d.seed);
  if(delta===null)return {missing:'拒绝或退订率护栏'};
  if(!sc||!sk)return {missing:'计划样本'};
  if(!w)return {missing:'成熟窗口天数'};
  if(m===null||i===null)return {missing:'预算'};
  // The input is Beijing time (as every time on the workbench), whatever the browser's zone: +08:00, no daylight saving.
  const ends=/^\d{4}-\d\d-\d\dT\d\d:\d\d$/.test(d.ends_at)?new Date(d.ends_at+':00+08:00'):null;if(!ends||Number.isNaN(ends.getTime()))return {missing:'结束时间'};
  const twin=d.twin.trim()?(int(d.twin_version)?{twin_id:d.twin.trim(),version:int(d.twin_version)}:undefined):null;
  if(twin===undefined)return {missing:'孪生版本'};
  if(!seed)return {missing:'分配种子'};
  return {registration:{schema:'nexloop-experiment-registration/1',experiment_id:d.experiment_id,stage:d.stage,world:d.world.trim(),hypothesis:d.hypothesis.trim(),
    candidate:{strategy_id:d.candidate,version:int(d.candidate_version)},control:{strategy_id:d.control,version:int(d.control_version)},
    primary_metric:{key:d.metric.trim(),direction:d.direction},guardrails:{refusal_or_unsubscribe_rate_delta:delta},sample_size:{candidate:sc,control:sk},
    maturity:{window_days:w},budget:{model_cny:m,incentive_cny:i},ends_at:ends.toISOString().replace(/\.\d{3}Z$/,'Z'),twin,allocation_seed:seed,
    ...(d.budget_drill?{budget_drill:true}:{})}};
}

/** Preregistration: the fields, then one in-place confirmation (the registration is frozen once accepted). */
export function Register({member,listed}:{member:Me;listed:string[]|null}){
  const [open,setOpen]=useState(false);const [d,setD]=useState<Draft>(EMPTY_DRAFT);
  if(!offers(member,'register_experiment'))return null;
  if(!open)return <button type="button" onClick={()=>setOpen(true)}>登记新实验</button>;
  const set=<K extends keyof Draft>(k:K)=>(v:Draft[K])=>setD({...d,[k]:v});
  const text=(k:keyof Draft,label:string,hint?:string,type='text')=><FormField id={'exp-'+k} label={label} hint={hint}>
    <input id={'exp-'+k} type={type} value={String(d[k])} onChange={e=>set(k)(e.target.value as never)}/></FormField>;
  const r=registrationOf(d);
  const taken=listed?.includes(d.experiment_id)??false;
  return <section className="experiment-register" aria-label="登记新实验"><h2>登记新实验</h2>
    {text('experiment_id','实验编号','登记后不能修改')}
    <ChoiceGroup legend="阶段" name="exp-stage" value={d.stage} onChange={set('stage')}
      options={(Object.keys(STAGE_TEXT) as Experiment['stage'][]).map(k=>({value:k,label:STAGE_TEXT[k]}))}/>
    {text('world','世界','孪生筛选用仿真世界，影子用影子世界，线上用 real')}{text('hypothesis','假设')}
    {text('candidate','候选策略')}{text('candidate_version','候选策略版本')}{text('control','对照策略')}{text('control_version','对照策略版本')}
    {text('metric','主要指标')}
    <ChoiceGroup legend="指标方向" name="exp-direction" value={d.direction} onChange={set('direction')}
      options={[{value:'increase',label:'越高越好'},{value:'decrease',label:'越低越好'}]}/>
    {text('refusal_delta',GUARDRAIL_TEXT.refusal_or_unsubscribe_rate_delta,'例如 0.02 表示高出 2 个百分点即停止')}
    {text('sample_candidate','候选组样本')}{text('sample_control','对照组样本')}{text('window_days','成熟窗口（天）')}
    {text('model_cny','模型费用上限（元）')}{text('incentive_cny','激励费用上限（元）')}{text('ends_at','结束时间','按北京时间填写，提交时换算为 UTC','datetime-local')}
    {text('twin','孪生（线上可留空）')}{text('twin_version','孪生版本')}{text('seed','分配种子','正整数，决定分组，登记后固定')}
    <label className="choice"><input type="checkbox" checked={d.budget_drill} onChange={e=>set('budget_drill')(e.target.checked)}/>预算演练（只用于演示预算用尽；仿真世界、孪生筛选、上限不超过 1 元、一小时内结束）</label>
    {d.budget_drill?<Notice tone="warning">{DRILL_TEXT}</Notice>:null}
    {taken?<p className="note" role="status">这个编号已在列表中：已登记（如果刚才结果未知，说明登记已生效，不要重试）。</p>
      :'missing' in r?<p className="disabled-reason">还需要填写：{r.missing}</p>
      :<ConfirmedForm operation="register_experiment" member={member} trigger="登记" title="确认登记实验" withReason={false}
        impact={<>影响：登记内容冻结后不能修改；登记不等于开始，开始需要另行确认。如果结果未知：列表会自动刷新，先看这个编号是否已出现，出现就不要重试。</>}
        body={()=>({registration:r.registration})}/>}
    <button type="button" className="btn-secondary" onClick={()=>setOpen(false)}>收起</button>
  </section>;
}
