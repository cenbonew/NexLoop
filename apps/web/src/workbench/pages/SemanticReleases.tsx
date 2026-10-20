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
import {kindOf,readSemanticReleases,STATE_TEXT,type PublishableCandidate,type SemanticRelease} from '../api';
import {Form,WithMember} from '../actions';
import {offers,readMe} from '../api';
import {DataTable} from '../DataTable';
import {Empty,StatusBadge} from '../ui';
import {Failure} from './Page';
import {when} from './Consumers';

export const CLASS_TEXT:Record<'patch'|'minor',string>={patch:'补丁级',minor:'次要级'};
const KIND_TEXT:Record<'alias'|'description',string>={alias:'别名',description:'补充说明'};
const shortId=(id:string)=>id.length>14?id.slice(0,8)+'…'+id.slice(-4):id;
export const NOTE_LIMIT=200;

/** Shown only once /me has answered with a release Action; while it loads or fails nothing is shown (the page's own state speaks). */
export function SemanticReleaseSection(){
  const member=useQuery({queryKey:['workbench','me'],queryFn:readMe,refetchOnWindowFocus:false});
  return member.isSuccess&&(offers(member.data,'publish_semantic_release')||offers(member.data,'rollback_semantic_release'))?<Releases/>:null;
}

function Releases(){
  const query=useQuery({queryKey:['workbench','semantic-releases'],queryFn:readSemanticReleases});
  return <section className="workbench-block" aria-labelledby="semantic-release-title">
    <h2 id="semantic-release-title" style={{marginTop:0}}>语义发布</h2>
    <p className="note">只发布通过独立评估的候选；分级由评估报告给出，破坏性变更不能发布。回退会生成更高的版本号，不改动已有 Claim。</p>
    {query.isPending?<p role="status">{STATE_TEXT.loading}</p>
      :query.isError?(kindOf(query.error)==='forbidden'?<Empty kind="forbidden"/>:<Failure kind={kindOf(query.error)} onRetry={()=>void query.refetch()}/>)
      :<>
        <p role="status">当前语义版本：{query.data.current?<strong>v{query.data.current}</strong>:'尚未发布'}</p>
        <h3>可发布的候选</h3>
        {query.data.publishable.length?<DataTable label="可发布的候选" rows={query.data.publishable} rowKey={c=>c.candidate_id} columns={[
          {label:'候选',main:true,cell:c=><code title={c.candidate_id}>{shortId(c.candidate_id)}</code>},
          {label:'分级',cell:c=><StatusBadge text={CLASS_TEXT[c.change_class]} tone="ok" code={c.change_class}/>},
          {label:'变更',cell:c=>KIND_TEXT[c.change_kind]},{label:'目标',wide:true,cell:c=>c.target_refs.join('、')},
          {label:'发布',cell:c=>c.parent_ok?<PublishForm candidate={c}/>:<StatusBadge text="基线已变化，需重新评估" tone="limited" code="parent_moved"/>}]}/>
          :<Empty kind="none" title="暂无可发布的候选" next="候选通过独立评估、且分级为补丁级或次要级后会出现在这里。"/>}
        <h3>版本历史</h3>
        {query.data.releases.length?<>
          <DataTable label="语义版本历史" rows={query.data.releases} rowKey={r=>r.version} columns={[
            {label:'版本',main:true,cell:r=><>v{r.version}{r.version===query.data.current?<> <StatusBadge text="当前" tone="ok" code="current"/></>:null}</>},
            {label:'方式',cell:r=>r.reason==='publish'?`发布（${r.change_class?CLASS_TEXT[r.change_class]:'—'}）`:`回退到 v${r.rolled_back_to}`},
            {label:'内容',cell:r=>itemsText(r)},{label:'回退报告',wide:true,cell:r=>reportText(r)},
            {label:'原因',cell:r=>r.owner_reason},{label:'时间',cell:r=><span style={{whiteSpace:'nowrap'}}>{when(r.recorded_at)}</span>}]}/>
          {query.data.current&&query.data.releases.length>1?<RollbackForm current={query.data.current} versions={query.data.releases.map(r=>r.version)}/>:null}
        </>:<Empty kind="none" title="暂无语义版本" next="发布第一个候选后，版本历史会出现在这里。"/>}
      </>}
  </section>;
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
    <div className="field"><label htmlFor={`release-ref-${id}`}>目标</label>
      <select id={`release-ref-${id}`} value={ref} onChange={e=>setRef(e.target.value)}>{candidate.target_refs.map(r=><option key={r} value={r}>{r}</option>)}</select></div>
    <div className="field"><label htmlFor={`release-text-${id}`}>{alias?'别名':'补充说明'}</label>
      {alias?<input id={`release-text-${id}`} value={body} maxLength={200} onChange={e=>setBody(e.target.value)}/>
        :<textarea id={`release-text-${id}`} value={body} maxLength={NOTE_LIMIT} rows={3} onChange={e=>setBody(e.target.value)}/>}
      <p className="note">{alias?'必须是候选证据中未命中的那个词（服务端按哈希核对）。':`最多 ${NOTE_LIMIT} 字，进入 Context 的语义段。`}</p></div>
    <div className="field"><label htmlFor={`release-reason-${id}`}>原因</label><input id={`release-reason-${id}`} value={reason} onChange={e=>setReason(e.target.value)}/></div>
  </Form>}</WithMember>;
}

function RollbackForm({current,versions}:{current:string;versions:string[]}){
  const targets=versions.filter(v=>v!==current);
  const [target,setTarget]=useState(targets[0]??'');const [reason,setReason]=useState('');
  const ok=!!target&&reason.trim().length>0;
  return <WithMember>{member=><Form operation="rollback_semantic_release" member={member} title="回退" disabled={!ok}
    body={()=>ok?{target_version:target,reason:reason.trim()}:null}>
    <p className="note">回退到所选版本的内容，并生成一个更高的版本号。已有 Claim 不会被改动；回退报告列出期间经被移除别名匹配的 Claim 数量与 ID，供人工复核。不会自动回退。</p>
    <div className="field"><label htmlFor="rollback-target">回退到</label>
      <select id="rollback-target" value={target} onChange={e=>setTarget(e.target.value)}>{targets.map(v=><option key={v} value={v}>v{v}</option>)}</select></div>
    <div className="field"><label htmlFor="rollback-reason">原因</label><input id="rollback-reason" value={reason} onChange={e=>setReason(e.target.value)}/></div>
  </Form>}</WithMember>;
}
