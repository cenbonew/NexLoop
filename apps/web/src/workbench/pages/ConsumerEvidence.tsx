/**
 * K5 (docs/design/k5-consumer-evidence-views.md §2.3): the consumer profile card at the top of the consumer detail, on existing reads only.
 * First the upcoming commitments (open, due from now, soonest first, at most UPCOMING), then the properties as the Object read gives them
 * (GET /objects/Consumer/{id}: object revision, per-property ok / withheld with its reason / missing). A withheld property keeps its 受限
 * badge and never reads as empty; properties without a value are counted, not drawn as dashes (K4l). Without the Object read (no
 * permission or not available) the card says what the consumer read says, as before.
 */
import {useState,type ReactNode} from 'react';
import {useQuery} from '@tanstack/react-query';
import {kindOf,readCommitments,readObjectValues,type ConsumerDetail} from '../api';
import {EmptyState,KeyValue} from '../kit';
import {commitmentSummary,dueText} from './Commitments';
import {NO_VALUES_TEXT,PropertyCell} from './WorldInstances';
import {glossText,useWorldGlosses} from '../Gloss';

export const UPCOMING=3;
export const CONSUMER_NO_VALUES=NO_VALUES_TEXT.replace('这些对象','这位客户');

function Upcoming({consumerId}:{consumerId:string}){
  const [now]=useState(()=>new Date().toISOString());
  const q=useQuery({queryKey:['workbench','commitments','upcoming',consumerId,now],
    queryFn:()=>readCommitments({status_group:'open',consumer_id:consumerId,due_after:now,sort:'due_at',order:'asc'},UPCOMING)});
  if(q.isPending)return <EmptyState kind="loading" subject="即将发生的承诺" compact/>;
  if(q.isError)return <EmptyState kind={kindOf(q.error)==='forbidden'?'forbidden':'unavailable'} subject="即将发生的承诺" onRetry={()=>void q.refetch()} compact/>;
  const items=q.data.commitments.slice(0,UPCOMING);
  if(!items.length)return <p className="note">没有即将到期的承诺</p>;
  return <ol className="profile-upcoming">{items.map(c=><li key={c.commitment_id}><span className="profile-due">{dueText(c)}</span> {commitmentSummary(c)}</li>)}</ol>;
}

function Properties({consumerId,fallback}:{consumerId:string;fallback:ReactNode}){
  const glosses=useWorldGlosses();
  const q=useQuery({queryKey:['workbench','object','Consumer',consumerId],queryFn:()=>readObjectValues('Consumer',consumerId),retry:false});
  if(q.isPending)return <EmptyState kind="loading" subject="属性" compact/>;
  if(q.isError)return <div className="note">{fallback}</div>;
  const v=q.data;const shown=v.properties.filter(p=>p.status!=='missing'&&!(p.status==='ok'&&p.value===undefined));
  const missing=v.properties.length-shown.length;
  return <>
    <p className="note profile-revision">修订 r{v.revision} · Schema v{v.schema_version}</p>
    {shown.length?<KeyValue label="客户的 Property" items={shown.map(p=>({key:glossText(p.name,glosses.property('Consumer',p.name).gloss),value:<PropertyCell p={p}/>}))}/>:null}
    {!shown.length?<p className="note">{CONSUMER_NO_VALUES}</p>:missing?<p className="note">另有 {missing} 个属性还没有值，未列出</p>:null}
  </>;
}

/** The card; `fallback` is the consumer read's own wording for when the Object read is not available to this member, `groups` the
 * owner-restricted property groups (ADR-025), named only. */
export function ProfileCard({data,fallback,groups}:{data:ConsumerDetail;fallback:ReactNode;groups:ReactNode}){
  return <section className="detail-section profile-card" aria-labelledby="consumer-profile">
    <h2 id="consumer-profile">画像</h2>
    <h3>即将发生的承诺</h3><Upcoming consumerId={data.consumer_id}/>
    <h3 id="consumer-properties">属性</h3><Properties consumerId={data.consumer_id} fallback={fallback}/>
    {groups?<p className="note">{groups}</p>:null}
  </section>;
}
