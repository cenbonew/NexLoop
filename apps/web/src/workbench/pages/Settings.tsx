import type {ReactNode} from 'react';
import {useQuery} from '@tanstack/react-query';
import {STATE_TEXT,kindOf,readAudit,readSettings} from '../api';
import {Failure,Page} from './Page';
import {Empty} from '../ui';
import {when} from './Consumers';
import {DataTable} from '../DataTable';
import {RealDispatchPanel} from '../RealDispatchPanel';

const ROLE_TEXT:Record<string,string>={owner:'负责人',operator:'运营',reviewer:'审核人'};
export function Settings({actions}:{actions?:ReactNode}){
  const query=useQuery({queryKey:['workbench','settings'],queryFn:readSettings});
  return <Page title="设置与治理" query={query} actions={actions}>{data=><>
    <p>工作台应用 {data.application_id} · 配置版本 {data.manifest_version} · 角色清单 v{data.roles_version}</p>
    <h2>成员与角色（只读）</h2>
    <DataTable label="成员与角色" rows={data.members} rowKey={m=>m.principal_id} columns={[{label:'成员',main:true,cell:m=>m.principal_id},{label:'角色',cell:m=>ROLE_TEXT[m.role]??m.role}]}/>
    <p className="note">成员与角色只能经可信配置变更，工作台内不能自助注册或改角色。</p>
    <RealDispatchPanel/>
    <ReadAudit/>
  </>}</Page>;
}

/** ADR-025: every member read of message bodies and Consumer properties is audited; only the owner sees the audit. */
export function ReadAudit(){
  const query=useQuery({queryKey:['workbench','audit'],queryFn:readAudit});
  return <section aria-labelledby="workbench-audit-title"><h2 id="workbench-audit-title">成员读取审计</h2>
    {query.isPending?<p role="status">{STATE_TEXT.loading}</p>:query.isError?<Failure kind={kindOf(query.error)} onRetry={()=>void query.refetch()}/>
      :!query.data.length?<p className="note">{STATE_TEXT.empty}</p>:<DataTable label="成员读取审计" rows={query.data} rowKey={r=>String(r.audit_id)} columns={[
      {label:'时间',main:true,cell:r=>when(r.read_at)},{label:'成员',cell:r=>r.principal_id},{label:'角色',cell:r=>r.role},{label:'对象',wide:true,cell:r=>r.target_resource},{label:'用途',cell:r=>r.read_purpose}]}/>}</section>;
}

export function NotEnabled({title}:{title:string}){
  return <section className="workbench-page"><h1>{title}</h1><Empty kind="not_enabled"/></section>;
}
