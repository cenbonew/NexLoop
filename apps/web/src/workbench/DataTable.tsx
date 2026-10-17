import type {ReactNode} from 'react';

/** One column: the header text, the cell, and how it lays out. `main` is the row's title in the narrow card view; `num` is a
 * right-aligned narrow count; `wide` takes the spare width at desktop sizes (long text such as KR or conditions). */
export type Column<T>={label:string;cell:(row:T)=>ReactNode;main?:boolean;num?:boolean;wide?:boolean};

/**
 * The workbench's one responsive table (web layout audit §4.2 batch 2). At 640 px and wider it is an ordinary table; below
 * 640 px each row becomes a card: the `main` column is the card title and every other cell is a "label: value" line, so no
 * column is cut off or hidden in a horizontal scroller. Same markup at every width (CSS only), so screen readers keep the
 * table semantics.
 */
export function DataTable<T>({rows,columns,rowKey,rowClass,label,className}:{rows:T[];columns:Column<T>[];rowKey:(row:T,index:number)=>string;
  rowClass?:(row:T)=>string|undefined;label?:string;className?:string}){
  const kind=(c:Column<T>)=>[c.main?'cell-main':'',c.num?'cell-num':'',c.wide?'cell-wide':''].filter(Boolean).join(' ')||undefined;
  return <table className={'data-table'+(className?' '+className:'')} aria-label={label}>
    <thead><tr>{columns.map(c=><th key={c.label} scope="col" className={kind(c)}>{c.label}</th>)}</tr></thead>
    <tbody>{rows.map((row,i)=><tr key={rowKey(row,i)} className={rowClass?.(row)}>
      {columns.map(c=><td key={c.label} data-label={c.label} className={kind(c)}>{c.cell(row)}</td>)}</tr>)}</tbody>
  </table>;
}
