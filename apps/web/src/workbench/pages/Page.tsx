import type {ReactNode} from 'react';
import type {UseQueryResult} from '@tanstack/react-query';
import {STATE_TEXT,SECTION_TEXT,kindOf,type Section} from '../api';

/** Every page: one title, an `actions` slot for slice 2/3 governed Actions (empty by default), and exact states (AT-045). */
export function Page<T>({title,query,actions,empty,children}:{title:string;query:UseQueryResult<T>;actions?:ReactNode;empty?:(data:T)=>boolean;children:(data:T)=>ReactNode}){
  return <section className="workbench-page" aria-labelledby="workbench-page-title">
    <h1 id="workbench-page-title">{title}</h1>
    {actions?<div className="workbench-actions" role="group" aria-label="可执行的操作">{actions}</div>:null}
    {query.isPending?<p role="status">{STATE_TEXT.loading}</p>
      :query.isError?<Failure kind={kindOf(query.error)} onRetry={()=>void query.refetch()}/>
      :empty&&empty(query.data)?<p className="note" role="status">{STATE_TEXT.empty}</p>
      :children(query.data)}
  </section>;
}

export function Failure({kind,onRetry}:{kind:ReturnType<typeof kindOf>;onRetry?:()=>void}){
  return <div className={'workbench-state workbench-state-'+kind} role="alert">
    <p>{STATE_TEXT[kind]}</p>
    {kind==='unavailable'&&onRetry?<button type="button" onClick={onRetry}>重试</button>:null}
  </div>;
}

/** One block of a partial response: forbidden/unavailable blocks are labelled, never shown as zero. */
export function Block<T>({title,section,children}:{title:string;section:Section<T>;children:(data:T)=>ReactNode}){
  return <article className={'workbench-block workbench-block-'+section.status} aria-label={title}>
    <h2>{title}</h2>
    {section.status==='ok'?children(section.data):<p className="note" role="status">{SECTION_TEXT[section.status]}</p>}
  </article>;
}

export function Short({value}:{value:string}){return <code title={value}>{value.slice(0,12)}…</code>;}
