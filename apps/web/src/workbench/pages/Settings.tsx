import type {ReactNode} from 'react';
import {useQuery} from '@tanstack/react-query';
import {kindOf,readAudit,readSettings} from '../api';
import {Page} from './Page';
import {StatusBadge,Time} from '../ui';
import {EmptyState,IdText,KeyValue,PageHeader} from '../kit';
import {DataTable} from '../DataTable';
import {statusLabel} from '../status-labels';
import {RealDispatchPanel} from '../RealDispatchPanel';
export function Settings({actions}:{actions?:ReactNode}){
  const query=useQuery({queryKey:['workbench','settings'],queryFn:readSettings});
  return <Page title="设置与治理" query={query} actions={actions} lead="工作台的成员与角色、真实派发三层开关，以及成员读取消息原文与客户资料的审计。">{data=><>
    <KeyValue label="工作台配置" items={[{key:'工作台应用',value:<IdText value={data.application_id} head={20} tail={4}/>},{key:'配置版本',value:`第 ${data.manifest_version} 版`},
      {key:'角色清单',value:`v${data.roles_version}`}]}/>
    <h2>成员与角色（只读）</h2>
    <DataTable label="成员与角色" empty={{subject:'成员'}} rows={data.members} rowKey={m=>m.principal_id} columns={[{label:'成员',main:true,cell:m=><IdText kind="成员" value={m.principal_id} head={12} tail={4}/>},
      {label:'角色',cell:m=><StatusBadge domain="member_role" code={m.role}/>}]}/>
    <p className="note">成员与角色只能经可信配置变更，工作台内不能自助注册或改角色。</p>
    <RealDispatchPanel/>
    <ReadAudit/>
  </>}</Page>;
}

/** ADR-025: every member read of message bodies and Consumer properties is audited; only the owner sees the audit. */
export function ReadAudit(){
  const query=useQuery({queryKey:['workbench','audit'],queryFn:readAudit});
  return <section aria-labelledby="workbench-audit-title"><h2 id="workbench-audit-title">成员读取审计</h2>
    {query.isPending?<EmptyState kind="loading" subject="成员读取审计"/>:query.isError?<EmptyState kind={kindOf(query.error)==='forbidden'?'forbidden':'unavailable'} subject="成员读取审计"
      onRetry={()=>void query.refetch()}/>
      :<DataTable label="成员读取审计" empty={{subject:'读取记录'}} rows={query.data} rowKey={r=>String(r.audit_id)} columns={[
      {label:'时间',main:true,cell:r=><Time value={r.read_at}/>},{label:'成员',cell:r=><><StatusBadge domain="member_role" code={r.role}/> <IdText value={r.principal_id} head={6} tail={4} copy={false}/></>},
      {label:'对象',wide:true,cell:r=><IdText kind={statusLabel('audit_target',r.object_kind)} value={r.target_resource.split(/[/:]/).pop()||r.target_resource}/>},
      {label:'用途',cell:r=><StatusBadge domain="read_purpose" code={r.read_purpose}/>}]}/>}</section>;
}

export function NotEnabled({title}:{title:string}){
  return <section className="workbench-page"><PageHeader title={title}/><EmptyState kind="not_enabled" subject={title}/></section>;
}
