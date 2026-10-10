import type {ReactNode} from 'react';
import {useQuery} from '@tanstack/react-query';
import {readSettings} from '../api';
import {Page} from './Page';

const ROLE_TEXT:Record<string,string>={owner:'负责人',operator:'运营',reviewer:'审核人'};
export function Settings({actions}:{actions?:ReactNode}){
  const query=useQuery({queryKey:['workbench','settings'],queryFn:readSettings});
  return <Page title="设置与治理" query={query} actions={actions}>{data=><>
    <p>工作台应用 {data.application_id} · 配置版本 {data.manifest_version} · 角色清单 v{data.roles_version}</p>
    <h2>成员与角色（只读）</h2>
    <table><thead><tr><th>成员</th><th>角色</th></tr></thead><tbody>{data.members.map(m=><tr key={m.principal_id}><td>{m.principal_id}</td><td>{ROLE_TEXT[m.role]??m.role}</td></tr>)}</tbody></table>
    <p className="note">成员与角色只能经可信配置变更，工作台内不能自助注册或改角色。</p>
  </>}</Page>;
}

export function NotEnabled({title}:{title:string}){
  return <section className="workbench-page"><h1>{title}</h1><p className="note" role="status">未启用：属于下一阶段，当前不提供任何发布或操作。</p></section>;
}
