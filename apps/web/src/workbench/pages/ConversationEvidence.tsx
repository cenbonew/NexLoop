/**
 * K5 §2.4 / §2.5 (docs/design/k5-consumer-evidence-views.md): on the conversation page, the conversation's metrics card
 * (GET /conversations/{id}/metrics) and, per message, its processing trace (GET /messages/{id}/trace). World real only. Always the nine
 * steps in order; a step without a record says so (`none`), a step not applicable says so (`skipped`), a step the role may not read
 * says so (`restricted`) - never a made-up value. Amounts stay the server's decimal strings; model cost is the owner's.
 */
import {useState} from 'react';
import {useQuery} from '@tanstack/react-query';
import {kindOf,readConversationMetrics,readMessageTrace,type ConversationMetrics,type TraceStatus,type TraceStep} from '../api';
import {EmptyState,IdText,KeyValue} from '../kit';
import {StatusBadge,Time} from '../ui';
import type {Tone} from '../status-labels';

export const STEP_TEXT:Record<TraceStep['step'],string>={message:'消息',relay:'relay 转发',context:'上下文',run:'AI 运行',action:'受治理的 Action',
  receipt:'外部回执',extraction:'提取',matching:'匹配',ontology_update:'本体更新'};
export const STATUS_TEXT:Record<TraceStatus,string>={done:'完成',running:'进行中',failed:'失败',skipped:'不适用',none:'没有记录',restricted:'无读取权限'};
const STATUS_TONE:Record<TraceStatus,Tone>={done:'ok',running:'pending',failed:'fail',skipped:'neutral',none:'pending',restricted:'limited'};
/** What a `none` step means, said per step where the design names it (§2.7). */
export const NONE_TEXT:Partial<Record<TraceStep['step'],string>>={matching:'没有匹配记录',ontology_update:'没有本体更新提案'};
const NOT_REAL='只在真实世界提供；当前会话不在真实世界、正在删除或不存在。';

function stepDetail(s:TraceStep):string{
  if(s.status==='none')return `${NONE_TEXT[s.step]??'没有记录'}：应当发生但没有记录`;
  if(s.status==='skipped')return '这条消息不涉及这一步';
  if(s.status==='restricted')return '你的角色不能读取这一步';
  return s.code??'';
}

export function MessageTrace({messageId}:{messageId:string}){
  const q=useQuery({queryKey:['workbench','message-trace',messageId],queryFn:()=>readMessageTrace(messageId),retry:false});
  if(q.isPending)return <EmptyState kind="loading" subject="处理链路" compact/>;
  if(q.isError)return kindOf(q.error)==='not_found'?<p className="note">处理链路{NOT_REAL}</p>
    :<EmptyState kind={kindOf(q.error)==='forbidden'?'forbidden':'unavailable'} subject="处理链路" onRetry={()=>void q.refetch()} compact/>;
  return <ol className="trace-steps" aria-label="处理链路">{q.data.steps.map(s=><li key={s.step} data-step={s.step} data-status={s.status}>
    <span className="trace-name">{STEP_TEXT[s.step]}</span>
    <StatusBadge text={STATUS_TEXT[s.status]} tone={STATUS_TONE[s.status]} code={s.status}/>
    <span className="trace-time">{s.at?<Time value={s.at}/>:'—'}</span>
    <span className="note trace-detail">{stepDetail(s)?<span className="trace-note">{stepDetail(s)}</span>:null}
      {s.refs.length?<span className="trace-refs">{s.refs.map(r=><IdText key={r} kind={r.split(':')[0]} value={r.slice(r.indexOf(':')+1)}/>)}</span>:null}
      {s.more?<span className="trace-more">等另 {s.more} 个</span>:null}</span>
  </li>)}</ol>;
}

/** A message's 处理链路 toggle: the trace opens below the message (read only when opened). */
export function MessageTraceToggle({messageId}:{messageId:string}){
  const [open,setOpen]=useState(false);
  return <div className="trace-toggle"><button type="button" className="link" aria-expanded={open} onClick={()=>setOpen(!open)}>{open?'收起处理链路':'处理链路'}</button>
    {open?<MessageTrace messageId={messageId}/>:null}</div>;
}

function cost(m:ConversationMetrics):string{
  if(!m.model_cost)return m.withheld.includes('model_cost')?'仅负责人可见':'—';
  if(!m.model_cost.totals.length)return m.model_cost.unpriced_entries?`没有已定价的费用（未定价 ${m.model_cost.unpriced_entries} 条）`:'没有费用记录';
  return m.model_cost.totals.map(t=>`${t.amount} ${t.currency}${t.amount_unit==='minor'?'（最小单位）':''}`).join(' · ')
    +(m.model_cost.unpriced_entries?`；另有未定价 ${m.model_cost.unpriced_entries} 条`:'');
}

export function ConversationMetricsCard({conversationId}:{conversationId:string}){
  const q=useQuery({queryKey:['workbench','conversation-metrics',conversationId],queryFn:()=>readConversationMetrics(conversationId),retry:false});
  const head=<h2 id="conversation-metrics">本会话指标</h2>;
  if(q.isPending)return <section className="detail-section" aria-labelledby="conversation-metrics">{head}<EmptyState kind="loading" subject="本会话指标" compact/></section>;
  if(q.isError)return <section className="detail-section" aria-labelledby="conversation-metrics">{head}{kindOf(q.error)==='not_found'?<p className="note">本会话指标{NOT_REAL}</p>
    :<EmptyState kind={kindOf(q.error)==='forbidden'?'forbidden':'unavailable'} subject="本会话指标" onRetry={()=>void q.refetch()} compact/>}</section>;
  const m=q.data;
  return <section className="detail-section conversation-metrics" aria-labelledby="conversation-metrics">{head}
    <KeyValue label="本会话指标" items={[
      {key:'响应时间',value:m.response_ms?`中位 ${(m.response_ms.p50/1000).toFixed(1)} 秒 · P90 ${(m.response_ms.p90/1000).toFixed(1)} 秒`:'没有样本',hint:m.response_ms?`${m.response_ms.samples} 次回复`:undefined},
      {key:'模型费用',value:cost(m),hint:m.model_cost?`${m.model_cost.runs} 次运行`:undefined},
      {key:'抽取的 Claim 记录',value:m.claims,hint:'你可见的原始记录数；重复抽取修好之前，不等于事实条数'},
      {key:'承诺',value:m.commitments,hint:'你可见的'},
      {key:'闸门拦截',value:m.gate_blocks.total,hint:m.gate_blocks.by_code.length?m.gate_blocks.by_code.map(c=>`${c.code} ${c.count}`).join(' · '):undefined}]}/>
    <p className="note">统计于 <Time value={m.as_of}/></p>
  </section>;
}
