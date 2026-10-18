/**
 * Web pages batch 6 (owner walkthrough 2026-10-18, F02/F07/F11), page-local helpers until the shared components converge:
 * - Operations: the governed controls after the facts (object, state, evidence, pending work), not above them as Page's action
 *   slot puts them. A page passes its `actions` here instead of to Page.
 * - IdRef: an object as type + short ID; the full value is one click away (expand) and copyable.
 * - NoWrap: a table cell value that never breaks (time, category, short codes).
 */
import {useState,type ReactNode} from 'react';

export function Operations({actions,title='操作'}:{actions?:ReactNode;title?:string}){
  if(!actions)return null;
  return <section className="workbench-ops" aria-labelledby="workbench-ops-title">
    <h2 id="workbench-ops-title">{title}</h2>
    <div className="workbench-actions" role="group" aria-label="可执行的操作">{actions}</div>
  </section>;
}

export function NoWrap({children}:{children:ReactNode}){return <span className="nowrap" style={{whiteSpace:'nowrap'}}>{children}</span>;}

/** `ends`: head…tail (identifiers that share a long prefix, e.g. principal ids); otherwise the last path segment's head
 * (a resource like eios:object:Consumer/<hash> shows the hash, its type is the `kind`). */
export function shortId(value:string,size=8,ends=false):string{
  if(ends)return value.length>size+6?value.slice(0,size-2)+'…'+value.slice(-4):value;
  const tail=value.split(/[/:]/).pop()||value;
  return tail.length>size+2?tail.slice(0,size)+'…':tail===value?value:'…'+tail;
}
export function IdRef({kind,value,size=8,ends=false}:{kind?:string;value:string;size?:number;ends?:boolean}){
  const [open,setOpen]=useState(false);const [copied,setCopied]=useState(false);
  const short=shortId(value,size,ends);
  async function copy(){try{await navigator.clipboard.writeText(value);setCopied(true);}catch{setOpen(true);}}
  return <span className="id-ref" style={{whiteSpace:open?'normal':'nowrap'}}>
    {kind?<span className="id-ref-kind">{kind} </span>:null}
    <button type="button" className="link id-ref-toggle" aria-expanded={open} title={value} onClick={()=>setOpen(!open)}><code style={open?{wordBreak:'break-all'}:undefined}>{open?value:short}</code></button>
    {short!==value?<button type="button" className="link id-ref-copy" style={{marginLeft:'0.5em'}} aria-label={`复制完整标识 ${short}`} onClick={()=>void copy()}>{copied?'已复制':'复制'}</button>:null}
  </span>;
}
