import type {ReactNode} from 'react';
import {useQuery} from '@tanstack/react-query';
import {RESTRICTED_TEXT,REVIEW_ENDED_TEXT,readReviewEvidence,readReviewQueue} from '../api';
import {Page,Short} from './Page';

/** Knowledge review entry (read-only): pending review items; decisions stay on the NX-046 review page. */
export function Knowledge({actions,onOpen}:{actions?:ReactNode;onOpen:(candidateId:string)=>void}){
  const query=useQuery({queryKey:['workbench','review'],queryFn:readReviewQueue});
  return <Page title="知识工作台" query={query} actions={actions} empty={d=>d.length===0}>{items=><>
    <p className="note">候选定义只显示为“候选”，不会出现在正式本体视图中。审核决定沿用现有审核页面（NX-046/NX-044）。</p>
    <table><thead><tr><th>候选</th><th>类别</th><th>证据</th><th>提出时间</th></tr></thead><tbody>
      {items.map(x=><tr key={x.candidate_id}><td><button type="button" className="link" onClick={()=>onOpen(x.candidate_id)}>{x.display_name??x.candidate_id}</button></td>
        <td>{x.kind}</td><td>{x.evidence_count}</td><td>{x.created_at}</td></tr>)}</tbody></table>
  </>}</Page>;
}

/** The evidence messages of one pending item, read through the reviewer derivation (each read audited). */
export function ReviewEvidencePage({candidateId,actions}:{candidateId:string;actions?:ReactNode}){
  const query=useQuery({queryKey:['workbench','review',candidateId],queryFn:()=>readReviewEvidence(candidateId)});
  return <Page title="审核证据" query={query} actions={actions}>{v=>v.status==='ended'?<p className="note" role="status">{REVIEW_ENDED_TEXT}</p>:<>
    <p>候选：{v.display_name??v.candidate_id}（{v.kind}，待审核）</p>
    {v.evidence.length?<ol>{v.evidence.map(e=><li key={e.claim_id}>
      <p className="meta">{e.predicate??'—'}{e.source_message_id?<> · 消息 <Short value={e.source_message_id}/></>:null}</p>
      {e.content.status==='ok'?<blockquote>{e.content.body}</blockquote>:<p className="note">{e.source_message_id?RESTRICTED_TEXT:'无来源消息'}</p>}
    </li>)}</ol>:<p className="note">暂无证据。</p>}
  </>}</Page>;
}
