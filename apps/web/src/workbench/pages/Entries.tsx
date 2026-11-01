/**
 * 关联系统入口 under 管理 (owner S73 2026-10-31; S80 after walkthrough R64: the workbench pages are the sidebar's, not repeated here):
 * read-only links to the other pages the gateway serves on the same origin (the synthetic-customer WebChat login, the readiness
 * check) and to external systems. Addresses come from the page's own origin, never a host name or port written here; every link
 * opens in a new window and says so. No link changes state.
 * The reference endpoint's staff page (deploy/stage/reference-endpoint §6) is another host whose address only the deployment knows
 * (GET /api/v1/workbench/entries, compose REFERENCE_ENDPOINT_STAFF_URL): linked in a new window when given, left out when not.
 */
import {useQuery} from '@tanstack/react-query';
import {readEntryLinks} from '../api';
import {PAGES} from '../route';
import {PageHeader} from '../kit';

export type Entry={label:string;href:string;note:string};
/** Same-origin pages outside the workbench, from apps/web (main.tsx: everything not under /workbench) and the gateway (Caddyfile). */
export function originEntries(origin:string):Entry[]{
  return [{label:'WebChat（合成客户登录）',href:origin+'/',note:'客户侧登录与会话页，用合成账号演示'},
    {label:'网关就绪检查',href:origin+'/health/ready',note:'只读 JSON：API 与依赖是否就绪'}];
}
/** Pages on other hosts, from the deployment's runtime configuration; a link the deployment does not give is left out. */
export function externalEntries(staff:string|null|undefined):Entry[]{
  return staff?[{label:'参考端点 · 员工页面',href:staff,note:'企业交付端点的替身，内部员工在这里处理交付请求'}]:[];
}
function EntryList({entries}:{entries:Entry[]}){
  return <ul className="entry-list">{entries.map(e=><li key={e.href}>
    <a href={e.href} target="_blank" rel="noopener noreferrer">{e.label}<span className="entry-new">（新窗口打开）</span></a>
    {e.note?<span className="note">{e.note}</span>:null}</li>)}</ul>;
}

export function Entries({origin=window.location.origin}:{origin?:string}){
  const links=useQuery({queryKey:['workbench','entries'],queryFn:readEntryLinks,retry:false,refetchOnWindowFocus:false});
  const external=externalEntries(links.data?.reference_endpoint_staff_url);
  return <section className="workbench-page" aria-labelledby="workbench-page-title">
    <PageHeader title={PAGES.entries} lead="列出同一网关的其他页面与外部系统的入口，只读跳转，不会改动任何状态，均在新窗口打开。"/>
    <div className="entries">
      <section className="entry-group" aria-labelledby="entry-origin"><h2 id="entry-origin">同一网关的其他页面</h2><EntryList entries={originEntries(origin)}/></section>
      {external.length?<section className="entry-group" aria-labelledby="entry-external"><h2 id="entry-external">外部系统</h2>
        <EntryList entries={external}/></section>:null}
    </div>
  </section>;
}
