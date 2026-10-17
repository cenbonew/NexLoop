import type {ReactNode} from 'react';
import {useQuery} from '@tanstack/react-query';
import {RESTRICTED_TEXT,REVIEW_ENDED_TEXT,readReviewEvidence,readReviewQueue} from '../api';
import {Page,Short} from './Page';
import {when} from './Consumers';
import {DataTable} from '../DataTable';
import {ApiError} from '../../api';
import {DecisionBar} from '../../Review';
import {WORKBENCH_REVIEW,readCandidate,reviewError,type CandidateDetail} from '../../review-api';

/** The 403 of the review pages for the owner and operator roles: by design (NX-028 D2, owner 2026-10-10), the review Action
 * ontology.schema.review is the reviewer role's only; the page says so instead of the generic "no permission". */
export const REVIEWER_ONLY_TEXT=<>
  <p>知识工作台属于<strong>审核员（reviewer）</strong>角色。审核候选定义需要审核 Action（<code>ontology.schema.review</code>），负责人和运营角色不包含这项 Action（NX-028 D2）。</p>
  <p>需要审核时，请用审核员账号登录工作台。成员的角色由可信配置分配（<code>workbench_roles</code>），不能在工作台内自行更改。</p>
</>;

/** Knowledge review entry: pending review items; a reviewer decides on each item's evidence page (governed review Action). */
export function Knowledge({actions,onOpen}:{actions?:ReactNode;onOpen:(candidateId:string)=>void}){
  const query=useQuery({queryKey:['workbench','review'],queryFn:readReviewQueue});
  return <Page title="知识工作台" query={query} actions={actions} empty={d=>d.length===0} forbidden={REVIEWER_ONLY_TEXT}>{items=><>
    <p className="note">候选定义只显示为“候选”，不会出现在正式本体视图中。审核员在候选的证据页中提交决定（通过、并入已有定义、拒绝），决定写入审计。</p>
    <DataTable label="待审核候选" rows={items} rowKey={x=>x.candidate_id} columns={[
      {label:'候选',main:true,cell:x=><button type="button" className="link" onClick={()=>onOpen(x.candidate_id)}>{x.display_name??x.candidate_id}</button>},
      {label:'类别',cell:x=>x.kind},{label:'证据',num:true,cell:x=>x.evidence_count},{label:'提出时间',cell:x=>when(x.created_at)}]}/>
  </>}</Page>;
}

/** The evidence messages of one pending item, read through the reviewer derivation (each read audited). */
export function ReviewEvidencePage({candidateId,actions}:{candidateId:string;actions?:ReactNode}){
  const query=useQuery({queryKey:['workbench','review',candidateId],queryFn:()=>readReviewEvidence(candidateId)});
  return <Page title="审核证据" query={query} actions={actions} forbidden={REVIEWER_ONLY_TEXT}>{v=>v.status==='ended'?<p className="note" role="status">{REVIEW_ENDED_TEXT}</p>:<>
    <p>候选：{v.display_name??v.candidate_id}（{v.kind}，待审核）</p>
    {v.evidence.length?<ol>{v.evidence.map(e=><li key={e.claim_id}>
      <p className="meta">{e.predicate??'—'}{e.source_message_id?<> · 消息 <Short value={e.source_message_id}/></>:null}</p>
      {e.content.status==='ok'?<blockquote>{e.content.body}</blockquote>:<p className="note">{e.source_message_id?RESTRICTED_TEXT:'无来源消息'}</p>}
    </li>)}</ol>:<p className="note">暂无证据。</p>}
    <ReviewDecisions candidateId={v.candidate_id}/>
  </>}</Page>;
}

/** The three governed review decisions, for a member whose current role grants ontology.schema.review (reviewer). Without
 * the grant the workbench route answers 403 and nothing is shown; other failures are labelled (AT-045). */
export function ReviewDecisions({candidateId}:{candidateId:string}){
  const query=useQuery({queryKey:['workbench','review-decision',candidateId],queryFn:()=>readCandidate(candidateId,WORKBENCH_REVIEW),retry:false});
  return <ReviewDecisionsView state={query.isPending?{status:'pending'}:query.isError?{status:'error',error:query.error}:{status:'ok',data:query.data}}/>;
}
type DecisionState={status:'pending'}|{status:'error';error:unknown}|{status:'ok';data:CandidateDetail};
export function ReviewDecisionsView({state}:{state:DecisionState}){
  if(state.status==='error'&&state.error instanceof ApiError&&state.error.status===403)return null;
  return <section aria-labelledby="review-decisions-heading"><h2 id="review-decisions-heading">审核决定</h2>
    {state.status==='pending'?<p role="status">正在读取候选详情…</p>:state.status==='error'?<p role="alert">{reviewError(state.error)}</p>
      :<><p className="note">版本 {state.data.revision} · 依赖 Claim {state.data.dependent_claim_count} 条 · 粘合总分 {Math.round(state.data.merge_scores.weighted_total*100)}%</p>
        <DecisionBar detail={state.data} endpoints={WORKBENCH_REVIEW}/></>}
  </section>;
}
