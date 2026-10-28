import type {ReactNode} from 'react';
import {useQuery} from '@tanstack/react-query';
import {ageText,readOverview,type Backlog,type Commercial} from '../api';
import {Page} from './Page';
import {Money,StatusBadge} from '../ui';
import {CardGrid,EmptyState,KeyValue,Notice,ScopeText,SectionCard} from '../kit';
import {statusLabel} from '../status-labels';
import {takeoverText} from '../actions';

export function Overview({actions,onOpen}:{actions?:ReactNode;onOpen:(page:string)=>void}){
  const query=useQuery({queryKey:['workbench','overview'],queryFn:readOverview});
  const open=(page:string,text:string)=><button type="button" onClick={()=>onOpen(page)}>{text}</button>;
  return <Page title="总览" query={query} actions={actions} lead="今天需要关注什么：目标与控制、承诺、Action 与异常、联系限制、人工接管、后台积压与费用。">{data=><CardGrid label="总览">
    <SectionCard title="目标与控制" section={data.goals} footer={open('goals','查看目标')}>{g=><>
      <KeyValue items={[{key:'已发布目标',value:`${g.goals.length} 个`}]}/>
      {g.goals.length?<ul>{g.goals.map(x=><li key={x.goal_id}>{x.objective}（v{x.current_version}，KR {x.key_results} 个）</li>)}</ul>:<EmptyState kind="empty" subject="已发布目标" compact/>}
      {g.paused_scopes.length?<Notice tone="warning">已暂停主动联系：{g.paused_scopes.map((s,i)=><span key={s.scope_kind+s.scope_ref}>{i?'、':''}<ScopeText kind={s.scope_kind} value={s.scope_ref}/></span>)}</Notice>:null}</>}</SectionCard>
    <SectionCard title="承诺" section={data.commitments} footer={open('commitments','查看承诺')}>{c=><>
      <p>未结束 {c.open} 条，其中已违约 {c.breached} 条</p>
      {c.exceptions.length?<ul className="badge-list">{c.exceptions.map(e=><li key={e.reason}><StatusBadge domain="commitment_exception" code={e.reason}/> {e.count}</li>)}</ul>
        :<EmptyState kind="empty" subject="承诺异常" compact/>}</>}</SectionCard>
    <SectionCard title="Action / 异常" section={data.actions} footer={open('actions','查看 Action')}>{a=>
      <p>待核对 {a.count} 个{a.oldest_age_seconds!==null?`，最久 ${Math.round(a.oldest_age_seconds/60)} 分钟`:''}</p>}</SectionCard>
    <SectionCard title="联系限制" section={data.contact} footer={open('contact','查看限制')}>{c=>
      <p>受限客户 {c.restricted} 位；来信必回升级 {c.escalations} 条</p>}</SectionCard>
    <SectionCard title="人工接管" section={data.takeovers}>{t=>t.length?<ul>{t.map(x=><li key={x.takeover_id}>{takeoverText(x)}</li>)}</ul>:<EmptyState kind="empty" subject="人工接管" compact/>}</SectionCard>
    <SectionCard title="队列积压" section={data.backlog}>{b=><BacklogView backlog={b}/>}</SectionCard>
    <SectionCard title="商业事件与费用" section={data.commercial}>{c=><CommercialView commercial={c}/>}</SectionCard>
  </CardGrid>}</Page>;
}

/** Every part is stated: an empty map reads “无”, never a blank block (AT-045). */
export function BacklogView({backlog}:{backlog:Backlog}){
  const queues=Object.entries(backlog.queues),feeds=Object.entries(backlog.feeds);
  return <>
    <h3>队列</h3>
    {queues.length?<ul>{queues.map(([q,v])=><li key={q}>{statusLabel('queue',q)}：待处理 {v.pending??0}，重试 {v.retry_wait??0}，死信 {v.dead_lettered??0}，最久等待 {ageText(v.oldest_pending_seconds)}</li>)}</ul>
      :<EmptyState kind="empty" subject="排队中的任务" compact/>}
    <h3>后台任务</h3>
    {feeds.length?<ul>{feeds.map(([f,v])=><li key={f}>{statusLabel('feed',f)}：待处理 {v.due??v.pending??0}{v.scheduled?`（另有 ${v.scheduled} 个定时未到期）`:''}，死信 {v.dead_lettered??0}，最久等待 {ageText(v.oldest_pending_seconds)}</li>)}</ul>
      :<EmptyState kind="empty" subject="积压" compact/>}
    <p>待提取消息 {backlog.extraction.pending_messages} 条，最久等待 {ageText(backlog.extraction.oldest_pending_seconds)}</p>
  </>;
}

export function CommercialView({commercial}:{commercial:Commercial}){
  const budgets=Object.entries(commercial.budgets);
  return <>
    <h3>预算</h3>
    {budgets.length?<ul>{budgets.map(([k,b])=><li key={k}>{statusLabel('budget_kind',k)}：已用 <Money value={b.consumed}/> / <Money value={b.limit}/> {b.unit}{b.ratio!==null?`（${(b.ratio*100).toFixed(1)}%）`:''}</li>)}</ul>
      :<EmptyState kind="empty" subject="预算" reason="还没有设置" compact/>}
    <h3>最近 24 小时费用</h3>
    {commercial.last_24h.length?<ul>{commercial.last_24h.map(x=><li key={`${x.cost_kind}:${x.data_mode}:${x.currency}`}>{statusLabel('cost_kind',x.cost_kind)}（{statusLabel('data_mode',x.data_mode)}）：<Money value={x.amount}/> {x.currency}，{x.entries} 条
      {x.unpriced?`，其中 ${x.unpriced} 条未定价`:''}</li>)}</ul>:<EmptyState kind="empty" subject="费用记录" reason="最近 24 小时" compact/>}
  </>;
}
