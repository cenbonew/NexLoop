import type {ReactNode} from 'react';
import {useQuery} from '@tanstack/react-query';
import {ageText,readOverview,type Backlog} from '../api';
import {Block,Page} from './Page';
import {takeoverText} from '../actions';

export function Overview({actions,onOpen}:{actions?:ReactNode;onOpen:(page:string)=>void}){
  const query=useQuery({queryKey:['workbench','overview'],queryFn:readOverview});
  return <Page title="总览" query={query} actions={actions}>{data=><div className="workbench-grid">
    <Block title="目标与控制" section={data.goals}>{g=><>
      <p>控制 revision：{g.control_revision}</p>
      {g.goals.length?<ul>{g.goals.map(x=><li key={x.goal_id}>{x.objective}（v{x.current_version}，KR {x.key_results} 个）</li>)}</ul>:<p className="note">暂无已发布目标。</p>}
      {g.paused_scopes.length?<p className="warning">已暂停：{g.paused_scopes.map(s=>`${s.scope_kind}:${s.scope_ref}`).join('、')}</p>:null}
      <button type="button" onClick={()=>onOpen('goals')}>查看目标</button></>}</Block>
    <Block title="承诺" section={data.commitments}>{c=><>
      <p>未结束 {c.open} 条，其中已违约 {c.breached} 条</p>
      {c.exceptions.length?<ul>{c.exceptions.map(e=><li key={e.reason}>{e.reason}：{e.count}</li>)}</ul>:<p className="note">没有异常。</p>}
      <button type="button" onClick={()=>onOpen('commitments')}>查看承诺</button></>}</Block>
    <Block title="待核对的外部动作" section={data.actions}>{a=><>
      <p>待核对 {a.count} 个{a.oldest_age_seconds!==null?`，最久 ${Math.round(a.oldest_age_seconds/60)} 分钟`:''}</p>
      <button type="button" onClick={()=>onOpen('actions')}>查看 Action</button></>}</Block>
    <Block title="联系限制" section={data.contact}>{c=><>
      <p>受限客户 {c.restricted} 位；来信必回升级 {c.escalations} 条</p>
      <button type="button" onClick={()=>onOpen('contact')}>查看限制</button></>}</Block>
    <Block title="人工接管" section={data.takeovers}>{t=>t.length?<ul>{t.map(x=><li key={x.takeover_id}>{takeoverText(x)}</li>)}</ul>:<p className="note">当前没有人工接管。</p>}</Block>
    <Block title="队列积压" section={data.backlog}>{b=><BacklogView backlog={b}/>}</Block>
    <Block title="商业事件与费用" section={data.commercial}>{()=>null}</Block>
  </div>}</Page>;
}

/** Every part is stated: an empty map reads “无”, never a blank block (AT-045). */
export function BacklogView({backlog}:{backlog:Backlog}){
  const queues=Object.entries(backlog.queues),feeds=Object.entries(backlog.feeds);
  return <>
    <h3>队列</h3>
    {queues.length?<ul>{queues.map(([q,v])=><li key={q}>{q}：待处理 {v.pending??0}，重试 {v.retry_wait??0}，死信 {v.dead_lettered??0}，最久等待 {ageText(v.oldest_pending_seconds)}</li>)}</ul>
      :<p className="note">无（没有排队中的任务）。</p>}
    <h3>后台 feed</h3>
    {feeds.length?<ul>{feeds.map(([f,v])=><li key={f}>{f}：待处理 {v.pending??0}，死信 {v.dead_lettered??0}，最久等待 {ageText(v.oldest_pending_seconds)}</li>)}</ul>
      :<p className="note">无（没有积压）。</p>}
    <p>待提取消息 {backlog.extraction.pending_messages} 条，最久等待 {ageText(backlog.extraction.oldest_pending_seconds)}</p>
  </>;
}
