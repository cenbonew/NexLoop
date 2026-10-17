import {useState,type ReactNode} from 'react';
import {useQuery} from '@tanstack/react-query';
import {matchedText,readContact} from '../api';
import {Page,Short} from './Page';
import {when} from './Consumers';
import {DataTable} from '../DataTable';

export function Contact({actions}:{actions?:ReactNode}){
  const [all,setAll]=useState(false);
  const query=useQuery({queryKey:['workbench','contact',all],queryFn:()=>readContact(all)});
  return <Page title="联系限制" query={query} actions={actions}>{data=><>
    <label><input type="checkbox" checked={all} onChange={e=>setAll(e.target.checked)}/> 包括已解除</label>
    {data.restrictions.length?<DataTable label="联系限制" rows={data.restrictions} rowKey={r=>r.consumer_id} columns={[
      {label:'客户',main:true,cell:r=><Short value={r.consumer_id}/>},{label:'状态',cell:r=>r.active?'受限':'已解除'+(r.release_reason?`（${r.release_reason}）`:'')},
      {label:'命中',wide:true,cell:matchedText},{label:'控制 revision',num:true,cell:r=>r.control_revision},{label:'时间',cell:r=>when(r.restricted_at)}]}/>:<p className="note">暂无记录。</p>}
    <h2>来信必回的升级记录</h2>
    {data.escalations.length?<ul>{data.escalations.map(e=><li key={e.message_id}>{e.reason} · 客户 <Short value={e.consumer_id}/> · {when(e.escalated_at)}</li>)}</ul>:<p className="note">暂无记录。</p>}
    <p className="note">只有负责人能解除联系限制；解除后之前被拒的意图不会重放，计划将重新评估。</p>
  </>}</Page>;
}
