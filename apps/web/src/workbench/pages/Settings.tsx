import type {ReactNode} from 'react';
import {useQuery} from '@tanstack/react-query';
import {STATE_TEXT,kindOf,readAudit,readSettings} from '../api';
import {Failure,Page} from './Page';

const ROLE_TEXT:Record<string,string>={owner:'负责人',operator:'运营',reviewer:'审核人'};
export function Settings({actions}:{actions?:ReactNode}){
  const query=useQuery({queryKey:['workbench','settings'],queryFn:readSettings});
  return <Page title="设置与治理" query={query} actions={actions}>{data=><>
    <p>工作台应用 {data.application_id} · 配置版本 {data.manifest_version} · 角色清单 v{data.roles_version}</p>
    <h2>成员与角色（只读）</h2>
    <table><thead><tr><th>成员</th><th>角色</th></tr></thead><tbody>{data.members.map(m=><tr key={m.principal_id}><td>{m.principal_id}</td><td>{ROLE_TEXT[m.role]??m.role}</td></tr>)}</tbody></table>
    <p className="note">成员与角色只能经可信配置变更，工作台内不能自助注册或改角色。</p>
    <ReadAudit/>
  </>}</Page>;
}

/** ADR-025: every member read of message bodies and Consumer properties is audited; only the owner sees the audit. */
export function ReadAudit(){
  const query=useQuery({queryKey:['workbench','audit'],queryFn:readAudit});
  return <section aria-labelledby="workbench-audit-title"><h2 id="workbench-audit-title">成员读取审计</h2>
    {query.isPending?<p role="status">{STATE_TEXT.loading}</p>:query.isError?<Failure kind={kindOf(query.error)} onRetry={()=>void query.refetch()}/>
      :!query.data.length?<p className="note">{STATE_TEXT.empty}</p>:<table>
      <thead><tr><th>时间</th><th>成员</th><th>角色</th><th>对象</th><th>用途</th></tr></thead>
      <tbody>{query.data.map(r=><tr key={r.audit_id}><td>{r.read_at}</td><td>{r.principal_id}</td><td>{r.role}</td><td>{r.target_resource}</td><td>{r.read_purpose}</td></tr>)}</tbody>
    </table>}</section>;
}

export function NotEnabled({title}:{title:string}){
  return <section className="workbench-page"><h1>{title}</h1><p className="note" role="status">未启用：属于下一阶段，当前不提供任何发布或操作。</p></section>;
}
