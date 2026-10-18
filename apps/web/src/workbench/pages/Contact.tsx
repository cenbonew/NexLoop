import {useState,type ReactNode} from 'react';
import {useQuery} from '@tanstack/react-query';
import {matchedText,offers,readContact,readMe,type Restriction} from '../api';
import {Page,Short} from './Page';
import {when} from './Consumers';
import {DataTable} from '../DataTable';
import {ReleaseForm,WithMember} from '../actions';
import {NoWrap} from './PageOps';

/** Contact restrictions; the owner releases one from that customer's row (F06): its refusal source, time and scope beside the form. */
export function Contact({actions}:{actions?:ReactNode}){
  const [all,setAll]=useState(false);const [releasing,setReleasing]=useState<string|null>(null);
  const query=useQuery({queryKey:['workbench','contact',all],queryFn:()=>readContact(all)});
  const member=useQuery({queryKey:['workbench','me'],queryFn:readMe,refetchOnWindowFocus:false});
  const canRelease=!!member.data&&offers(member.data,'release_contact_restriction');
  const chosen=(rows:Restriction[])=>rows.find(r=>r.consumer_id===releasing&&r.active)??null;
  return <Page title="联系限制" query={query}>{data=>{const target=chosen(data.restrictions);return <>
    <p className="workbench-summary" role="status">当前受限客户 {data.restrictions.filter(r=>r.active).length} 位{all?`（含已解除共 ${data.restrictions.length} 条）`:''}。</p>
    <label className="choice"><input type="checkbox" checked={all} onChange={e=>setAll(e.target.checked)}/>包括已解除</label>
    {data.restrictions.length?<DataTable label="联系限制" rows={data.restrictions} rowKey={r=>r.consumer_id} columns={[
      {label:'客户',main:true,cell:r=><Short value={r.consumer_id}/>},{label:'状态',cell:r=>r.active?'受限':'已解除'+(r.release_reason?`（${r.release_reason}）`:'')},
      {label:'命中',wide:true,cell:matchedText},{label:'控制 revision',num:true,cell:r=>r.control_revision},{label:'时间',cell:r=><NoWrap>{when(r.restricted_at)}</NoWrap>},
      ...(canRelease?[{label:'操作',cell:(r:Restriction)=>r.active?<button type="button" className="link" aria-pressed={releasing===r.consumer_id}
        onClick={()=>setReleasing(releasing===r.consumer_id?null:r.consumer_id)}>解除…</button>:'—'}]:[])]}/>:<p className="note">暂无记录。</p>}
    {target?<WithMember>{m=><ReleaseForm key={target.consumer_id} member={m} consumerId={target.consumer_id}
      context={<p>拒绝来源：{matchedText(target)} · 受限时间 {when(target.restricted_at)} · 范围：这位客户的全部主动联系（控制 revision {target.control_revision}）</p>}/>}</WithMember>:null}
    <h2>来信必回的升级记录</h2>
    {data.escalations.length?<ul>{data.escalations.map(e=><li key={e.message_id}>{e.reason} · 客户 <Short value={e.consumer_id}/> · {when(e.escalated_at)}</li>)}</ul>:<p className="note">暂无记录。</p>}
    <p className="note">只有负责人能解除联系限制，且要从具体客户的记录进入；解除后之前被拒的意图不会重放，计划将重新评估。</p>
    {actions}
  </>;}}</Page>;
}
