/**
 * Workbench UI kit, batch 1 (docs/design/workbench-ui-spec.md §2–§8, §13; interfaces frozen in its appendix A so batches 2 and 3
 * can apply them in parallel). Styling only through the tokens in styles.css; no inline styles.
 */
import {useState,type ReactNode} from 'react';
import {Announce} from './ui';
import {statusLabel} from './status-labels';
import type {Section} from './api';

// ---- §2 page skeleton ----
/** The page head: one h1, the lead (what this page answers), the page's actions on the right, then the toolbar. */
export function PageHeader({title,lead,actions,toolbar,titleId='workbench-page-title'}:{title:ReactNode;lead?:ReactNode;actions?:ReactNode;toolbar?:ReactNode;titleId?:string}){
  return <>
    <header className="page-header">
      <div className="page-header-text"><h1 id={titleId}>{title}</h1>{lead?<p className="workbench-lead">{lead}</p>:null}</div>
      {actions?<div className="workbench-actions page-header-actions" role="group" aria-label="可执行的操作">{actions}</div>:null}
    </header>
    {toolbar?<div className="workbench-toolbar">{toolbar}</div>:null}
  </>;
}

// ---- §8 states ----
export type StateKind='loading'|'empty'|'filtered'|'unavailable'|'forbidden'|'not_enabled';
/** Chinese next to a Latin entity name gets a space ("暂无 Artifact", "Embedding 预算暂不可用"); next to Chinese none. */
const before=(s:string)=>s&&/^[A-Za-z0-9]/.test(s)?' '+s:s,after=(s:string)=>s&&/[A-Za-z0-9]$/.test(s)?s+' ':s;
const STATE:Record<StateKind,{title:(s:string)=>string;next:string;mark:string;alert:boolean}>={
  loading:{title:s=>`正在加载${before(s)}`,next:'',mark:'',alert:false},
  empty:{title:s=>`暂无${before(s)}`,next:'有新记录时会出现在这里。',mark:'○',alert:false},
  filtered:{title:s=>`没有符合筛选条件的${before(s)}`,next:'换一个筛选条件，或选择“全部”。',mark:'○',alert:false},
  unavailable:{title:s=>`${after(s)}暂不可用`,next:'未加载，不代表数量为零。',mark:'!',alert:true},
  forbidden:{title:s=>`无权查看${before(s)}`,next:'需要时请负责人调整角色；这不表示没有数据。',mark:'!',alert:true},
  not_enabled:{title:s=>`${after(s)}尚未启用`,next:'属于下一阶段，当前不提供任何操作。',mark:'—',alert:false},
};
/** One loading / empty / unavailable / forbidden state: a fixed title per kind ("暂无 X", "X 暂不可用"), an optional specific
 * reason, the next step, a retry button where it helps and the technical details folded away (HTTP status, codes). */
export function EmptyState({kind,subject='',reason,next,onRetry,details,compact}:{kind:StateKind;subject?:string;reason?:ReactNode;next?:ReactNode;
  onRetry?:()=>void;details?:ReactNode;compact?:boolean}){
  const t=STATE[kind];
  const subjectText=subject||(kind==='empty'||kind==='filtered'?'数据':kind==='loading'?'':'此内容');
  if(kind==='loading')return <div className={'state state-loading'+(compact?' state-compact':'')} role="status" aria-busy="true" data-state="loading">
    <span className="sr-only">{t.title(subjectText)}</span><span className="skeleton-line"/><span className="skeleton-line"/><span className="skeleton-line short"/></div>;
  return <div className={`state state-${kind}`+(compact?' state-compact':'')} role={t.alert?'alert':'status'} data-state={kind}>
    <p className="state-title"><span className="state-mark" aria-hidden="true">{t.mark}</span>{t.title(subjectText)}{reason?<>：{reason}</>:null}</p>
    {next??t.next?<p className="state-next">{next??t.next}</p>:null}
    {onRetry&&kind==='unavailable'?<button type="button" onClick={onRetry}>重试</button>:null}
    {details?<details className="state-details"><summary>技术细节</summary>{details}</details>:null}
  </div>;
}

// ---- §3 cards and grids ----
/** A card: title (and a status badge on its right), content, and a footer kept at the bottom so a row's buttons line up. A card
 * whose data is not there keeps its place and shows the state instead (never a different card style). */
export function Card({title,badge,footer,compact,state,subject,children,label}:{title:ReactNode;badge?:ReactNode;footer?:ReactNode;compact?:boolean;
  state?:'unavailable'|'forbidden';subject?:string;children?:ReactNode;label?:string}){
  return <article className={'card'+(compact?' card-compact':'')+(state?' card-'+state:'')} aria-label={label??(typeof title==='string'?title:undefined)}>
    <div className="card-head"><h2>{title}</h2>{badge}</div>
    <div className="card-body">{state?<EmptyState kind={state} subject={subject??(typeof title==='string'?title:'')} compact/>:children}</div>
    {footer&&!state?<div className="card-foot">{footer}</div>:null}
  </article>;
}
/** Equal-height cards in one grid (min column width, default 18rem). */
export function CardGrid({children,min,label}:{children:ReactNode;min?:'14rem'|'18rem'|'24rem';label?:string}){
  return <div className={'card-grid'+(min&&min!=='18rem'?' card-grid-'+min.replace('rem',''):'')} aria-label={label}>{children}</div>;
}
/** One number with its label and a line of explanation; the whole card is a link when it opens something. */
export function MetricCard({label,value,hint,onOpen,href,tone}:{label:string;value:ReactNode;hint?:ReactNode;onOpen?:()=>void;href?:string;tone?:'ok'|'limited'|'fail'}){
  const body=<><span className="metric-label">{label}</span><strong className="metric-value">{value}</strong>{hint?<span className="metric-hint">{hint}</span>:null}</>;
  const cls='metric-card'+(tone?' metric-'+tone:'');
  if(onOpen||href)return <a className={cls+' metric-link'} href={href??'#'} aria-label={`${label} ${typeof value==='string'||typeof value==='number'?value:''}，查看`}
    onClick={event=>{if(onOpen){event.preventDefault();onOpen();}}}>{body}</a>;
  return <div className={cls}>{body}</div>;
}

// ---- §4 key-value ----
export type KeyValueItem={key:string;value:ReactNode;hint?:ReactNode};
/** A description list: keys muted on the left (on top below 640 px), values right; values are badges, ids or times, never raw JSON. */
export function KeyValue({items,label}:{items:KeyValueItem[];label?:string}){
  return <dl className="key-value" aria-label={label}>{items.map(i=><div key={i.key} className="kv-row"><dt>{i.key}</dt>
    <dd>{i.value}{i.hint?<span className="kv-hint">{i.hint}</span>:null}</dd></div>)}</dl>;
}

// ---- §6 long ids ----
export function shorten(value:string,head=8,tail=4):string{return value.length<=head+tail+1?value:value.slice(0,head)+'…'+value.slice(-tail);}
/** A long identifier: kind in words, the head…tail form (monospace), the full value on hover and focus, a copy button (announces
 * 已复制); `name` replaces the hash as the visible text when the object has one; `expandable` shows the full value on click. */
export function IdText({value,kind,name,head=8,tail=4,copy=true,expandable}:{value:string;kind?:string;name?:string|null;head?:number;tail?:number;copy?:boolean;expandable?:boolean}){
  const [open,setOpen]=useState(false);const [copied,setCopied]=useState(false);
  const full=(kind?kind+' ':'')+value;
  const short=shorten(value,head,tail);
  const text=name?<span className="id-name">{name}</span>:expandable?<button type="button" className="id-code id-expand" aria-expanded={open} title={full} onClick={()=>setOpen(!open)}>{open?value:short}</button>
    :<code className="id-code" title={full} tabIndex={0} aria-label={full}>{short}</code>;
  function doCopy(){void navigator.clipboard?.writeText(value).then(()=>{setCopied(true);setTimeout(()=>setCopied(false),1500);}).catch(()=>undefined);}
  return <span className="id-text" data-id={value}>{kind?<span className="id-kind">{kind}</span>:null}{text}
    {name?<code className="id-code id-secondary" title={full}>{short}</code>:null}
    {copy?<button type="button" className="id-copy" aria-label={`复制${kind?kind+' ':''}ID`} title="复制完整 ID" onClick={doCopy}>{copied?'✓':'⧉'}</button>:null}
    {copied?<Announce text="已复制"/>:null}</span>;
}

// ---- adapters for partial responses and scopes ----
/** A card over one block of a partial response (api Section): forbidden / unavailable keep the card's place and say so. */
export function SectionCard<T>({title,section,footer,children}:{title:string;section:Section<T>;footer?:ReactNode;children:(data:T)=>ReactNode}){
  return section.status==='ok'?<Card title={title} footer={footer}>{children(section.data)}</Card>:<Card title={title} state={section.status}/>;
}
const HEX=/^(?:consumer:)?([a-f0-9]{64})$/;
/** A control scope in words: the scope kind's label, then the reference (a Consumer hash as IdText, the tenant-wide "*" as
 * nothing, anything else as code), never "consumer:<64 hex>". */
export function ScopeText({kind,value}:{kind:string;value:string}){
  const hex=HEX.exec(value);
  return <span className="scope-text">{statusLabel('scope_kind',kind)}{value==='*'||!value?null:<> {hex?<IdText value={hex[1]}/>:<code className="id-code">{value.replace(/^eios:action:/,'')}</code>}</>}</span>;
}
