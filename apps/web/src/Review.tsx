import {useState} from 'react';
import {useQuery} from '@tanstack/react-query';
import {DECISION_LABELS,KIND_LABELS,readCandidate,readQueue,reviewError,type CandidateDetail,type Decisions,type QueueItem} from './review-api';

const percent=(value:number)=>`${Math.round(value*100)}%`;

/** Decisions are shown but not actionable until NX-044 provides the governed human review Actions. */
export function DecisionBar({decisions}:{decisions:Decisions}){
  return <div className="decisions" role="group" aria-label="审核决定">
    {decisions.actions.map(action=><button key={action} type="button" disabled aria-disabled="true" aria-describedby="decisions-disabled">{DECISION_LABELS[action]}</button>)}
    <p id="decisions-disabled" role="status">未启用：审核决定需等待受治理的人类审核 Action（NX-044）上线；当前不会提交任何决定。</p>
  </div>;
}

export function CandidateView({detail}:{detail:CandidateDetail}){
  return <article className="candidate" aria-labelledby="candidate-heading">
    <h3 id="candidate-heading">{KIND_LABELS[detail.kind]??detail.kind}：{detail.display_name}</h3>
    <p className="note">依赖 Claim：{detail.dependent_claim_count} 条 · 粘合配置版本：{detail.config_version}</p>
    <h4>原文证据</h4>
    {detail.evidence.length?<ol className="evidence">{detail.evidence.map(e=><li key={e.claim_id}><q>{e.quote||'（推断，无原文）'}</q>
      <small>{e.span_start===null?'无原文片段':`消息 ${e.source_message_id} 第 ${e.span_start}–${e.span_end} 字符`} · 谓词：{e.predicate} · 状态：{e.resolution_state}</small></li>)}</ol>:<p className="note">没有可显示的原文证据。</p>}
    <h4>粘合分数</h4>
    <dl className="scores"><dt>词相似度</dt><dd>{percent(detail.merge_scores.lexical_similarity)}</dd><dt>核心词包含</dt><dd>{percent(detail.merge_scores.core_term_containment)}</dd>
      <dt>向量聚类</dt><dd>{percent(detail.merge_scores.vector_cluster)}</dd><dt>业务白名单</dt><dd>{percent(detail.merge_scores.rule_whitelist)}</dd>
      <dt>加权总分 / 阈值</dt><dd>{percent(detail.merge_scores.weighted_total)} / {percent(detail.merge_scores.threshold)}</dd>
      {detail.merge_scores.best_match_ref&&<><dt>最接近的已有定义</dt><dd>{detail.merge_scores.best_match_ref}</dd></>}</dl>
    <h4>召回结果</h4>
    {detail.recall.length?<ol>{detail.recall.map(h=><li key={h.ref+h.method}>{h.ref}（{h.method}，{percent(h.score)}）</li>)}</ol>:<p className="note">没有召回结果。</p>}
    <h4>相似候选</h4>
    {detail.similar.length?<ul>{detail.similar.map(s=><li key={s.candidate_id}>{s.display_name}（已并入本候选{s.merge_scores?`，相似度 ${percent(s.merge_scores.weighted_total)}`:''}）</li>)}</ul>:<p className="note">没有相似候选。</p>}
    {detail.cooldown&&<p role="status">同文本候选曾被拒绝，冷却期至 {new Date(detail.cooldown.cooldown_until).toLocaleString('zh-CN')}。</p>}
    <DecisionBar decisions={detail.decisions}/>
  </article>;
}

export function QueueList({items,onSelect,selected}:{items:QueueItem[];onSelect:(id:string)=>void;selected:string}){
  if(!items.length)return <p className="note">审核队列为空。</p>;
  return <ul className="queue">{items.map(item=><li key={item.candidate_id}><button type="button" aria-pressed={item.candidate_id===selected} onClick={()=>onSelect(item.candidate_id)}>
    {KIND_LABELS[item.kind]??item.kind}：{item.display_name} · 依赖 {item.dependent_claim_count} 条 · 总分 {percent(item.merge_scores.weighted_total)}</button></li>)}</ul>;
}

export function ReviewWorkbench(){
  const [selected,setSelected]=useState('');
  const queue=useQuery({queryKey:['review-queue'],queryFn:()=>readQueue(),retry:false});
  const detail=useQuery({queryKey:['review-candidate',selected],queryFn:()=>readCandidate(selected),enabled:!!selected,retry:false});
  // No current review permission: the queue is not shown at all (AT-070).
  if(queue.data&&!queue.data.visible)return null;
  return <section className="review" aria-labelledby="review-heading"><h2 id="review-heading">候选定义审核</h2>
    {queue.isPending?<p role="status">正在读取审核队列…</p>:queue.isError?<p role="alert">{reviewError(queue.error)}</p>:queue.data?.visible&&<>
      <QueueList items={queue.data.items} selected={selected} onSelect={setSelected}/>
      <button type="button" onClick={()=>void queue.refetch()}>刷新队列</button></>}
    {selected&&(detail.isPending?<p role="status">正在读取候选详情…</p>:detail.isError?<p role="alert">{reviewError(detail.error)}</p>:detail.data&&<CandidateView detail={detail.data}/>)}
  </section>;
}
