import type {ReactNode} from 'react';
import {useQuery} from '@tanstack/react-query';
import {RESTRICTED_TEXT,REVIEW_ENDED_TEXT,readMe,readReviewEvidence,readReviewQueue,readsKnowledgeOnly,reviews} from '../api';
import {Page,Short} from './Page';
import {Time} from '../ui';
import {when} from './Consumers';
import {DataTable} from '../DataTable';
import {ApiError} from '../../api';
import {DecisionBar} from '../../Review';
import {WORKBENCH_REVIEW,readCandidate,reviewError,type CandidateDetail} from '../../review-api';

/** The 403 of the knowledge pages for a role with neither the review Action nor the owner's read-only one (the operator, a
 * reviewer-less member): the page says whose page it is instead of the generic "no permission" (NX-028 D2 and its 2026-10-18
 * revision). */
export const REVIEWER_ONLY_TEXT=<>
  <p>查看知识工作台需要审核 Action（<code>ontology.schema.review</code>，<strong>审核员（reviewer）</strong>角色）或知识只读 Action（<code>nexloop.knowledge.read</code>）。当前账号的角色两者都不包含。</p>
  <p>需要查看或审核时，请用相应角色的账号登录工作台。成员的角色由可信配置分配（<code>workbench_roles</code>），不能在工作台内自行更改。</p>
</>;

/** NX-028 D2 revision (owner 2026-10-18): the owner sees the knowledge pages read-only; decisions are the reviewer's. */
export const READ_ONLY_TEXT='只读：你可以查看待审核的候选和证据；审核决定（通过、并入、拒绝）由审核员提交。';

/** Knowledge review entry: pending review items; a reviewer decides on each item's evidence page (governed review Action). */
export function Knowledge({actions,onOpen}:{actions?:ReactNode;onOpen:(candidateId:string)=>void}){
  const query=useQuery({queryKey:['workbench','review'],queryFn:readReviewQueue});
  const member=useQuery({queryKey:['workbench','me'],queryFn:readMe,refetchOnWindowFocus:false});
  return <Page title="知识工作台" query={query} actions={actions} empty={d=>d.length===0} forbidden={REVIEWER_ONLY_TEXT}>{items=><>
    {readsKnowledgeOnly(member.data)?<p className="note" role="status">{READ_ONLY_TEXT}</p>:null}
    <p className="note">候选定义只显示为“候选”，不会出现在正式本体视图中。审核员在候选的证据页中提交决定（通过、并入已有定义、拒绝），决定写入审计。</p>
    <DataTable label="待审核候选" rows={items} rowKey={x=>x.candidate_id} columns={[
      {label:'候选',main:true,cell:x=><button type="button" className="link" onClick={()=>onOpen(x.candidate_id)}>{x.display_name??x.candidate_id}</button>},
      {label:'类别',cell:x=>x.kind},{label:'证据',num:true,cell:x=>x.evidence_count},{label:'提出时间',cell:x=><Time value={x.created_at}/>}]}/>
  </>}</Page>;
}

/** The evidence messages of one pending item, read through the reviewer derivation (each read audited). */
export function ReviewEvidencePage({candidateId,actions}:{candidateId:string;actions?:ReactNode}){
  const query=useQuery({queryKey:['workbench','review',candidateId],queryFn:()=>readReviewEvidence(candidateId)});
  const member=useQuery({queryKey:['workbench','me'],queryFn:readMe,refetchOnWindowFocus:false});
  return <Page title="审核证据" query={query} actions={actions} forbidden={REVIEWER_ONLY_TEXT}>{v=>v.status==='ended'?<p className="note" role="status">{REVIEW_ENDED_TEXT}</p>:<>
    <p>候选：{v.display_name??v.candidate_id}（{v.kind}，待审核）</p>
    {v.evidence.length?<ol>{v.evidence.map(e=><li key={e.claim_id}>
      <p className="meta">{e.predicate??'—'}{e.source_message_id?<> · 消息 <Short value={e.source_message_id}/></>:null}</p>
      {e.content.status==='ok'?<blockquote>{e.content.body}</blockquote>:<p className="note">{e.source_message_id?RESTRICTED_TEXT:'无来源消息'}</p>}
    </li>)}</ol>:<p className="note">暂无证据。</p>}
    {/* PR #6 review: the decisions mount only once /me has answered with the review Action; while it loads or fails nothing is
        requested from the reviewer's detail endpoint. */}
    {member.isPending?<p className="note" role="status">正在确认当前账号的角色…</p>
      :member.isError?<p className="note" role="alert">读取当前账号的角色失败，审核决定暂不显示。</p>
      :reviews(member.data)?<ReviewDecisions candidateId={v.candidate_id}/>
      :readsKnowledgeOnly(member.data)?<p className="note" role="status">{READ_ONLY_TEXT}</p>:null}
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
