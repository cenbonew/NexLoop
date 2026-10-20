import {useState,type ReactNode} from 'react';
import {useQuery} from '@tanstack/react-query';
import {matchedText,offers,readContact,readMe,type Restriction} from '../api';
import {Page} from './Page';
import {StatusBadge,Time} from '../ui';
import {EmptyState,IdText} from '../kit';
import {DataTable} from '../DataTable';
import {ReleaseForm,WithMember} from '../actions';

/** Contact restrictions; the owner releases one from that customer's row (F06): its refusal source, time and scope beside the form. */
export function Contact({actions}:{actions?:ReactNode}){
  const [all,setAll]=useState(false);const [releasing,setReleasing]=useState<string|null>(null);
  const query=useQuery({queryKey:['workbench','contact',all],queryFn:()=>readContact(all)});
  const member=useQuery({queryKey:['workbench','me'],queryFn:readMe,refetchOnWindowFocus:false});
  const canRelease=!!member.data&&offers(member.data,'release_contact_restriction');
  const chosen=(rows:Restriction[])=>rows.find(r=>r.consumer_id===releasing&&r.active)??null;
  return <Page title="联系限制" query={query} lead="客户明确拒绝后自动受限的记录；受限客户不会收到任何主动联系，只有负责人能从客户的记录解除。"
    toolbar={<div className="list-tools"><label className="inline"><input type="checkbox" checked={all} onChange={e=>setAll(e.target.checked)}/>包括已解除</label></div>}>{data=>{const target=chosen(data.restrictions);return <>
    <p className="workbench-summary" role="status">当前受限客户 {data.restrictions.filter(r=>r.active).length} 位{all?`（含已解除共 ${data.restrictions.length} 条）`:''}。</p>
    <DataTable label="联系限制" empty={{subject:'联系限制记录'}} rows={data.restrictions} rowKey={r=>r.consumer_id} columns={[
      {label:'客户',main:true,cell:r=><IdText kind="客户" value={r.consumer_id}/>},
      {label:'状态',cell:r=><>{r.active?<StatusBadge text="受限" tone="limited"/>:<StatusBadge text="已解除" tone="neutral"/>}{!r.active&&r.release_reason?<span className="kv-hint">{r.release_reason}</span>:null}</>},
      {label:'命中',wide:true,cell:matchedText},{label:'控制修订号',num:true,cell:r=>r.control_revision},{label:'时间',cell:r=><Time value={r.restricted_at}/>},
      ...(canRelease?[{label:'操作',cell:(r:Restriction)=>r.active?<button type="button" className="link" aria-pressed={releasing===r.consumer_id}
        onClick={()=>setReleasing(releasing===r.consumer_id?null:r.consumer_id)}>解除…</button>:'—'}]:[])]}/>
    {target?<WithMember>{m=><ReleaseForm key={target.consumer_id} member={m} consumerId={target.consumer_id}
      context={<p>拒绝来源：{matchedText(target)} · 受限时间 <Time value={target.restricted_at}/> · 范围：这位客户的全部主动联系（控制修订号 {target.control_revision}）</p>}/>}</WithMember>:null}
    <h2>来信必回的升级记录</h2>
    <DataTable label="来信必回的升级记录" empty={{subject:'升级记录'}} rows={data.escalations} rowKey={e=>e.message_id} columns={[
      {label:'原因',main:true,cell:e=><StatusBadge domain="escalation_reason" code={e.reason}/>},{label:'客户',cell:e=><IdText kind="客户" value={e.consumer_id}/>},
      {label:'时间',cell:e=><Time value={e.escalated_at}/>}]}/>
    <p className="note">只有负责人能解除联系限制，且要从具体客户的记录进入；解除后之前被拒的意图不会重放，计划将重新评估。</p>
    {actions}
  </>;}}</Page>;
}
