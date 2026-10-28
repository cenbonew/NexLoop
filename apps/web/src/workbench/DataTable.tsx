import {useLayoutEffect,useRef,useState,type ReactNode} from 'react';
import {EmptyState} from './kit';

/** One column: the header text, the cell, and how it lays out. `main` is the row's title in the narrow card view (and the column
 * kept in view while the table scrolls sideways); `num` is a right-aligned number (counts, amounts, milliseconds - not versions);
 * `wide` takes the spare width at desktop sizes (long text such as KR or conditions). */
export type Column<T>={label:string;cell:(row:T)=>ReactNode;main?:boolean;num?:boolean;wide?:boolean};

/**
 * The workbench's one responsive table (web layout audit §4.2 batch 2; UI spec §5). From 640 px it is a table inside its own
 * horizontal scroller (the page title and toolbar never move with it), the `main` column stays in view, and the header sticks
 * when the list is long (more than 20 rows, or `stickyHeader`). Below 640 px each row becomes a card. No rows: the state instead
 * of a table with only a header (`empty`, default "暂无数据").
 */
export function DataTable<T>({rows,columns,rowKey,rowClass,label,className,stickyHeader,empty}:{rows:T[];columns:Column<T>[];rowKey:(row:T,index:number)=>string;
  rowClass?:(row:T)=>string|undefined;label?:string;className?:string;stickyHeader?:boolean;empty?:{subject?:string;filtered?:boolean}}){
  // C3: the scroller is a tab stop (to scroll it by keyboard) only while it actually overflows; measured after mount and on resize.
  const scroller=useRef<HTMLDivElement>(null);const [overflows,setOverflows]=useState(false);
  useLayoutEffect(()=>{const el=scroller.current;if(!el)return;const measure=()=>setOverflows(el.scrollWidth>el.clientWidth+1);measure();
    if(typeof ResizeObserver==='undefined')return;const seen=new ResizeObserver(measure);seen.observe(el);return ()=>seen.disconnect();},[rows,columns]);
  if(!rows.length&&empty)return <EmptyState kind={empty.filtered?'filtered':'empty'} subject={empty.subject??label}/>;
  const kind=(c:Column<T>)=>[c.main?'cell-main':'',c.num?'cell-num':'',c.wide?'cell-wide':''].filter(Boolean).join(' ')||undefined;
  const sticky=stickyHeader??rows.length>20;
  return <div className="table-scroll" ref={scroller} role={overflows?'region':undefined} aria-label={overflows&&label?label+'（可横向滚动）':undefined} tabIndex={overflows?0:undefined}>
    <table className={'data-table'+(sticky?' table-sticky':'')+(className?' '+className:'')} aria-label={label}>
    <thead><tr>{columns.map(c=><th key={c.label} scope="col" className={kind(c)}>{c.label}</th>)}</tr></thead>
    <tbody>{rows.map((row,i)=><tr key={rowKey(row,i)} className={rowClass?.(row)}>
      {columns.map(c=><td key={c.label} data-label={c.label} className={kind(c)}>{c.cell(row)}</td>)}</tr>)}</tbody>
  </table></div>;
}
