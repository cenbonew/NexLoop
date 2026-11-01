/**
 * K4e: the home "待我处理" block (workbench-pending v1, GET /api/v1/workbench/pending; design notes l9-k4-ui v2). Per category the
 * count, the longest wait and a link to where it is handled; all_clear is one line. Which categories and counts a member sees is the
 * server's (filtered by what this member may act on); this block decides nothing by role. Read-only: no write entry here.
 * Questions are not shown until context-pack v7 is enabled (owner decision, L6 inert release): the question category and the question
 * list are dropped even when the server returns them. Alert acknowledgement is not a category.
 */
import {useQuery} from '@tanstack/react-query';
import {ageText,readPending,type PendingCategory,type PendingCount} from '../api';
import type {Route} from '../route';
import {Card,EmptyState} from '../kit';
import {Time} from '../ui';

/** Hidden until context-pack v7: flip only with that release (and its test). */
export const QUESTIONS_SHOWN=false;
export const PENDING_LABEL:Record<PendingCategory,string>={question:'问题',approval:'待批审批',remediation:'待处理补救',reply_escalation:'回复升级',
  takeover_expiry:'即将到期的人工接管',candidate_review:'待审核候选定义'};
const UUID=/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
/** Where a category is handled; the oldest item itself when its page has a detail route (a candidate's review evidence). */
export function pendingTarget(c:PendingCount):{route:Route;text:string}|null{
  switch(c.category){
    case 'approval':return {route:{page:'approvals'},text:'去审批'};
    case 'remediation':return {route:{page:'actions'},text:'去处理'};
    case 'reply_escalation':return {route:{page:'contact'},text:'查看升级'};
    case 'candidate_review':{const id=c.oldest_ref?.startsWith('candidate:')?c.oldest_ref.slice(10):'';
      return {route:UUID.test(id)?{page:'knowledge',id}:{page:'knowledge'},text:UUID.test(id)?'审核最早一项':'去审核'};}
    case 'takeover_expiry':return null;  // listed in 人工接管 on this page; no page of its own
    case 'question':return null;
  }
}
export const shownCategories=(categories:PendingCount[])=>categories.filter(c=>c.count>0&&(QUESTIONS_SHOWN||c.category!=='question'));

export function PendingBlock({go,href}:{go:(route:Route)=>void;href:(route:Route)=>string}){
  const query=useQuery({queryKey:['workbench','pending'],queryFn:readPending});
  const title='待我处理';
  if(query.isPending)return <Card title={title}><EmptyState kind="loading" subject={title} compact/></Card>;
  if(query.isError)return <Card title={title} state="unavailable" subject={title}/>;
  const shown=shownCategories(query.data.categories);
  if(!shown.length)return <Card title={title}><EmptyState kind="empty" title="没有待处理事项" compact
    next={query.data.last_health_check_at?<>最近一次健康检查：<Time value={query.data.last_health_check_at}/></>:'还没有健康检查记录'}/></Card>;
  return <Card title={title}><ul className="pending-list">{shown.map(c=>{const t=pendingTarget(c);
    return <li key={c.category} data-category={c.category}><span className="pending-label">{PENDING_LABEL[c.category]}</span>
      <span className="badge-count">{c.count}</span><span className="note">最久等待 {ageText(c.oldest_waiting_seconds)}</span>
      {t?<a href={href(t.route)} aria-label={`${t.text}：${PENDING_LABEL[c.category]}（${c.count}）`}
        onClick={e=>{if(e.metaKey||e.ctrlKey||e.shiftKey)return;e.preventDefault();go(t.route);}}>{t.text} →</a>
        :c.category==='takeover_expiry'?<span className="note">见本页“人工接管”</span>:null}</li>;})}</ul></Card>;
}
