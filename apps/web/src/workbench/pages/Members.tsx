/**
 * NX-058 (docs/design/account-self-service.md v7 §1, §1b, §2, §7; nx058-owner-actions.md; nx058-member-erasure.md): the owner's members
 * page. Facts first - members (label, username, role, statuses, vote cooldown), unexpired invitations, the last 90 days of reset codes - then
 * the controls each row allows. Operators and reviewers are changed directly; an owner target, an owner invitation, an owner reset code and
 * the erasure of a departed member are requests under NX-056 approval: another owner approves them on the approvals page, then the
 * requester executes them there. A control is offered only when the member's role carries its Action and the row's role and status allow
 * it; the server rechecks both at execution. A reset code or an invitation token is shown once, in the form that issued it.
 */
import {useEffect,useRef,useState} from 'react';
import {useQuery} from '@tanstack/react-query';
import {offers,readMembers,type Me,type MemberRow,type Members as MembersData,type Operation} from '../api';
import {Form,WithMember} from '../actions';
import {DataTable} from '../DataTable';
import {Page} from './Page';
import {StatusBadge,Time} from '../ui';
import {IdText} from '../kit';

export const MEMBER_STATUS_TEXT:Record<MemberRow['member_status'],string>={active:'在用',suspended:'已停用',invited:'待接受',revoked:'已撤销'};
export const ACCOUNT_STATUS_TEXT:Record<string,string>={active:'在用',disabled:'已停用',locked:'已锁定',erased:'已擦除'};
const ROLE_TEXT:Record<string,string>={owner:'负责人',operator:'运营',reviewer:'审核人'};
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

const approvalNote=<p className="note">需要另一位负责人批准：提交后在“审批”页等待批准，批准后由你在那里执行。</p>;
const OPS_CHANNEL='请走运维通道（可信配置的成员文件，或运维重置工具），并在审计中留下记录。';
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
  return <p className="impact-summary" role="note">现在没有能批准的负责人（除你{target?'和被处置的负责人':''}之外，需要一位在用、且成为负责人已满 24 小时的负责人）。{OPS_CHANNEL}</p>;
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

function RoleChoice({value,onChange,name,roles=['operator','reviewer']}:{value:string;onChange:(v:string)=>void;name:string;roles?:string[]}){
  return <fieldset className="choices"><legend>{roles.includes('owner')?'角色':'新角色'}</legend>
    {roles.map(r=><label key={r} className="choice"><input type="radio" name={name} checked={value===r} onChange={()=>onChange(r)}/>{ROLE_TEXT[r]}{r==='owner'?'（需另一位负责人批准）':''}</label>)}</fieldset>;
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
  return <Reasoned member={member} operation="demote_workbench_owner" title="降级这位负责人" row={row} extra={{new_role:role}} note={note} ready={!!role&&!blocked} tone="danger"
    notReady={blocked?'现在没有能批准的负责人，原因见上方说明。':'先选择新角色。'}>
    <RoleChoice value={role} onChange={setRole} name={'demote-'+row.label}/>
  </Reasoned>;
}

function IssueReset({member,row,owner,blocked}:{member:Me;row:MemberRow;owner:boolean;blocked?:boolean}){
  const operation=owner?'issue_workbench_owner_reset':'issue_workbench_reset';
  return <Form operation={operation} member={member} title={owner?'为这位负责人签发重置码':'签发重置码'} disabled={blocked} body={()=>blocked?null:({target_principal:row.principal_id})}>
    <p>对象：{row.username??'（无账户）'} {row.label}</p>
    <p className="impact-summary" role="note">影响：生成一次性重置码（30 分钟内有效），这位成员的现有会话立即失效，旧的未用重置码作废。{owner?'':'按签发人限速。'}</p>
    {owner?(blocked?<NoApprover target/>:<p className="note">需要另一位负责人批准；发起请求不会让对方下线，也不生成重置码，批准后执行时才生成。</p>):null}
  </Form>;
}

/** The controls a row offers, in words, each with its governed operation (the role must carry it to be offered). */
export const CONTROL_TEXT:Record<string,{title:string;operation:Operation;danger?:boolean}>={
  change_role:{title:'改角色',operation:'change_workbench_member_role'},suspend:{title:'停用',operation:'suspend_workbench_member',danger:true},
  resume:{title:'恢复',operation:'resume_workbench_member'},revoke:{title:'撤销成员',operation:'revoke_workbench_member',danger:true},
  issue_reset:{title:'签发重置码',operation:'issue_workbench_reset'},promote:{title:'提升为负责人',operation:'promote_workbench_member'},
  demote:{title:'降级这位负责人',operation:'demote_workbench_owner',danger:true},suspend_owner:{title:'停用这位负责人',operation:'suspend_workbench_owner',danger:true},
  resume_owner:{title:'恢复这位负责人',operation:'resume_workbench_owner'},revoke_owner:{title:'撤销这位负责人',operation:'revoke_workbench_owner',danger:true},
  issue_owner_reset:{title:'为这位负责人签发重置码',operation:'issue_workbench_owner_reset'},erase:{title:'擦除这位成员的个人数据',operation:'erase_workbench_member',danger:true}};
/** The kinds a member may use on a row: what the row allows (memberControls) and the member's role carries. */
export const offeredControls=(member:Me,row:MemberRow)=>memberControls(row,member.principal_id).filter(k=>offers(member,CONTROL_TEXT[k].operation));

/** UX audit B9: a row offers its operations in one picker; the chosen one opens as a single form under the table (never several
 * forms stacked in a table cell). */
function Controls({member,row,selected,onSelect}:{member:Me;row:MemberRow;selected:string;onSelect:(kind:string)=>void}){
  const kinds=offeredControls(member,row);
  if(!kinds.length)return <span className="note">—</span>;
  return <select className="member-control" aria-label={`对 ${row.username??row.label} 的操作`} value={selected} onChange={e=>onSelect(e.target.value)}>
    <option value="">选择操作…</option>{kinds.map(k=><option key={k} value={k}>{CONTROL_TEXT[k].title}{CONTROL_TEXT[k].danger?'（需谨慎）':''}</option>)}</select>;
}

/** One operation's form for one row (the forms themselves are unchanged: reason, impact, approval note, danger tone). */
export function MemberControlPanel({member,row,members,kind}:{member:Me;row:MemberRow;members:MemberRow[];kind:string}){
  // An owner target never approves its own request (AA2): it is left out of the approvers.
  const blocked=approversFor(members,member.principal_id,row.principal_id)===0;
  const gated=blocked?<NoApprover target={row.role==='owner'}/>:approvalNote;
  switch(kind){
    case 'change_role':return <ChangeRole member={member} row={row}/>;
    case 'suspend':return <Reasoned member={member} operation="suspend_workbench_member" tone="danger" title="停用" row={row}
      note={<p className="impact-summary" role="note">影响：撤销授权，会话立即失效；恢复后按角色当前的模板重新授权。</p>}/>;
    case 'resume':return <Reasoned member={member} operation="resume_workbench_member" title="恢复" row={row}/>;
    case 'revoke':return <Reasoned member={member} operation="revoke_workbench_member" tone="danger" title="撤销成员" row={row}
      note={<p className="impact-summary" role="note">影响：撤销授权、移出配置、会话失效；账号保留，个人数据需另行发起擦除。</p>}/>;
    case 'issue_reset':return <IssueReset member={member} row={row} owner={false}/>;
    case 'promote':return <Reasoned member={member} operation="promote_workbench_member" title="提升为负责人" row={row} note={gated} ready={!blocked}/>;
    case 'demote':return <Demote member={member} row={row} note={gated} blocked={blocked}/>;
    case 'suspend_owner':return <Reasoned member={member} operation="suspend_workbench_owner" tone="danger" title="停用这位负责人" row={row} note={gated} ready={!blocked}/>;
    case 'resume_owner':return <Reasoned member={member} operation="resume_workbench_owner" title="恢复这位负责人" row={row} note={gated} ready={!blocked}/>;
    case 'revoke_owner':return <Reasoned member={member} operation="revoke_workbench_owner" tone="danger" title="撤销这位负责人" row={row} note={gated} ready={!blocked}/>;
    case 'issue_owner_reset':return <IssueReset member={member} row={row} owner blocked={blocked}/>;
    case 'erase':return <EraseForm member={member} row={row} blocked={blocked}/>;
    default:return null;
  }
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
    <p className="impact-summary" role="note">影响：账号停用、密码清空、用户名与邮箱改为假名、会话与邀请和重置记录删除；成员身份标识与审计保留。不可撤销。同一个人的全部成员身份都已撤销才能执行。</p>
    {blocked?<NoApprover target={false}/>:approvalNote}
  </Form>;
}

/** UX audit B9: one invitation form; choosing 负责人 makes it the approval-gated owner invitation (another owner approves it). */
function InviteForm({member,members}:{member:Me;members:MemberRow[]}){
  const ownerBlocked=approversFor(members,member.principal_id,null)===0;
  const [username,setUsername]=useState('');const [role,setRole]=useState('');const [hours,setHours]=useState(24);
  const valid=/^(?!erased-)[a-z0-9][a-z0-9._-]{2,63}$/.test(username)&&hours>=1&&hours<=72;
  const owner=role==='owner';
  const roles=offers(member,'invite_workbench_owner')?['operator','reviewer','owner']:['operator','reviewer'];
  const fields=<>
    <div className="field"><label htmlFor="invite-username">用户名</label><input id="invite-username" value={username} onChange={e=>setUsername(e.target.value.trim().toLowerCase())}
      required maxLength={64} pattern="[a-z0-9][a-z0-9._-]{2,63}"/></div>
    <RoleChoice value={role} onChange={setRole} name="invite-role" roles={roles}/>
    <div className="field"><label htmlFor="invite-hours">有效期（小时）</label><input id="invite-hours" type="number" min={1} max={72} value={hours} onChange={e=>setHours(Number(e.target.value))}/></div>
    <p className="impact-summary" role="note">影响：生成一次性邀请令牌；同一用户名的旧邀请作废。用户名以 erased- 开头的保留给系统，不能使用。</p></>;
  const why=!valid?'用户名需 3–64 位小写字母、数字或 . _ -，有效期 1–72 小时。':!role?'先选择角色。':null;
  return owner?<Form key="owner" operation="invite_workbench_owner" member={member} title="邀请负责人" disabled={!valid||ownerBlocked}
      disabledReason={why??'现在没有能批准的负责人，原因见下方说明。'} body={()=>valid&&!ownerBlocked?{username,expires_in_hours:hours}:null}>
      {fields}{ownerBlocked?<NoApprover target={false}/>:approvalNote}</Form>
    :<Form key="member" operation="invite_workbench_member" member={member} title="邀请成员" disabled={!valid||!role} disabledReason={why??undefined}
      body={()=>valid&&role?{username,role,expires_in_hours:hours}:null}>{fields}</Form>;
}

export function MembersView({data,member}:{data:MembersData;member:Me}){
  const [chosen,setChosen]=useState<{principal:string;kind:string}|null>(null);
  const panel=useRef<HTMLHeadingElement>(null);
  useEffect(()=>{if(chosen)panel.current?.focus();},[chosen]);
  const target=chosen?data.members.find(m=>m.principal_id===chosen.principal)??null:null;
  const who=(principal:string)=>{const m=data.members.find(x=>x.principal_id===principal);return m?<>{m.username??'（无账户）'} <span className="note">{m.label}</span></>:<IdText value={principal} head={8} tail={4} copy={false}/>;};
  return <>
    <h2>成员</h2>
    <DataTable label="成员" empty={{subject:'成员'}} rows={data.members} rowKey={m=>m.principal_id} columns={[
      {label:'成员',main:true,cell:m=><>{m.username??'（无账户）'}<br/><span className="note">{m.label}</span></>},
      {label:'角色',cell:m=>m.role?<StatusBadge domain="member_role" code={m.role}/>:<span className="note">无角色</span>},
      {label:'状态',cell:m=><>{m.erased?'已擦除':MEMBER_STATUS_TEXT[m.member_status]}{m.account_status&&m.account_status!=='active'?<><br/><span className="note" title={m.account_status}>账户{ACCOUNT_STATUS_TEXT[m.account_status]??'状态未知'}</span></>:null}</>},
      {label:'负责人投票资格',cell:m=>m.role!=='owner'?'—':m.vote_eligible_from?<>自 <Time value={m.vote_eligible_from}/></>:<span className="note">尚无</span>},
      {label:'操作',cell:m=><Controls member={member} row={m} selected={chosen?.principal===m.principal_id?chosen.kind:''}
        onSelect={kind=>setChosen(kind?{principal:m.principal_id,kind}:null)}/>}]}/>
    {chosen&&target?<section className="member-operation" aria-labelledby="member-operation-title">
      <h3 id="member-operation-title" ref={panel} tabIndex={-1}>{CONTROL_TEXT[chosen.kind]?.title}：{target.username??'（无账户）'} {target.label}</h3>
      <MemberControlPanel key={chosen.principal+chosen.kind} member={member} row={target} members={data.members} kind={chosen.kind}/>
      <button type="button" className="btn-secondary" onClick={()=>setChosen(null)}>关闭</button>
    </section>:null}
    <h2>未接受的邀请</h2>
    <DataTable label="未接受的邀请" empty={{subject:'未接受的邀请'}} rows={data.invitations} rowKey={i=>i.invitation_id} columns={[
      {label:'用户名',main:true,cell:i=>i.username},{label:'角色',cell:i=>roleText(i.role)},{label:'签发人',cell:i=>who(i.issued_by)},
      {label:'到期',cell:i=><Time value={i.expires_at}/>},
      {label:'撤销',cell:i=><Form operation="revoke_workbench_invitation" member={member} className="inline-action" tone="danger" title="撤销邀请" body={()=>({invitation_id:i.invitation_id})}><span/></Form>}]}/>
    <h2>最近 90 天的重置码</h2>
    <DataTable label="最近 90 天的重置码" empty={{subject:'重置记录'}} rows={data.resets} rowKey={r=>r.reset_id} columns={[
      {label:'签发时间',main:true,cell:r=><Time value={r.issued_at}/>},{label:'对象',cell:r=>who(r.target_principal)},
      {label:'签发人',cell:r=>who(r.issued_by)},{label:'类别',cell:r=>r.kind==='owner'?'负责人（经审批）':'成员'},
      {label:'状态',cell:r=>RESET_STATE_TEXT[r.state]??r.state}]}/>
    <h2>邀请</h2>
    <InviteForm member={member} members={data.members}/>
  </>;
}

export function Members(){
  const query=useQuery({queryKey:['workbench','members'],queryFn:readMembers});
  return <Page title="成员与账号" query={query} lead="工作台成员、未接受的邀请和最近的重置码。成员管理只对负责人开放；处置负责人、邀请负责人、为负责人签发重置码和擦除离职成员需要另一位负责人批准。">
    {data=><WithMember>{member=><MembersView data={data} member={member}/>}</WithMember>}</Page>;
}
