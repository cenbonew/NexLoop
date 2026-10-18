/**
 * Shared workbench components (web layout batch 5, walkthrough 2026-10-18 F12–F15): empty states that say why and what next,
 * status badges with words (never colour alone; the technical code in the title), keyboard-complete tabs, money and Beijing time.
 */
import {useRef,type KeyboardEvent,type ReactNode} from 'react';

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
export type Tone='ok'|'fail'|'pending'|'limited'|'neutral';
const MARK:Record<Tone,string>={ok:'✓',fail:'✕',pending:'…',limited:'!',neutral:'–'};
/** Chinese status text with a symbol and a tone; the technical code goes into the title, never shown as the label. */
export function StatusBadge({text,tone,code}:{text:string;tone:Tone;code?:string|null}){
  return <span className={'status-badge tone-'+tone} title={code??undefined} data-code={code??undefined}><span aria-hidden="true" className="status-mark">{MARK[tone]}</span>{text}</span>;
}

/** Intent states (accepted … confirmed) as tones: done, failed, still open. */
export const intentTone=(state:string):Tone=>state==='fulfilled'||state==='confirmed'?'ok':state==='failed'?'fail':state==='unknown'||state==='dispatching'||state==='accepted'?'pending':'neutral';

/** Commitment statuses as tones: kept, broken, open or waiting, cancelled. */
export const commitmentTone=(status:string):Tone=>/fulfill|kept|satisf|confirm|done/.test(status)?'ok':/breach|fail|violat/.test(status)?'fail':/cancel|withdraw|void/.test(status)?'neutral':'pending';

// ---- F14: tabs ----
/** Tabs with aria-selected, roving tabindex, ←/→/Home/End to move and select, Enter/Space to select; focus follows the selection. */
export function Tabs<K extends string>({label,tabs,value,onChange}:{label:string;tabs:{key:K;label:ReactNode}[];value:K;onChange:(key:K)=>void}){
  const refs=useRef<(HTMLButtonElement|null)[]>([]);
  const index=Math.max(0,tabs.findIndex(t=>t.key===value));
  function select(i:number){const t=tabs[(i+tabs.length)%tabs.length];onChange(t.key);refs.current[(i+tabs.length)%tabs.length]?.focus();}
  function key(event:KeyboardEvent<HTMLButtonElement>,i:number){
    const to={ArrowRight:i+1,ArrowDown:i+1,ArrowLeft:i-1,ArrowUp:i-1,Home:0,End:tabs.length-1}[event.key];
    if(to!==undefined){event.preventDefault();select(to);}
    else if(event.key==='Enter'||event.key===' '){event.preventDefault();select(i);}
  }
  return <div role="tablist" aria-label={label} className="tabs">{tabs.map((t,i)=><button key={t.key} ref={e=>{refs.current[i]=e;}} type="button" role="tab"
    aria-selected={i===index} tabIndex={i===index?0:-1} className={i===index?'tab tab-selected':'tab'} onClick={()=>select(i)} onKeyDown={e=>key(e,i)}>{t.label}</button>)}</div>;
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
export function Time({value,now}:{value:string|null;now?:number}){
  if(!value)return <>—</>;
  const d=parse(value);if(Number.isNaN(d.getTime()))return <>{value}</>;
  const abs=d.toLocaleString('zh-CN',{hour12:false,timeZone:'Asia/Shanghai'});
  return <time dateTime={d.toISOString()} title={`北京时间 ${abs}`}>{abs}<span className="time-rel">（{relative(value,now)}）</span></time>;
}
