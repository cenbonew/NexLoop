import type {ReactNode} from 'react';
import type {UseQueryResult} from '@tanstack/react-query';
import {STATE_TEXT,SECTION_TEXT,kindOf,type Section} from '../api';
import {Announce,Empty} from '../ui';

/** Every page: one title, an `actions` slot for slice 2/3 governed Actions (empty by default), and exact states (AT-045).
 * `forbidden`: what this page says on a 403 instead of the generic text, when the refusal is the role's by design (whose page it
 * is and how to get there); still a 403, still role="alert". */
/** `toolbar` (tabs, filters) stays mounted while the data loads; with placeholder data the old rows stay, marked busy (F12). */
/** `announce`: what a screen reader hears once the data for the current tab/filter has arrived (e.g. "失败：3 条"). */
export function Page<T>({title,lead,query,actions,toolbar,empty,emptyKind='none',forbidden,announce,children}:{title:string;lead?:ReactNode;query:UseQueryResult<T>;actions?:ReactNode;announce?:(data:T)=>string;
  toolbar?:ReactNode;empty?:(data:T)=>boolean;emptyKind?:'none'|'filtered';forbidden?:ReactNode;children:(data:T)=>ReactNode}){
  return <section className="workbench-page" aria-labelledby="workbench-page-title">
    <h1 id="workbench-page-title">{title}</h1>
    {lead?<p className="workbench-lead">{lead}</p>:null}
    {actions?<div className="workbench-actions" role="group" aria-label="可执行的操作">{actions}</div>:null}
    {toolbar?<div className="workbench-toolbar">{toolbar}</div>:null}
    {announce?<Announce text={query.data!==undefined&&!query.isFetching?announce(query.data):''}/>:null}
    <div className="workbench-data" aria-busy={query.isFetching||undefined}>
    {query.isPending?<p role="status" className="workbench-loading">{STATE_TEXT.loading}</p>
      :query.isError?(forbidden&&kindOf(query.error)==='forbidden'?<div className="workbench-state workbench-state-forbidden" role="alert">{forbidden}</div>
        :<Failure kind={kindOf(query.error)} onRetry={()=>void query.refetch()}/>)
      :empty&&empty(query.data)?<Empty kind={emptyKind} title={emptyKind==='none'?STATE_TEXT.empty:undefined}/>
      :children(query.data)}</div>
  </section>;
}

export function Failure({kind,onRetry}:{kind:ReturnType<typeof kindOf>;onRetry?:()=>void}){
  return <div className={'workbench-state workbench-state-'+kind} role="alert">
    <Empty kind={kind==='forbidden'?'forbidden':'unavailable'} title={STATE_TEXT[kind]} next={({unauthenticated:'重新登录后再打开此页。',not_found:'返回列表重新选择。',invalid:'刷新页面后再打开。'} as Record<string,string>)[kind]}
      action={kind==='unavailable'&&onRetry?<button type="button" onClick={onRetry}>重试</button>:undefined}/>
  </div>;
}

/** One block of a partial response: forbidden/unavailable blocks are labelled, never shown as zero. */
export function Block<T>({title,section,children}:{title:string;section:Section<T>;children:(data:T)=>ReactNode}){
  return <article className={'workbench-block workbench-block-'+section.status} aria-label={title}>
    <h2>{title}</h2>
    {section.status==='ok'?children(section.data):<Empty kind={section.status} title={SECTION_TEXT[section.status]}/>}
  </article>;
}

export function Short({value}:{value:string}){return <code title={value}>{value.slice(0,12)}…</code>;}
