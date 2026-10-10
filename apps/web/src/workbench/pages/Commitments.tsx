import {useState,type ReactNode} from 'react';
import {useQuery} from '@tanstack/react-query';
import {COMMITMENT_STATUS_TEXT,OPEN_STATUSES,RESTRICTED_TEXT,readCommitment,readCommitments,type CommitmentView,type EvidenceItem} from '../api';
import {Page,Short} from './Page';

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
    {items.length?<ul>{items.map(x=><li key={x.kind+x.ref}>{x.kind} · {x.occurred_at}</li>)}</ul>:<p className="note">暂无证据。</p>}</div>;
}

export function Commitments({actions,onOpen}:{actions?:ReactNode;onOpen:(commitmentId:string)=>void}){
  const [ended,setEnded]=useState(false);
  const query=useQuery({queryKey:['workbench','commitments',ended],queryFn:()=>readCommitments(ended)});
  return <Page title="承诺 / 交付" query={query} actions={actions}>{data=>{
    const rows=data.commitments.filter(c=>OPEN_STATUSES.includes(c.status)!==ended);
    return <>
      <div role="tablist"><button type="button" role="tab" aria-selected={!ended} onClick={()=>setEnded(false)}>未结束</button>
        <button type="button" role="tab" aria-selected={ended} onClick={()=>setEnded(true)}>已结束</button></div>
      {rows.length?<table><thead><tr><th>承诺</th><th>状态</th><th>期限</th><th>条件</th><th>异常</th></tr></thead><tbody>
        {rows.map(c=><tr key={c.commitment_id}><td><button type="button" className="link" onClick={()=>onOpen(c.commitment_id)}>{typeof c.properties.predicate==='string'?c.properties.predicate:<Short value={c.commitment_id}/>}</button></td>
          <td>{COMMITMENT_STATUS_TEXT[c.status]??c.status}</td><td>{dueText(c)}</td><td>{typeof c.properties.condition==='string'&&c.properties.condition?c.properties.condition:'—'}</td>
          <td>{c.exceptions.map(e=>e.reason).join('、')||'—'}</td></tr>)}</tbody></table>:<p className="note">暂无记录。</p>}
      {data.exceptions.length?<><h2>异常</h2><ul>{data.exceptions.map(e=><li key={e.subject_ref+e.reason}>{e.reason} · {e.subject_ref} · {e.raised_at}</li>)}</ul></>:null}
    </>;}}</Page>;
}

export function CommitmentDetail({commitmentId,actions}:{commitmentId:string;actions?:ReactNode}){
  const query=useQuery({queryKey:['workbench','commitment',commitmentId],queryFn:()=>readCommitment(commitmentId)});
  return <Page title="承诺详情" query={query} actions={actions}>{c=><>
    <p>状态：{COMMITMENT_STATUS_TEXT[c.status]??c.status}（来自承诺对象，界面不推断） · 期限：{dueText(c)}</p>
    <blockquote>{quoteText(c)}</blockquote>
    <div className="evidence-grid">
      <Evidence title="请求创建" items={c.evidence.requested}/>
      <Evidence title="已交付" items={c.evidence.delivered} note="消息已送达不代表承诺已兑现。"/>
      <Evidence title="客户确认" items={c.evidence.customer_confirmed}/>
      <div className="evidence-column"><h3>问题解决</h3><p className="note">不可用（v0.1 不推断问题是否解决）。</p></div>
    </div>
    <h2>事件</h2><ol>{c.events.map((e,i)=><li key={i}>{e.kind} · {e.recorded_at}{e.principal_id?` · ${e.principal_id}`:''}</li>)}</ol>
    {c.exceptions.length?<><h2>异常</h2><ul>{c.exceptions.map(e=><li key={e.reason}>{e.reason} · {e.raised_at}</li>)}</ul></>:null}
  </>}</Page>;
}
