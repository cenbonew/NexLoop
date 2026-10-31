/**
 * Entries under 管理 (owner S73, 2026-10-31): one read-only page of links to every page a member can reach - the workbench pages the
 * sidebar offers this member, and the other pages the gateway serves on the same origin (the synthetic-customer WebChat login, the
 * readiness check). Addresses come from the page's own origin, never a host name or port written here; pages outside this origin
 * open in a new window and say so. No link changes state; a page this member's role does not get is left out, not refused.
 * The reference endpoint's staff page (deploy/stage/reference-endpoint §6) is another host whose address only the deployment knows
 * (GET /api/v1/workbench/entries, compose REFERENCE_ENDPOINT_STAFF_URL): linked in a new window when given, left out when not.
 */
import {useQuery} from '@tanstack/react-query';
import {readEntryLinks,readMe,type Me} from '../api';
import {PAGES,WORLD_SECTIONS,routePath,type PageKey,type Route} from '../route';
import {PageHeader} from '../kit';

export type Entry={label:string;href:string;note:string;route?:Route;newWindow?:boolean};
/** Same-origin pages outside the workbench, from apps/web (main.tsx: everything not under /workbench) and the gateway (Caddyfile). */
export function originEntries(origin:string):Entry[]{
  return [{label:'WebChat（合成客户登录）',href:origin+'/',note:'客户侧登录与会话页，用合成账号演示',newWindow:true},
    {label:'网关就绪检查',href:origin+'/health/ready',note:'只读 JSON：API 与依赖是否就绪',newWindow:true}];
}
/** Pages on other hosts, from the deployment's runtime configuration; a link the deployment does not give is left out. */
export function externalEntries(staff:string|null|undefined):Entry[]{
  return staff?[{label:'参考端点 · 员工页面',href:staff,note:'企业交付端点的替身，内部员工在这里处理交付请求',newWindow:true}]:[];
}
/** Workbench pages in sidebar order, the ones `offered` lets this member see; 企业世界模型 brings its section pages along. */
export function workbenchEntries(groups:{title:string;pages:PageKey[]}[],offered:(page:PageKey)=>boolean):{title:string;entries:Entry[]}[]{
  const entry=(route:Route,label:string,note=''):Entry=>({label,href:routePath(route),note,route});
  return groups.map(g=>({title:g.title,entries:g.pages.filter(p=>p!=='entries'&&offered(p)).flatMap(page=>[entry({page},PAGES[page]),
    ...(page==='ontology'?WORLD_SECTIONS.map(s=>entry({page,section:s.key},`${PAGES.ontology} · ${s.label}`)):[])])})).filter(g=>g.entries.length);
}

function EntryList({entries,go}:{entries:Entry[];go:(route:Route)=>void}){
  return <ul className="entry-list">{entries.map(e=><li key={e.href}>
    {e.newWindow?<a href={e.href} target="_blank" rel="noopener noreferrer">{e.label}<span className="entry-new">（新窗口打开）</span></a>
      :<a href={e.href} onClick={event=>{if(!e.route||event.metaKey||event.ctrlKey||event.shiftKey)return;event.preventDefault();go(e.route);}}>{e.label}</a>}
    {e.note?<span className="note">{e.note}</span>:null}</li>)}</ul>;
}

export function Entries({groups,offered,go,origin=window.location.origin}:{groups:{title:string;pages:PageKey[]}[];offered:(page:PageKey,member:Me|undefined)=>boolean;
  go:(route:Route)=>void;origin?:string}){
  const me=useQuery({queryKey:['workbench','me'],queryFn:readMe,refetchOnWindowFocus:false});
  const links=useQuery({queryKey:['workbench','entries'],queryFn:readEntryLinks,retry:false,refetchOnWindowFocus:false});
  const work=workbenchEntries(groups,page=>offered(page,me.data));
  const external=externalEntries(links.data?.reference_endpoint_staff_url);
  return <section className="workbench-page" aria-labelledby="workbench-page-title">
    <PageHeader title={PAGES.entries} lead="集中列出可以打开的页面，只读跳转，不会改动任何状态。外部页面在新窗口打开。"/>
    <div className="entries">
      {work.map(g=><section key={g.title} className="entry-group" aria-labelledby={'entry-'+g.title}><h2 id={'entry-'+g.title}>工作台 · {g.title}</h2>
        <EntryList entries={g.entries} go={go}/></section>)}
      <section className="entry-group" aria-labelledby="entry-origin"><h2 id="entry-origin">同一网关的其他页面</h2><EntryList entries={originEntries(origin)} go={go}/></section>
      {external.length?<section className="entry-group" aria-labelledby="entry-external"><h2 id="entry-external">外部系统</h2>
        <EntryList entries={external} go={go}/></section>:null}
    </div>
  </section>;
}
