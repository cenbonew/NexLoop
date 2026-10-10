import type {ReactNode} from 'react';
import {useQuery} from '@tanstack/react-query';
import {readOverview} from '../api';
import {Block,Page} from './Page';

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
    <Block title="人工接管" section={data.takeovers}>{()=>null}</Block>
    <Block title="队列积压" section={data.backlog}>{()=>null}</Block>
    <Block title="商业事件与费用" section={data.commercial}>{()=>null}</Block>
  </div>}</Page>;
}
