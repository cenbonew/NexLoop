import {useState,type ReactNode} from 'react';
import {keepPreviousData,useQuery} from '@tanstack/react-query';
import {COMMITMENT_STATUS_TEXT,OPEN_STATUSES,RESTRICTED_TEXT,readCommitment,readCommitments,type CommitmentView,type EvidenceItem} from '../api';
import {Page,Short} from './Page';
import {Empty,StatusBadge,Tabs,commitmentTone} from '../ui';
import {when} from './Consumers';
import {DataTable} from '../DataTable';
import {Operations} from './PageOps';

/** The promised words: shown only with the source Message's READ while it exists. */
export function quoteText(view:CommitmentView):string{
  return view.quote_status==='ok'&&view.quote?view.quote:view.quote_status==='restricted'?RESTRICTED_TEXT:view.quote_status==='unavailable'?'来源消息已删除，原文不可用':'无来源消息';
}
/** Due date and precision; a latest bound is a derived value, labelled as such. */
export function dueText(view:CommitmentView):string{
  const due=typeof view.properties.due_at==='string'?view.properties.due_at:null;
  if(!due)return '无期限';
  return view.properties.precision==='latest_bound'?`最晚界 ${due}（推导值）`:due;
}

function Evidence({title,items,note}:{title:string;items:EvidenceItem[];note?:string}){
  return <div className="evidence-column"><h3>{title}</h3>{note?<p className="note">{note}</p>:null}
    {items.length?<ul>{items.map(x=><li key={x.kind+x.ref}>{x.kind} · {when(x.occurred_at)}</li>)}</ul>:<p className="note">暂无证据。</p>}</div>;
}

export function Commitments({actions,onOpen}:{actions?:ReactNode;onOpen:(commitmentId:string)=>void}){
  const [ended,setEnded]=useState(false);
  const query=useQuery({queryKey:['workbench','commitments',ended],queryFn:()=>readCommitments(ended),placeholderData:keepPreviousData});
  // The tabs stay mounted (and focused) while the other list loads; the old rows stay, marked busy (F12, F14).
  return <Page title="承诺 / 交付" query={query} actions={actions} toolbar={<Tabs label="承诺范围" value={ended?'ended':'open'} onChange={k=>setEnded(k==='ended')}
    tabs={[{key:'open',label:'未结束'},{key:'ended',label:'已结束'}]}/>}>{data=>{
    const rows=data.commitments.filter(c=>OPEN_STATUSES.includes(c.status)!==ended);
    return <>
      {rows.length?<DataTable label="承诺" rows={rows} rowKey={c=>c.commitment_id} columns={[
        {label:'承诺',main:true,cell:c=><button type="button" className="link" onClick={()=>onOpen(c.commitment_id)}>{typeof c.properties.predicate==='string'?c.properties.predicate:<Short value={c.commitment_id}/>}</button>},
        {label:'状态',cell:c=><StatusBadge text={COMMITMENT_STATUS_TEXT[c.status]??'未知状态'} tone={commitmentTone(c.status)} code={c.status}/>},{label:'期限',cell:dueText},
        {label:'条件',wide:true,cell:c=>typeof c.properties.condition==='string'&&c.properties.condition?c.properties.condition:'—'},
        {label:'异常',cell:c=>c.exceptions.map(e=>e.reason).join('、')||'—'}]}/>:<Empty kind="filtered" title={ended?'没有已结束的承诺':'没有未结束的承诺'} next={ended?'切换到“未结束”查看进行中的承诺。':'切换到“已结束”查看历史承诺。'}/>}
      {data.exceptions.length?<><h2>异常</h2><ul>{data.exceptions.map(e=><li key={e.subject_ref+e.reason}>{e.reason} · {e.subject_ref} · {when(e.raised_at)}</li>)}</ul></>:null}
    </>;}}</Page>;
}

export function CommitmentDetail({commitmentId,actions}:{commitmentId:string;actions?:ReactNode}){
  const query=useQuery({queryKey:['workbench','commitment',commitmentId],queryFn:()=>readCommitment(commitmentId)});
  return <Page title="承诺详情" query={query}>{c=><>
    <h2>承诺正文</h2>
    <blockquote>{quoteText(c)}</blockquote>
    <h2>当前状态</h2>
    <p className="workbench-summary" role="status">{COMMITMENT_STATUS_TEXT[c.status]??c.status}（来自承诺对象，界面不推断） · 期限：{dueText(c)}
      {c.exceptions.length?` · 异常：${c.exceptions.map(e=>e.reason).join('、')}`:''}</p>
    <h2>证据</h2>
    <div className="evidence-grid">
      <Evidence title="请求创建" items={c.evidence.requested}/>
      <Evidence title="已交付" items={c.evidence.delivered} note="消息已送达不代表承诺已兑现。"/>
      <Evidence title="客户确认" items={c.evidence.customer_confirmed}/>
      <div className="evidence-column"><h3>问题解决</h3><p className="note">不可用（v0.1 不推断问题是否解决）。</p></div>
    </div>
    <h2>事件</h2><ol>{c.events.map((e,i)=><li key={i}>{e.kind} · {when(e.recorded_at)}{e.principal_id?` · ${e.principal_id}`:''}</li>)}</ol>
    {c.exceptions.length?<><h2>异常</h2><ul>{c.exceptions.map(e=><li key={e.reason}>{e.reason} · {when(e.raised_at)}</li>)}</ul></>:null}
    <Operations actions={actions} title="对这条承诺的操作"/>
  </>}</Page>;
}
