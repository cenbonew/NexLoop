import {useState,type ReactNode} from 'react';
import {useQuery} from '@tanstack/react-query';
import {matchedText,readContact} from '../api';
import {Page,Short} from './Page';

export function Contact({actions}:{actions?:ReactNode}){
  const [all,setAll]=useState(false);
  const query=useQuery({queryKey:['workbench','contact',all],queryFn:()=>readContact(all)});
  return <Page title="联系限制" query={query} actions={actions}>{data=><>
    <label><input type="checkbox" checked={all} onChange={e=>setAll(e.target.checked)}/> 包括已解除</label>
    {data.restrictions.length?<table><thead><tr><th>客户</th><th>状态</th><th>命中</th><th>控制 revision</th><th>时间</th></tr></thead><tbody>
      {data.restrictions.map(r=><tr key={r.consumer_id}><td><Short value={r.consumer_id}/></td><td>{r.active?'受限':'已解除'+(r.release_reason?`（${r.release_reason}）`:'')}</td>
        <td>{matchedText(r)}</td><td>{r.control_revision}</td><td>{r.restricted_at}</td></tr>)}</tbody></table>:<p className="note">暂无记录。</p>}
    <h2>来信必回的升级记录</h2>
    {data.escalations.length?<ul>{data.escalations.map(e=><li key={e.message_id}>{e.reason} · 客户 <Short value={e.consumer_id}/> · {e.escalated_at}</li>)}</ul>:<p className="note">暂无记录。</p>}
    <p className="note">只有负责人能解除联系限制；解除后之前被拒的意图不会重放，计划将重新评估。</p>
  </>}</Page>;
}
