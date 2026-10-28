/**
 * Shared workbench components (web layout batch 5, walkthrough 2026-10-18 F12–F15): empty states that say why and what next,
 * status badges with words (never colour alone; the technical code in the title), keyboard-complete tabs, money and Beijing time.
 */
import {useRef,type KeyboardEvent,type ReactNode} from 'react';
import {statusLabel,statusTone,type Domain,type Tone} from './status-labels';
export type {Tone} from './status-labels';

// ---- F12: empty states ----
export type EmptyKind='none'|'filtered'|'not_enabled'|'forbidden'|'unavailable';
const EMPTY:Record<EmptyKind,{title:string;next:string}>={
  none:{title:'暂无数据',next:'有新记录时会出现在这里。'},
  filtered:{title:'没有符合筛选条件的记录',next:'换一个筛选条件，或选择“全部”。'},
  not_enabled:{title:'未启用',next:'属于下一阶段，当前不提供任何发布或操作。'},
  forbidden:{title:'当前账号没有查看此内容的权限',next:'需要时请负责人调整角色；这不表示没有数据。'},
  unavailable:{title:'暂时无法读取',next:'稍后再打开；这不表示数量为零。'},
};
/** One empty/unavailable state: what it is, why (optional) and the next step. */
export function Empty({kind,title,reason,next,action}:{kind:EmptyKind;title?:string;reason?:ReactNode;next?:ReactNode;action?:ReactNode}){
  const t=EMPTY[kind];
  return <div className={'workbench-empty empty-'+kind} role="status" data-empty={kind}>
    <p className="empty-title">{title??t.title}{reason?<>：{reason}</>:null}</p>
    <p className="empty-next">{next??t.next}</p>{action}</div>;
}

// ---- F13: status badges ----
const MARK:Record<Tone,string>={ok:'✓',fail:'✕',pending:'…',limited:'!',neutral:'–'};
/** UI spec §7: `{domain, code}` takes the Chinese label and the tone from status-labels (an unlisted code reads 未知状态, the code
 * only in the title); `{text, tone}` stays for wording that is not a code. The technical code is never the visible label. */
export type StatusBadgeProps={domain:Domain;code:string|null|undefined;tone?:Tone}|{text:string;tone:Tone;code?:string|null;domain?:undefined};
export function StatusBadge(props:StatusBadgeProps){
  const text=props.domain?statusLabel(props.domain,props.code):props.text;
  const tone=props.tone??(props.domain?statusTone(props.domain,props.code):'neutral');
  const code=props.code??undefined;
  return <span className={'status-badge tone-'+tone} title={code} data-code={code}><span aria-hidden="true" className="status-mark">{MARK[tone]}</span>{text}</span>;
}

/** Intent states (accepted … confirmed) as tones: done, failed, still open. */
export const intentTone=(state:string):Tone=>state==='fulfilled'||state==='confirmed'?'ok':state==='failed'?'fail':state==='unknown'||state==='dispatching'||state==='accepted'?'pending':'neutral';

/** Commitment statuses as tones: kept, broken, open or waiting, cancelled. */
export const commitmentTone=(status:string):Tone=>/fulfill|kept|satisf|confirm|done/.test(status)?'ok':/breach|fail|violat/.test(status)?'fail':/cancel|withdraw|void/.test(status)?'neutral':'pending';

/** A polite live region (always mounted, visually hidden): screen readers hear `text` when it changes, e.g. the number of
 * results after a tab or filter switch (batch 8). */
export function Announce({text}:{text:string}){return <p className="sr-only" role="status" aria-live="polite" aria-atomic="true" data-announce>{text}</p>;}

// ---- F14: tabs ----
/** Tabs with aria-selected, roving tabindex, ←/→/Home/End to move and select, Enter/Space to select; focus follows the selection. */
/** With `id` (UI spec appendix B) each tab gets `${id}-tab-${key}` and aria-controls `${id}-panel`; wrap the content in
 * <TabPanel id value> so the panel is labelled by the selected tab. */
export function Tabs<K extends string>({label,tabs,value,onChange,announce,id}:{label:string;tabs:{key:K;label:ReactNode}[];value:K;onChange:(key:K)=>void;announce?:string;id?:string}){
  const refs=useRef<(HTMLButtonElement|null)[]>([]);
  const index=Math.max(0,tabs.findIndex(t=>t.key===value));
  function select(i:number){const t=tabs[(i+tabs.length)%tabs.length];onChange(t.key);refs.current[(i+tabs.length)%tabs.length]?.focus();}
  function key(event:KeyboardEvent<HTMLButtonElement>,i:number){
    const to={ArrowRight:i+1,ArrowDown:i+1,ArrowLeft:i-1,ArrowUp:i-1,Home:0,End:tabs.length-1}[event.key];
    if(to!==undefined){event.preventDefault();select(to);}
    else if(event.key==='Enter'||event.key===' '){event.preventDefault();select(i);}
  }
  return <><div role="tablist" aria-label={label} className="tabs">{tabs.map((t,i)=><button key={t.key} ref={e=>{refs.current[i]=e;}} type="button" role="tab"
    aria-selected={i===index} tabIndex={i===index?0:-1} id={id?`${id}-tab-${t.key}`:undefined} aria-controls={id?`${id}-panel`:undefined} className={i===index?'tab tab-selected':'tab'} onClick={()=>select(i)} onKeyDown={e=>key(e,i)}>{t.label}</button>)}</div>
    {announce!==undefined?<Announce text={announce}/>:null}</>;
}

/** The panel of a Tabs with the same `id`: role tabpanel, labelled by the selected tab, focusable. */
export function TabPanel({id,value,children}:{id:string;value:string;children:ReactNode}){
  return <div role="tabpanel" id={`${id}-panel`} aria-labelledby={`${id}-tab-${value}`} tabIndex={0} className="tab-panel">{children}</div>;
}

// ---- F15: numbers and time ----
/** Money and budget amounts: two decimals for reading, the full stored precision in the title. */
export function money(value:number|string):string{
  const n=typeof value==='number'?value:Number(value);
  return Number.isFinite(n)?n.toLocaleString('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:2}):String(value);
}
export function Money({value}:{value:number|string}){return <span className="num" title={String(value)}>{money(value)}</span>;}

function parse(value:string):Date{return new Date(value.replace(' ','T').replace(/(\.\d{3})\d+/,'$1').replace(/([+-]\d{2})$/,'$1:00'));}
/** Relative wording for a moment, against `now`. */
export function relative(value:string,now=Date.now()):string{
  const d=parse(value);if(Number.isNaN(d.getTime()))return '';
  const s=Math.round((now-d.getTime())/1000);const a=Math.abs(s);const after=s<0;
  const text=a<60?'刚刚':a<3600?`${Math.floor(a/60)} 分钟`:a<86400?`${Math.floor(a/3600)} 小时`:`${Math.floor(a/86400)} 天`;
  return text==='刚刚'?text:text+(after?'后':'前');
}
/** Absolute Beijing time (the workbench's one time zone, labelled once in the header) and the relative value; ISO in dateTime. */
/** A datetime-local value ("YYYY-MM-DDTHH:MM[:SS]") read as Beijing time (+08:00, no daylight saving), as every time on the workbench
 * is shown, whatever the browser's zone; the UTC instant as ISO text, or null when it is not such a value. */
export function beijingLocalToUtc(local:string):string|null{
  if(!/^\d{4}-\d\d-\d\dT\d\d:\d\d(:\d\d)?$/.test(local))return null;
  const d=new Date(local+(local.length===16?':00':'')+'+08:00');return Number.isNaN(d.getTime())?null:d.toISOString();
}
export function Time({value,now}:{value:string|null;now?:number}){
  if(!value)return <>—</>;
  const d=parse(value);if(Number.isNaN(d.getTime()))return <>{value}</>;
  const abs=d.toLocaleString('zh-CN',{hour12:false,timeZone:'Asia/Shanghai'});
  const rel=relative(value,now);
  return <time dateTime={d.toISOString()} title={`北京时间 ${abs}`}>{abs}{rel?<span className="time-rel">（{rel}）</span>:null}</time>;
}

// ---- Walkthrough F09 (batch 7): list tools ----
/** "共 N 条 · 第 a–b 条" and previous/next for an offset page of `size`; nothing when the server sent no total (older server). */
export function Pager({total,start,size,shown,onPage}:{total:number|null;start:number;size:number;shown:number;onPage:(start:number)=>void}){
  if(total===null)return null;
  return <div className="pager list-pager" role="navigation" aria-label="分页"><span className="list-total" role="status">共 {total} 条{shown?` · 第 ${start+1}–${start+shown} 条`:''}</span>
    {start>0?<button type="button" onClick={()=>onPage(Math.max(0,start-size))}>上一页</button>:null}
    {start+shown<total?<button type="button" onClick={()=>onPage(start+size)}>下一页</button>:null}</div>;
}
/** Keyword over the rows already on this page (D5: names and promised words are never searched server side); says so. */
export function Keyword({value,onChange,label}:{value:string;onChange:(value:string)=>void;label:string}){
  return <label className="list-keyword"><span>{label}</span><input type="search" value={value} maxLength={80} onChange={e=>onChange(e.target.value)} placeholder="仅在当前页内查找"/></label>;
}
/** A removable filter in words (e.g. the customer a list is narrowed to). */
export function FilterChip({label,onClear}:{label:ReactNode;onClear:()=>void}){
  return <span className="filter-chip" data-filter="active">{label}<button type="button" aria-label="清除此筛选" onClick={onClear}>×</button></span>;
}
/** Copy a full identifier (the short form stays visible; the value is in the title). */
export function CopyId({value,label='复制 ID'}:{value:string;label?:string}){
  return <span className="copy-id"><code title={value}>{value.slice(0,12)}…</code><button type="button" className="copy-button" aria-label={label}
    onClick={()=>void navigator.clipboard?.writeText(value).catch(()=>undefined)}>复制</button></span>;
}
