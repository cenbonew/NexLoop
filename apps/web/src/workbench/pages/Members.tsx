/**
 * NX-058 (docs/design/account-self-service.md v7 §1, §1b, §2, §7; nx058-owner-actions.md; nx058-member-erasure.md): the owner's members
 * page. Facts first - members (label, username, role, statuses, vote cooldown), unexpired invitations, the last 90 days of reset codes - then
 * the controls each row allows. Operators and reviewers are changed directly; an owner target, an owner invitation, an owner reset code and
 * the erasure of a departed member are requests under NX-056 approval: another owner approves them on the approvals page, then the
 * requester executes them there. A control is offered only when the member's role carries its Action and the row's role and status allow
 * it; the server rechecks both at execution. A reset code or an invitation token is shown once, in the form that issued it.
 */
import {useState} from 'react';
import {useQuery} from '@tanstack/react-query';
import {readMembers,type Me,type MemberRow,type Members as MembersData} from '../api';
import {Form,WithMember} from '../actions';
import {DataTable} from '../DataTable';
import {Page} from './Page';
import {StatusBadge,Time} from '../ui';
import {IdText} from '../kit';

export const MEMBER_STATUS_TEXT:Record<MemberRow['member_status'],string>={active:'在用',suspended:'已停用',invited:'待接受',revoked:'已撤销'};
const ROLE_TEXT:Record<string,string>={owner:'owner',operator:'运营',reviewer:'审核'};
const RESET_STATE_TEXT:Record<string,string>={issued:'未使用',completed:'已完成',revoked:'已作废',voided:'失败过多作废',expired:'已过期'};
const roleText=(role:string|null)=>role?ROLE_TEXT[role]??role:'无角色';

/** What the row allows (the server rechecks role and status at execution): which kinds of control to show. */
export function memberControls(row:MemberRow,self:string):string[]{
  if(row.principal_id===self||row.erased)return [];
  const out:string[]=[];
  // The erasure acts on the person: every membership of it must be revoked (slice E, C1), not only this row.
  if(row.member_status==='revoked')return row.person_all_revoked?['erase']:[];
  const account=row.account_status==='active';  // a reset code needs an active account (0333, 0348)
  if(row.role==='owner'){
    if(row.member_status==='active')out.push('demote','suspend_owner','revoke_owner',...(account?['issue_owner_reset']:[]));
    if(row.member_status==='suspended')out.push('demote','resume_owner','revoke_owner');
  }else if(row.role==='operator'||row.role==='reviewer'){
    if(row.member_status==='active')out.push('change_role','suspend','revoke',...(account?['issue_reset']:[]),'promote');
    if(row.member_status==='suspended')out.push('resume','revoke');
  }
  return out;
}

const approvalNote=<p className="note">需要另一位 owner 批准：提交后在“审批”页等待批准，批准后由你在那里执行。</p>;
const OPS_CHANNEL='请走运维通道（可信配置的成员文件，或运维重置工具 0198），并在审计中留下记录。';
/** NX-056 (design §0 I1/I2): who could approve a request now - an active owner whose vote cooldown is over, neither the requester nor the
 * member the request acts on. With nobody, the server refuses at once (quorum_unreachable); the page disables it and says why first. */
export function approversFor(members:MemberRow[],self:string,target:string|null,now=Date.now()):number{
  // By person, as NX-056 counts votes by subject: another principal of the requester or of the target is neither; an active human only.
  const person=(p:string|null)=>members.find(m=>m.principal_id===p)?.person;
  const excluded=new Set([person(self),person(target)].filter(Boolean));
  return new Set(members.filter(m=>m.role==='owner'&&m.member_status==='active'&&m.human_active&&!m.erased&&!excluded.has(m.person)
    &&m.vote_eligible_from!==null&&Date.parse(m.vote_eligible_from)<=now).map(m=>m.person)).size;
}
function NoApprover({target}:{target:boolean}){
  return <p className="impact-summary" role="note">现在没有能批准的 owner（除你{target?'和被处置的 owner ':''}之外，需要一位在用、且成为 owner 已满 24 小时的 owner）。{OPS_CHANNEL}</p>;
}

function Reasoned({member,operation,title,row,extra,note,children,ready=true,notReady,tone}:{member:Me;operation:Parameters<typeof Form>[0]['operation'];title:string;row:MemberRow;
  extra?:Record<string,unknown>;note?:React.ReactNode;children?:React.ReactNode;ready?:boolean;notReady?:string;tone?:'danger'}){
  const [reason,setReason]=useState('');const id=`${operation}-${row.label}`;
  return <Form operation={operation} member={member} title={title} tone={tone} disabled={!reason.trim()||!ready}
    disabledReason={!ready?notReady??'现在不能提交，原因见上方说明。':'填写原因后可提交。'}
    body={()=>reason.trim()&&ready?{target_principal:row.principal_id,reason:reason.trim(),...extra}:null}>
    <p>对象：{row.username??'（无账户）'} {row.label}</p>{children}{note}
    <div className="field"><label htmlFor={id}>原因</label><textarea id={id} value={reason} onChange={e=>setReason(e.target.value)} required maxLength={500} rows={2}/></div>
  </Form>;
}

function RoleChoice({value,onChange,name}:{value:string;onChange:(v:string)=>void;name:string}){
  return <fieldset className="choices"><legend>新角色</legend>
    {['operator','reviewer'].map(r=><label key={r} className="choice"><input type="radio" name={name} checked={value===r} onChange={()=>onChange(r)}/>{ROLE_TEXT[r]}</label>)}</fieldset>;
}

function ChangeRole({member,row}:{member:Me;row:MemberRow}){
  const [role,setRole]=useState('');
  return <Form operation="change_workbench_member_role" member={member} title="改角色" disabled={!role||role===row.role} disabledReason="先选择一个不同于当前的新角色。"
    body={()=>role&&role!==row.role?{target_principal:row.principal_id,new_role:role}:null}>
    <p>对象：{row.username??'（无账户）'} {row.label}（当前 {roleText(row.role)}）</p>
    <RoleChoice value={role} onChange={setRole} name={'role-'+row.label}/>
    <p className="impact-summary" role="note">影响：撤销原角色的授权，授予新角色的授权；这位成员的会话立即失效。</p>
  </Form>;
}

function Demote({member,row,note,blocked}:{member:Me;row:MemberRow;note:React.ReactNode;blocked:boolean}){
  const [role,setRole]=useState('');  // required: the server refuses a demotion without a new role
  return <Reasoned member={member} operation="demote_workbench_owner" title="降级这位 owner" row={row} extra={{new_role:role}} note={note} ready={!!role&&!blocked} tone="danger"
    notReady={blocked?'现在没有能批准的负责人，原因见上方说明。':'先选择新角色。'}>
    <RoleChoice value={role} onChange={setRole} name={'demote-'+row.label}/>
  </Reasoned>;
}

function IssueReset({member,row,owner,blocked}:{member:Me;row:MemberRow;owner:boolean;blocked?:boolean}){
  const operation=owner?'issue_workbench_owner_reset':'issue_workbench_reset';
  return <Form operation={operation} member={member} title={owner?'为这位 owner 签发重置码':'签发重置码'} disabled={blocked} body={()=>blocked?null:({target_principal:row.principal_id})}>
    <p>对象：{row.username??'（无账户）'} {row.label}</p>
    <p className="impact-summary" role="note">影响：生成一次性重置码（30 分钟内有效），这位成员的现有会话立即失效，旧的未用重置码作废。{owner?'':'按签发人限速。'}</p>
    {owner?(blocked?<NoApprover target/>:<p className="note">需要另一位 owner 批准；发起请求不会让对方下线，也不生成重置码，批准后执行时才生成。</p>):null}
  </Form>;
}

function Controls({member,row,members}:{member:Me;row:MemberRow;members:MemberRow[]}){
  const kinds=memberControls(row,member.principal_id);
  if(!kinds.length)return <span className="note">—</span>;
  // An owner target never approves its own request (AA2): it is left out of the approvers.
  const blocked=approversFor(members,member.principal_id,row.principal_id)===0;
  const gated=blocked?<NoApprover target={row.role==='owner'}/>:approvalNote;
  return <details><summary>操作</summary>
    {kinds.includes('change_role')?<ChangeRole member={member} row={row}/>:null}
    {kinds.includes('suspend')?<Reasoned member={member} operation="suspend_workbench_member" tone="danger" title="停用" row={row}
      note={<p className="impact-summary" role="note">影响：撤销授权，会话立即失效；恢复后按角色当前的模板重新授权。</p>}/>:null}
    {kinds.includes('resume')?<Reasoned member={member} operation="resume_workbench_member" title="恢复" row={row}/>:null}
    {kinds.includes('revoke')?<Reasoned member={member} operation="revoke_workbench_member" tone="danger" title="撤销成员" row={row}
      note={<p className="impact-summary" role="note">影响：撤销授权、移出配置、会话失效；账号保留，个人数据需另行发起擦除。</p>}/>:null}
    {kinds.includes('issue_reset')?<IssueReset member={member} row={row} owner={false}/>:null}
    {kinds.includes('promote')?<Reasoned member={member} operation="promote_workbench_member" title="提升为 owner" row={row} note={gated} ready={!blocked}/>:null}
    {kinds.includes('demote')?<Demote member={member} row={row} note={gated} blocked={blocked}/>:null}
    {kinds.includes('suspend_owner')?<Reasoned member={member} operation="suspend_workbench_owner" tone="danger" title="停用这位 owner" row={row} note={gated} ready={!blocked}/>:null}
    {kinds.includes('resume_owner')?<Reasoned member={member} operation="resume_workbench_owner" title="恢复这位 owner" row={row} note={gated} ready={!blocked}/>:null}
    {kinds.includes('revoke_owner')?<Reasoned member={member} operation="revoke_workbench_owner" tone="danger" title="撤销这位 owner" row={row} note={gated} ready={!blocked}/>:null}
    {kinds.includes('issue_owner_reset')?<IssueReset member={member} row={row} owner blocked={blocked}/>:null}
    {kinds.includes('erase')?<EraseForm member={member} row={row} blocked={blocked}/>:null}
  </details>;
}

const ERASE_REASONS={offboarding:'离职',member_request:'本人要求',other:'其他'} as const;
function EraseForm({member,row,blocked}:{member:Me;row:MemberRow;blocked:boolean}){
  const [reason,setReason]=useState<keyof typeof ERASE_REASONS|''>('');
  return <Form operation="erase_workbench_member" member={member} title="擦除这位成员的个人数据" tone="danger" disabled={!reason||blocked}
    disabledReason={blocked?'现在没有能批准的负责人，原因见下方说明。':'先选择原因。'}
    body={()=>reason&&!blocked?{target_principal:row.principal_id,reason}:null}>
    <p>对象：{row.username??'（无账户）'} {row.label}</p>
    <fieldset className="choices"><legend>原因</legend>{Object.entries(ERASE_REASONS).map(([k,v])=>
      <label key={k} className="choice"><input type="radio" name={'erase-'+row.label} checked={reason===k} onChange={()=>setReason(k as keyof typeof ERASE_REASONS)}/>{v}</label>)}</fieldset>
    <p className="impact-summary" role="note">影响：账号停用、口令清空、用户名与邮箱改为假名、会话与邀请和重置记录删除；成员身份标识与审计保留。不可撤销。同一个人的全部成员身份都已撤销才能执行。</p>
    {blocked?<NoApprover target={false}/>:approvalNote}
  </Form>;
}

function InviteForm({member,members}:{member:Me;members:MemberRow[]}){
  const ownerBlocked=approversFor(members,member.principal_id,null)===0;
  const [username,setUsername]=useState('');const [role,setRole]=useState('');const [hours,setHours]=useState(24);
  const valid=/^(?!erased-)[a-z0-9][a-z0-9._-]{2,63}$/.test(username)&&hours>=1&&hours<=72;
  return <>
    <Form operation="invite_workbench_member" member={member} title="邀请成员" disabled={!valid||!role}
      disabledReason={!valid?'用户名需 3–64 位小写字母、数字或 . _ -，有效期 1–72 小时。':'先选择角色。'}
      body={()=>valid&&role?{username,role,expires_in_hours:hours}:null}>
      <div className="field"><label htmlFor="invite-username">用户名</label><input id="invite-username" value={username} onChange={e=>setUsername(e.target.value.trim().toLowerCase())}
        required maxLength={64} pattern="[a-z0-9][a-z0-9._-]{2,63}"/></div>
      <RoleChoice value={role} onChange={setRole} name="invite-role"/>
      <div className="field"><label htmlFor="invite-hours">有效期（小时）</label><input id="invite-hours" type="number" min={1} max={72} value={hours} onChange={e=>setHours(Number(e.target.value))}/></div>
      <p className="impact-summary" role="note">影响：生成一次性邀请令牌；同一用户名的旧邀请作废。用户名以 erased- 开头的保留给系统，不能使用。</p>
    </Form>
    <Form operation="invite_workbench_owner" member={member} title="邀请 owner" disabled={!valid||ownerBlocked} body={()=>valid&&!ownerBlocked?{username,expires_in_hours:hours}:null}>
      <p>用户名与有效期同上。</p>{ownerBlocked?<NoApprover target={false}/>:approvalNote}
    </Form>
  </>;
}

export function MembersView({data,member}:{data:MembersData;member:Me}){
  return <>
    <h2>成员</h2>
    <DataTable label="成员" empty={{subject:'成员'}} rows={data.members} rowKey={m=>m.principal_id} columns={[
      {label:'成员',main:true,cell:m=><>{m.username??'（无账户）'}<br/><span className="note">{m.label}</span></>},
      {label:'角色',cell:m=>m.role?<StatusBadge domain="member_role" code={m.role}/>:<span className="note">无角色</span>},
      {label:'状态',cell:m=><>{m.erased?'已擦除':MEMBER_STATUS_TEXT[m.member_status]}{m.account_status&&m.account_status!=='active'?<><br/><span className="note">账户 {m.account_status}</span></>:null}</>},
      {label:'owner 投票资格',cell:m=>m.role!=='owner'?'—':m.vote_eligible_from?<>自 <Time value={m.vote_eligible_from}/></>:<span className="note">尚无</span>},
      {label:'操作',cell:m=><Controls member={member} row={m} members={data.members}/>}]}/>
    <h2>未接受的邀请</h2>
    <DataTable label="未接受的邀请" empty={{subject:'未接受的邀请'}} rows={data.invitations} rowKey={i=>i.invitation_id} columns={[
      {label:'用户名',main:true,cell:i=>i.username},{label:'角色',cell:i=>roleText(i.role)},{label:'签发人',cell:i=><IdText value={i.issued_by} head={8} tail={4} copy={false}/>},
      {label:'到期',cell:i=><Time value={i.expires_at}/>},
      {label:'撤销',cell:i=><Form operation="revoke_workbench_invitation" member={member} className="inline-action" tone="danger" title="撤销邀请" body={()=>({invitation_id:i.invitation_id})}><span/></Form>}]}/>
    <h2>最近 90 天的重置码</h2>
    <DataTable label="最近 90 天的重置码" empty={{subject:'重置记录'}} rows={data.resets} rowKey={r=>r.reset_id} columns={[
      {label:'签发时间',main:true,cell:r=><Time value={r.issued_at}/>},{label:'对象',cell:r=><IdText value={r.target_principal} head={8} tail={4} copy={false}/>},
      {label:'签发人',cell:r=><IdText value={r.issued_by} head={8} tail={4} copy={false}/>},{label:'类别',cell:r=>r.kind==='owner'?'owner（经审批）':'成员'},
      {label:'状态',cell:r=>RESET_STATE_TEXT[r.state]??r.state}]}/>
    <h2>邀请</h2>
    <InviteForm member={member} members={data.members}/>
  </>;
}

export function Members(){
  const query=useQuery({queryKey:['workbench','members'],queryFn:readMembers});
  return <Page title="成员与账号" query={query} lead="工作台成员、未接受的邀请和最近的重置码。成员管理只对 owner 开放；处置 owner、邀请 owner、为 owner 签发重置码和擦除离职成员需要另一位 owner 批准。">
    {data=><WithMember>{member=><MembersView data={data} member={member}/>}</WithMember>}</Page>;
}
