import {useState,type ReactNode} from 'react';
import {useQuery} from '@tanstack/react-query';
import {RESTRICTED_TEXT,REVIEW_ENDED_TEXT,readMe,readReviewEvidence,readReviewQueue,readsKnowledgeOnly,reviews,type ReviewEvidence} from '../api';
import {Page} from './Page';
import {StatusBadge,Time} from '../ui';
import {EmptyState,IdText,shorten} from '../kit';
import {when} from './Consumers';
import {DataTable} from '../DataTable';
import {ApiError} from '../../api';
import {DecisionBar} from '../../Review';
import {WORKBENCH_REVIEW,readCandidate,reviewError,type CandidateDetail} from '../../review-api';
import {SemanticReleaseSection} from './SemanticReleases';

/** The 403 of the knowledge pages for a role with neither the review Action nor the owner's read-only one (the operator, a
 * reviewer-less member): the page says whose page it is instead of the generic "no permission" (NX-028 D2 and its 2026-10-18
 * revision). */
export const REVIEWER_ONLY_TEXT=<>
  <p>知识工作台只对<strong>审核人</strong>和有知识只读权限的成员开放，当前账号的角色不包含这两项。</p>
  <p>需要查看或审核时，请用相应角色的账号登录工作台。成员的角色由可信配置分配，不能在工作台内自行更改。</p>
  <details className="state-details"><summary>技术细节</summary>需要 <code>ontology.schema.review</code>（审核）或 <code>nexloop.knowledge.read</code>（只读）；角色来自 <code>workbench_roles</code>。HTTP 403</details>
</>;

/** NX-028 D2 revision (owner 2026-10-18): a role with nexloop.knowledge.read but not ontology.schema.review sees the knowledge
 * pages read-only. Revision 2 (owner 2026-10-20): the owner role also holds the review Action and decides; this text is for a role
 * that reads without it. */
export const READ_ONLY_TEXT='只读：你可以查看待审核的候选和证据；审核决定（通过、并入、拒绝）由有审核权限的成员提交。';

/** Knowledge review entry: pending review items; a reviewer decides on each item's evidence page (governed review Action). */
export function Knowledge({actions,onOpen}:{actions?:ReactNode;onOpen:(candidateId:string)=>void}){
  const query=useQuery({queryKey:['workbench','review'],queryFn:readReviewQueue});
  const member=useQuery({queryKey:['workbench','me'],queryFn:readMe,refetchOnWindowFocus:false});
  // NX-039 V4: the owner's semantic release controls sit beside the review queue (shown only for a role carrying them).
  return <><Page title="知识工作台" query={query} actions={actions} lead="从对话中提出的候选定义，等待审核人依据证据决定是否进入正式的企业世界模型。"
    empty={d=>d.length===0} emptyState={<EmptyState kind="empty" subject="待审核的候选" next="从对话中提出新的候选定义后，会出现在这里。"/>} forbidden={REVIEWER_ONLY_TEXT}>{items=><>
    {readsKnowledgeOnly(member.data)?<p className="note" role="status">{READ_ONLY_TEXT}</p>:null}
    <p className="note">候选定义只显示为“候选”，不会出现在正式本体视图中。有审核权限的成员（负责人或审核人）在候选的证据页中提交决定（通过、并入已有定义、拒绝），理由必填，决定写入审计。</p>
    <DataTable label="待审核候选" rows={items} rowKey={x=>x.candidate_id} columns={[
      {label:'候选',main:true,cell:x=><button type="button" className="link" onClick={()=>onOpen(x.candidate_id)}>{x.display_name??shorten(x.candidate_id)}</button>},
      {label:'类别',cell:x=><StatusBadge domain="candidate_kind" code={x.kind}/>},{label:'证据',num:true,cell:x=>x.evidence_count},{label:'提出时间',cell:x=><Time value={x.created_at}/>}]}/>
  </>}</Page><SemanticReleaseSection/></>;
}

/** The evidence messages of one pending item, read through the reviewer derivation (each read audited). */
export function ReviewEvidencePage({candidateId,actions}:{candidateId:string;actions?:ReactNode}){
  const query=useQuery({queryKey:['workbench','review',candidateId],queryFn:()=>readReviewEvidence(candidateId)});
  const member=useQuery({queryKey:['workbench','me'],queryFn:readMe,refetchOnWindowFocus:false});
  return <Page title="审核证据" query={query} actions={actions} forbidden={REVIEWER_ONLY_TEXT}>{v=>v.status==='ended'?<p className="note" role="status">{REVIEW_ENDED_TEXT}</p>:<>
    {/* Owner 2026-10-20: the read-only notice sits under the title, not below the evidence. */}
    {readsKnowledgeOnly(member.data)?<p className="note" role="status">{READ_ONLY_TEXT}</p>:null}
    <p>候选：{v.display_name??<IdText value={v.candidate_id}/>} <StatusBadge domain="candidate_kind" code={v.kind}/> <StatusBadge text="待审核" tone="pending"/></p>
    <EvidenceByMessage evidence={v.evidence}/>
    {/* PR #6 review: the decisions mount only once /me has answered with the review Action; while it loads or fails nothing is
        requested from the reviewer's detail endpoint. */}
    {member.isPending?<p className="note" role="status">正在确认当前账号的角色…</p>
      :member.isError?<p className="note" role="alert">读取当前账号的角色失败，审核决定暂不显示。</p>
      :reviews(member.data)?<ReviewDecisions candidateId={v.candidate_id}/>:null}
  </>}</Page>;
}

type Evidence=Extract<ReviewEvidence,{status:'pending_review'}>['evidence'][number];
/** Evidence grouped by its source message (owner 2026-10-20: one message gave several Claims and its text repeated once per Claim):
 * each message's text once (six lines, expandable), then the Claims taken from it; Claims without a source message stay apart. */
export function groupEvidence(evidence:Evidence[]):{message:string|null;content:Evidence['content']|null;claims:Evidence[]}[]{
  const groups=new Map<string,{message:string|null;content:Evidence['content']|null;claims:Evidence[]}>();
  for(const e of evidence){
    const key=e.source_message_id?'m:'+e.source_message_id:'c:'+e.claim_id;
    const g=groups.get(key)??{message:e.source_message_id,content:e.source_message_id?e.content:null,claims:[]};
    if(g.content?.status!=='ok'&&e.content.status==='ok'&&e.source_message_id)g.content=e.content;
    g.claims.push(e);groups.set(key,g);
  }
  return [...groups.values()];
}
function MessageExcerpt({body}:{body:string}){
  const [open,setOpen]=useState(false);const long=body.split('\n').length>6||body.length>360;
  return <><blockquote className={long&&!open?'clamped':undefined}>{body}</blockquote>
    {long?<button type="button" className="link" aria-expanded={open} onClick={()=>setOpen(!open)}>{open?'收起':'展开全文'}</button>:null}</>;
}
export function EvidenceByMessage({evidence}:{evidence:Evidence[]}){
  if(!evidence.length)return <EmptyState kind="empty" subject="证据"/>;
  const groups=groupEvidence(evidence);
  return <ol className="evidence-list" aria-label={`证据：${groups.length} 条消息，${evidence.length} 条 Claim`}>{groups.map(g=><li key={g.message??g.claims[0].claim_id} className="evidence-message">
    <p className="meta">{g.message?<IdText kind="消息" value={g.message}/>:'无来源消息'} · {g.claims.length} 条 Claim</p>
    {g.message?(g.content?.status==='ok'&&g.content.body!=null?<MessageExcerpt body={g.content.body}/>:<p className="note">{RESTRICTED_TEXT}</p>):null}
    <ul className="evidence-claims">{g.claims.map(e=><li key={e.claim_id}>{e.predicate?<code className="id-code" title="谓词">{e.predicate}</code>:'—'} <IdText kind="Claim" value={e.claim_id}/></li>)}</ul>
  </li>)}</ol>;
}

/** The three governed review decisions, for a member whose current role grants ontology.schema.review (reviewer, and the owner
 * since NX-028 D2 revision 2, 2026-10-20). Without
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
