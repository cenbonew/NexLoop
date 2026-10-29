import type {ReactNode} from 'react';
import type {UseQueryResult} from '@tanstack/react-query';
import {STATE_TEXT,kindOf,technical} from '../api';
import {Announce} from '../ui';
import {EmptyState,PageHeader} from '../kit';

/** Every page: one title, an `actions` slot for slice 2/3 governed Actions (empty by default), and exact states (AT-045).
 * `forbidden`: what this page says on a 403 instead of the generic text, when the refusal is the role's by design (whose page it
 * is and how to get there); still a 403, still role="alert". */
/** `toolbar` (tabs, filters) stays mounted while the data loads; with placeholder data the old rows stay, marked busy (F12). */
/** `announce`: what a screen reader hears once the data for the current tab/filter has arrived (e.g. "失败：3 条"). */
export function Page<T>({title,lead,query,actions,toolbar,empty,emptyKind='none',emptyState,forbidden,announce,children}:{title:ReactNode;lead?:ReactNode;query:UseQueryResult<T>;actions?:ReactNode;announce?:(data:T)=>string;
  toolbar?:ReactNode;empty?:(data:T)=>boolean;emptyKind?:'none'|'filtered';/** UX audit C12: the page's own empty state instead of the generic one. */emptyState?:ReactNode;forbidden?:ReactNode;children:(data:T)=>ReactNode}){
  return <section className="workbench-page" aria-labelledby="workbench-page-title">
    <PageHeader title={title} lead={lead} actions={actions} toolbar={toolbar}/>
    {announce?<Announce text={query.data!==undefined&&!query.isFetching?announce(query.data):''}/>:null}
    <div className="workbench-data" aria-busy={query.isFetching||undefined}>
    {query.isPending?<p role="status" className="workbench-loading">{STATE_TEXT.loading}</p>
      :query.isError?(forbidden&&kindOf(query.error)==='forbidden'?<div className="workbench-state workbench-state-forbidden" role="alert">{forbidden}</div>
        :<Failure kind={kindOf(query.error)} onRetry={()=>void query.refetch()}/>)
      :empty&&empty(query.data)?emptyState??<EmptyState kind={emptyKind==='filtered'?'filtered':'empty'} title={emptyKind==='none'?'暂无记录':undefined}/>
      :children(query.data)}</div>
  </section>;
}

/** UX audit C1: the page's failure is the kit's state (its alert, the next step, a retry where it helps, the code in 技术细节). */
export function Failure({kind,onRetry}:{kind:ReturnType<typeof kindOf>;onRetry?:()=>void}){
  return <EmptyState kind={kind==='forbidden'?'forbidden':'unavailable'} title={STATE_TEXT[kind]}
    next={({unauthenticated:'重新登录后再打开此页。',not_found:'返回列表重新选择。',invalid:'刷新页面后再打开。',invalid_request:'检查筛选条件后再试。'} as Record<string,string>)[kind]}
    onRetry={kind==='unavailable'?onRetry:undefined} details={technical(kind)}/>;
}
