/**
 * NX-039 V4 semantic release (design §8; D39-7/8/9/10/14), next to the review queue in the knowledge workbench. Owner only: the
 * section is shown only when the member's role carries the release Actions, and SQL decides every read and write again.
 * Publishing takes one evaluated candidate (accepted, change class patch or minor as the evaluator classified it - never chosen
 * here); the owner writes the item text: an alias must be the very term the candidate's evidence missed (checked by its hash), a
 * semantic note at most 200 characters. Rollback makes a new, higher version with an earlier version's content; Claims are never
 * changed, the rollback report gives their count and ids for review. Entities in EIOS English terms (UI spec draft v1 §7, §8, §9).
 */
import {useState} from 'react';
import {useQuery} from '@tanstack/react-query';
import {kindOf,readSemanticReleases,readSemanticReview,type PublishableCandidate,type ReleaseWatch,type ReviewCandidate,type SemanticRelease,type SemanticReview} from '../api';
import {ConfirmedForm,Form,WithMember} from '../actions';
import {offers,readMe} from '../api';
import {DataTable} from '../DataTable';
import {StatusBadge,Time} from '../ui';
import {Card,EmptyState,FormField,IdText,KeyValue} from '../kit';
import {statusLabel} from '../status-labels';

export const CLASS_TEXT:Record<'patch'|'minor',string>={patch:'补丁级',minor:'次要级'};
const KIND_TEXT:Record<'alias'|'description',string>={alias:'别名',description:'补充说明'};
export const NOTE_LIMIT=200;

/** Shown only once /me has answered with a release Action; while it loads or fails nothing is shown (the page's own state speaks). */
export function SemanticReleaseSection(){
  const member=useQuery({queryKey:['workbench','me'],queryFn:readMe,refetchOnWindowFocus:false});
  // D39-8: the release scope's Actions (publish, rollback, stop; owner by default) decide whether the section exists at all.
  return member.isSuccess&&(offers(member.data,'publish_semantic_release')||offers(member.data,'rollback_semantic_release')||offers(member.data,'set_evolution_stop'))
    ?<><ReviewQueue/><Releases/></>:null;
}

function Releases(){
  const query=useQuery({queryKey:['workbench','semantic-releases'],queryFn:readSemanticReleases});
  const review=useQuery({queryKey:['workbench','semantic-review'],queryFn:readSemanticReview});
  const watchOf=(version:string)=>review.data?.watch.releases.find(w=>watchVersion(w)===version)??null;
  return <Card title="语义发布" label="语义发布">
    <p className="note">只发布通过独立评估的候选；分级由评估报告给出，破坏性变更不能发布。回退会生成更高的版本号，不改动已有 Claim。</p>
    {query.isPending?<EmptyState kind="loading" subject="语义发布" compact/>
      :query.isError?<EmptyState kind={kindOf(query.error)==='forbidden'?'forbidden':'unavailable'} subject="语义发布" onRetry={()=>void query.refetch()}/>
      :<>
        <p role="status">当前语义版本：{query.data.current?<strong>v{query.data.current}</strong>:'尚未发布'}</p>
        <h3>可发布的候选</h3>
        {query.data.publishable.length?<DataTable label="可发布的候选" rows={query.data.publishable} rowKey={c=>c.candidate_id} columns={[
          {label:'候选',main:true,cell:c=><IdText value={c.candidate_id} kind="候选"/>},
          {label:'分级',cell:c=><StatusBadge text={CLASS_TEXT[c.change_class]} tone="ok" code={c.change_class}/>},
          {label:'变更',cell:c=>KIND_TEXT[c.change_kind]},{label:'目标',wide:true,cell:c=>c.target_refs.join('、')},
          {label:'发布',cell:c=>c.parent_ok?<PublishForm candidate={c}/>:<StatusBadge text="基线已变化，需重新评估" tone="limited" code="parent_moved"/>}]}/>
          :<EmptyState kind="empty" subject="可发布的候选" next="候选通过独立评估、且分级为补丁级或次要级后会出现在这里。" compact/>}
        <h3>版本历史</h3>
        {query.data.releases.length?<>
          <DataTable label="语义版本历史" rows={query.data.releases} rowKey={r=>r.version} columns={[
            {label:'版本',main:true,cell:r=><>v{r.version}{r.version===query.data.current?<> <StatusBadge text="当前" tone="ok" code="current"/></>:null}</>},
            {label:'观察期',cell:r=><WatchBadge watch={watchOf(r.version)}/>},
            {label:'方式',cell:r=>r.reason==='publish'?`发布（${r.change_class?CLASS_TEXT[r.change_class]:'—'}）`:`回退到 v${r.rolled_back_to}`},
            {label:'内容',cell:r=><>{itemsText(r)}{r.items.filter(i=>i.kind==='semantic_note').map(i=><NotePreview key={i.ref} ref_={i.ref} text={i.body}/>)}</>},{label:'回退报告',wide:true,cell:r=>reportText(r)},
            {label:'原因',cell:r=>r.owner_reason},{label:'时间',cell:r=><Time value={r.recorded_at}/>}]}/>
          {review.data?.watch.releases.some(w=>w.regressing)?<WatchWarning releases={review.data.watch.releases}/>:null}
          {query.data.current&&query.data.releases.length>1?<RollbackForm current={query.data.current} versions={query.data.releases.map(r=>r.version)}/>:null}
        </>:<EmptyState kind="empty" subject="语义版本" next="发布第一个候选后，版本历史会出现在这里。" compact/>}
      </>}
  </Card>;
}

export function itemsText(r:SemanticRelease):string{
  const aliases=r.items.filter(i=>i.kind==='alias').length,notes=r.items.length-aliases;
  return [aliases?`别名 ${aliases} 个`:'',notes?`补充说明 ${notes} 条`:''].filter(Boolean).join('，')||'无';
}
export function reportText(r:SemanticRelease):string{
  if(r.reason!=='rollback'||!r.rollback_report)return '—';
  const x=r.rollback_report;
  return `移除别名 ${x.removed_aliases} 个；期间经这些定义匹配的 Claim ${x.claim_count} 条（未改动，需人工复核）`+(x.dropped_erased_items?`；${x.dropped_erased_items} 项因来源客户删除未恢复`:'');
}

function PublishForm({candidate}:{candidate:PublishableCandidate}){
  const [ref,setRef]=useState(candidate.target_refs[0]??'');const [body,setBody]=useState('');const [reason,setReason]=useState('');
  const alias=candidate.change_kind==='alias';const id=candidate.candidate_id.slice(0,8);
  const text=body.trim();const ok=!!ref&&text.length>=1&&text.length<=(alias?200:NOTE_LIMIT)&&reason.trim().length>0;
  return <WithMember>{member=><Form operation="publish_semantic_release" member={member} title="发布" className="inline-action" disabled={!ok}
    body={()=>ok?{candidate_id:candidate.candidate_id,items:[{kind:alias?'alias':'semantic_note',ref,text}],reason:reason.trim()}:null}>
    <FormField id={`release-ref-${id}`} label="目标">
      <select id={`release-ref-${id}`} value={ref} onChange={e=>setRef(e.target.value)}>{candidate.target_refs.map(r=><option key={r} value={r}>{r}</option>)}</select></FormField>
    <FormField id={`release-text-${id}`} label={alias?'别名':'补充说明'} hint={alias?'必须是候选证据中未命中的那个词（服务端按哈希核对）。':`最多 ${NOTE_LIMIT} 字，进入 Context 的语义段。`}>
      {alias?<input id={`release-text-${id}`} value={body} maxLength={200} aria-describedby={`release-text-${id}-hint`} onChange={e=>setBody(e.target.value)}/>
        :<textarea id={`release-text-${id}`} value={body} maxLength={NOTE_LIMIT} rows={3} aria-describedby={`release-text-${id}-hint`} onChange={e=>setBody(e.target.value)}/>}</FormField>
    {!alias&&text?<NotePreview ref_={ref} text={text} counter/>:null}
    <FormField id={`release-reason-${id}`} label="原因"><input id={`release-reason-${id}`} value={reason} onChange={e=>setReason(e.target.value)}/></FormField>
  </Form>}</WithMember>;
}

function RollbackForm({current,versions}:{current:string;versions:string[]}){
  const targets=versions.filter(v=>v!==current);
  const [target,setTarget]=useState(targets[0]??'');const [reason,setReason]=useState('');
  const ok=!!target&&reason.trim().length>0;
  return <WithMember>{member=><Form operation="rollback_semantic_release" member={member} title="回退" disabled={!ok}
    body={()=>ok?{target_version:target,reason:reason.trim()}:null}>
    <p className="note">回退到所选版本的内容，并生成一个更高的版本号。已有 Claim 不会被改动；回退报告列出期间经被移除别名匹配的 Claim 数量与 ID，供人工复核。不会自动回退。</p>
    <FormField id="rollback-target" label="回退到" width="sm">
      <select id="rollback-target" value={target} onChange={e=>setTarget(e.target.value)}>{targets.map(v=><option key={v} value={v}>v{v}</option>)}</select></FormField>
    <FormField id="rollback-reason" label="原因"><input id="rollback-reason" value={reason} onChange={e=>setReason(e.target.value)}/></FormField>
  </Form>}</WithMember>;
}


// ---- NX-039 V6: the review queue, the stop switch, the release watch, the semantic_note preview ----
/** The release a watch entry belongs to: its ref is "release:<version>" (or the bare version). */
export function watchVersion(w:ReleaseWatch):string{return w.release_ref.replace(/^release:/,'');}
const pct=(v:number|null|undefined)=>v===null||v===undefined?'—':`${(v*100).toFixed(1)}%`;
const points=(a:number,b:number)=>`${((b-a)*100>=0?'+':'')}${((b-a)*100).toFixed(1)} 个百分点`;

/** D39-10: 观察中 for 7 days after publication; 劣化 above 2 points (V5 decides, here only shown); never an automatic rollback. */
export function WatchBadge({watch}:{watch:ReleaseWatch|null}){
  if(!watch)return <span className="note">—</span>;
  if(watch.regressing)return <StatusBadge text="劣化，建议回退" tone="fail" code="regressing"/>;
  return <StatusBadge text={watch.judged?'观察中，未劣化':'观察中，样本不足'} tone={watch.judged?'ok':'pending'} code={watch.judged?'watching':'insufficient'}/>;
}
export function WatchWarning({releases}:{releases:ReleaseWatch[]}){
  return <div className="warning" role="alert">{releases.filter(w=>w.regressing).map(w=><p key={w.release_ref}>
    v{watchVersion(w)} 发布后：未命中率 {pct(w.miss_rate_before)} → {pct(w.miss_rate_after)}（{points(w.miss_rate_before,w.miss_rate_after)}），
    歧义率 {pct(w.ambiguity_rate_before)} → {pct(w.ambiguity_rate_after)}（{points(w.ambiguity_rate_before,w.ambiguity_rate_after)}）；劣化超过 2 个百分点，建议回退到上一个版本。
    系统不会自动回退，请在下方“回退”中选择版本并填写原因。</p>)}</div>;
}

/** D39-14: a semantic_note only from a release item, at most 200 characters, shown as the Context will carry it. */
export function NotePreview({ref_,text,counter}:{ref_:string;text:string;counter?:boolean}){
  return <figure className="note-preview" aria-label="补充说明预览"><figcaption className="note">补充说明预览 · <code className="id-code">{ref_}</code>
    {counter?` · ${text.length}/${NOTE_LIMIT} 字`:''}</figcaption><blockquote>{text.slice(0,NOTE_LIMIT)}</blockquote></figure>;
}

export function gateSummary(c:ReviewCandidate):string{
  const passed=c.gates.filter(g=>g.result==='pass').length;
  return c.gates.length?`通过 ${passed}/${c.gates.length}`:'—';
}
export function scoreText(s:{parent:number|null;candidate:number|null;cases:number|null}):string{
  return s.parent===null&&s.candidate===null?'—':`${pct(s.parent)} → ${pct(s.candidate)}${s.cases!==null?`（${s.cases} 个用例）`:''}`;
}
export function niText(c:ReviewCandidate):string{
  if(!c.non_inferiority)return '—';
  const n=c.non_inferiority;
  return `主指标覆盖率；候选不低于基线 ${(n.margin*100).toFixed(0)} 个百分点以内${n.ties_accept?'':'，持平不算通过'}`;
}

/** The review queue: each candidate's latest report as the evaluator wrote it. Codes are Chinese labels; the change class is the
 * evaluator's classify_change result, shown and never set here (D39-7); digests and counts only (D39-11). */
export function ReviewQueue(){
  const query=useQuery({queryKey:['workbench','semantic-review'],queryFn:readSemanticReview});
  return <Card title="评估与审核" label="评估与审核">
    <p className="note">每个候选最新的评估报告：逐项门槛的结论、各级指标和“是否不比现有版本差”的判定。变更分级由评估器给出，这里只显示、不能修改；报告只含摘要和计数。</p>
    {query.isPending?<EmptyState kind="loading" subject="评估报告" compact/>
      :query.isError?<EmptyState kind={kindOf(query.error)==='forbidden'?'forbidden':'unavailable'} subject="评估报告" onRetry={()=>void query.refetch()}/>
      :<ReviewBody review={query.data}/>}
  </Card>;
}
export function ReviewBody({review}:{review:SemanticReview}){
  return <>
    <h3>演进停止开关</h3>
    {review.stops.length?<ul className="stop-list">{review.stops.map(s=><li key={s.dimension}>
      {statusLabel('evo_dimension',s.dimension)} <StatusBadge text={s.stopped?'已停止':'运行中'} tone={s.stopped?'limited':'ok'} code={s.stopped?'stopped':'running'}/>
      {s.changed_at?<span className="kv-hint">第 {s.revision} 次变更 · <Time value={s.changed_at}/></span>:null}
      <StopForm dimension={s.dimension} stopped={s.stopped}/></li>)}</ul>:<EmptyState kind="empty" subject="开放的演进维度" compact/>}
    <h3>评估报告</h3>
    {review.candidates.length?<DataTable label="评估报告" rows={review.candidates} rowKey={c=>c.candidate_id} columns={[
      {label:'候选',main:true,cell:c=><IdText value={c.candidate_id} kind="候选"/>},
      {label:'状态',cell:c=><><StatusBadge domain="evo_candidate_status" code={c.status}/>{c.end_reason?<span className="kv-hint">{statusLabel('evo_end_reason',c.end_reason)}</span>:null}</>},
      {label:'结论',cell:c=><><StatusBadge domain="evo_conclusion" code={c.conclusion}/>{c.reason?<span className="kv-hint">{statusLabel('evo_report_reason',c.reason)}</span>:null}</>},
      {label:'分级',cell:c=>c.change_class?<span title="由评估器评定，不能修改"><StatusBadge domain="evo_change_class" code={c.change_class}/></span>:'—'},
      {label:'门槛',cell:c=>gateSummary(c)},{label:'设计集',cell:c=>scoreText(c.scores.design)},{label:'留出集',cell:c=>scoreText(c.scores.validation)},
      {label:'详情',wide:true,cell:c=><ReportDetail c={c}/>}]}/>
      :<EmptyState kind="empty" subject="评估报告" next="候选经独立评估写入报告后会出现在这里。" compact/>}
  </>;
}
export function ReportDetail({c}:{c:ReviewCandidate}){
  return <details className="report-detail"><summary>{c.level} 报告 · {statusLabel('evo_dimension',c.dimension)}{c.published_version?` · 已发布为 v${c.published_version}`:''}</summary>
    <KeyValue label="门槛逐项结论" items={c.gates.map(g=>({key:statusLabel('evo_gate',g.gate),value:<StatusBadge domain="evo_gate_result" code={g.result}/>}))}/>
    <KeyValue label="指标与判定" items={[
      {key:'非劣判定',value:niText(c)},{key:'歧义率',value:pct(c.scores.ambiguity_rate)},{key:'回归数',value:c.scores.regressions??'—'},
      {key:'缺失用例',value:c.scores.missing_cases??'—'},{key:'用例',value:<>{c.cases} 个 · 集合摘要 <IdText value={c.cases_digest}/></>},
      {key:'门槛清单',value:c.hard_gates_version??'—'},{key:'报告摘要',value:<IdText value={c.report_digest}/>},{key:'报告时间',value:<Time value={c.recorded_at}/>}]}/>
  </details>;
}
/** D39-8: the stop switch, the release scope's third Action; a reason is required and audited. UX audit C12: stopping is a danger
 * action confirmed in place (the effect, the member's own reason); resuming is confirmed the same way without the danger tone. */
function StopForm({dimension,stopped}:{dimension:string;stopped:boolean}){
  const name=statusLabel('evo_dimension',dimension);
  return <WithMember>{member=><ConfirmedForm operation="set_evolution_stop" member={member} trigger={stopped?'恢复…':'停止…'} tone={stopped?undefined:'danger'}
    title={stopped?`确认恢复“${name}”的演进`:`确认停止“${name}”的演进`}
    impact={stopped?'影响：恢复后可以重新开评估和发布这个维度的候选。':'影响：停止期间不能开评估，也不能发布这个维度的候选；可以再恢复。'}
    body={reason=>({dimension,stopped:!stopped,reason})}/>}</WithMember>;
}
