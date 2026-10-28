/**
 * NX-056 A4 (docs/design/unified-approvals.md v5 §8): the unified approvals page, three columns for the signed-in member.
 * 待我审批: another person's waiting request the member may decide now (the server checks the role, the subject and the deadline);
 * 我发起的: the member's own requests of the last 30 days; 待我执行: the member's approved requests, executed by resubmitting the frozen
 * inputs (a new request key; the same body, so the same digest). Others' request bodies never reach this page: a row shows the
 * Action, the requester, the policy's summary fields and the digest's first 12 digits.
 */
import {useState} from 'react';
import {useQuery} from '@tanstack/react-query';
import {OPERATION_ACTION,kindOf,readApprovals,type ApprovalRow,type ApprovalStatus,type Me,type Operation} from '../api';
import {Form,WithMember} from '../actions';
import {DataTable} from '../DataTable';
import {Empty,Time} from '../ui';

export const APPROVAL_STATUS_TEXT:Record<ApprovalStatus,string>={waiting_approval:'等待审批',approved:'已批准',rejected:'已拒绝',cancelled:'已撤回',expired:'已过期'};
/** The result of an executed approval in words (consumption result codes, design §4). */
export function resultText(result:string|null|undefined):string{
  if(!result)return '—';
  if(result==='executed')return '已执行';
  return result.startsWith('execution_refused:')?'执行被拒（'+result.slice('execution_refused:'.length)+'）':result;
}
function summaryText(row:ApprovalRow):string{
  const entries=Object.entries(row.summary);
  return entries.length?entries.map(([k,v])=>`${k}=${v===null?'空':String(v)}`).join('，'):'—';
}
/** The Action in business words (L6 review 7); the capability stays visible as secondary text. */
export const APPROVAL_ACTION_TEXT:Record<string,string>={set_real_dispatch:'打开真实派发',erase_consumer:'擦除客户数据',release_retention_hold:'解除数据保全',
  issue_incentive:'发放激励',
  // NX-058: the owner-only member Actions and the staff erasure.
  promote_workbench_member:'提升为 owner',demote_workbench_owner:'降级 owner',suspend_workbench_owner:'停用 owner',resume_workbench_owner:'恢复 owner',
  revoke_workbench_owner:'撤销 owner',issue_workbench_owner_reset:'为 owner 签发重置码',invite_workbench_owner:'邀请 owner',erase_workbench_member:'擦除离职成员',
  // NX-056 budget approval.
  loosen_budget:'放宽预算'};
export function actionText(row:ApprovalRow):string{return APPROVAL_ACTION_TEXT[row.operation]??'其他操作';}
/** The target member in words (ADR-039 §6): who the request acts on, as the server rendered it for this reader. */
export function targetText(t:ApprovalRow['target_member']):string{
  if(t===null)return '—';
  if(t.kind==='hidden')return '（仅 owner 可见）';
  if(t.kind==='unresolved')return '目标已不在租户中';
  if(t.kind==='erased')return '已擦除';
  if(t.kind==='invitee')return t.same_username_exists?`邀请：${t.username}（已有同名账户，应拒绝）`:`邀请：${t.username}`;
  const status=[t.role??'无角色',t.member_status,t.account_status&&t.account_status!=='active'?'账户 '+t.account_status:null].filter(Boolean).join('，');
  return `${t.username??'（无账户）'} ${t.label}（${status}）`;
}
const isOperation=(value:string):value is Operation=>Object.prototype.hasOwnProperty.call(OPERATION_ACTION,value);

const common=[
  {label:'操作',main:true,cell:(r:ApprovalRow)=><>{actionText(r)}<br/><span className="note">{r.capability} · 策略 v{r.policy_version}</span></>},
  {label:'目标',cell:(r:ApprovalRow)=>targetText(r.target_member)},
  {label:'内容摘要',wide:true,cell:(r:ApprovalRow)=><>{summaryText(r)}<br/><span className="note">摘要 <code>{r.digest}</code></span></>},
  {label:'截止',cell:(r:ApprovalRow)=><Time value={r.due_by}/>},
  {label:'批准',cell:(r:ApprovalRow)=>`${r.approvals} / ${r.quorum}`},
];

function DecideForms({member,row}:{member:Me;row:ApprovalRow}){
  const [reason,setReason]=useState('');
  const id='approval-reason-'+row.approval_id;
  return <>
    <div className="field"><label htmlFor={id}>原因</label><textarea id={id} value={reason} onChange={e=>setReason(e.target.value)} required maxLength={500} rows={2}/></div>
    <Form operation="decide_approval" member={member} className="inline-action" title="批准" disabled={!reason.trim()}
      body={()=>reason.trim()?{approval_id:row.approval_id,decision:'approve',reason:reason.trim()}:null}><span/></Form>
    <Form operation="decide_approval" member={member} className="inline-action" title="拒绝" disabled={!reason.trim()}
      body={()=>reason.trim()?{approval_id:row.approval_id,decision:'reject',reason:reason.trim()}:null}><span/></Form>
  </>;
}

/** The single-person mode banner (design §15.5): shown while the switch is on, and says when it does not apply. */
export function soloBanner(solo:{enabled:boolean;others_eligible:boolean}):string|null{
  if(!solo.enabled)return null;
  return solo.others_eligible?'单人模式未生效：已有其他成员可审批。':'单人模式已开启：审批可由本人批准（需要过去 24 小时内只有你一人有审批资格；每次自批都有审计和告警）。';
}

function SoloModeForm({member,enabled}:{member:Me;enabled:boolean}){
  const [reason,setReason]=useState('');
  return <Form operation="set_approval_solo_mode" member={member} title={enabled?'关闭单人模式':'开启单人模式'} disabled={!reason.trim()}
    body={()=>reason.trim()?{enabled:!enabled,reason:reason.trim()}:null}>
    <p className="impact-summary" role="note">{enabled?'关闭后，已有的自批票不再计入，需要另一位成员批准。':'开启后，只有在过去 24 小时内一直只有你一人有审批资格时，你才能批准自己的请求；出现第二位有资格的成员即自动失效。'}</p>
    <div className="field"><label htmlFor="solo-reason">原因</label><textarea id="solo-reason" value={reason} onChange={e=>setReason(e.target.value)} required maxLength={500} rows={2}/></div>
  </Form>;
}

export function Approvals(){
  const query=useQuery({queryKey:['workbench','approvals'],queryFn:readApprovals});
  if(query.isPending)return <section><h1>审批</h1><p role="status">正在加载…</p></section>;
  if(query.isError)return <section><h1>审批</h1><Empty kind={kindOf(query.error)==='forbidden'?'forbidden':'unavailable'}/></section>;
  const {to_decide,mine,to_execute,solo_mode}=query.data;
  const banner=soloBanner(solo_mode);
  return <section aria-labelledby="approvals-title"><h1 id="approvals-title">审批</h1>
    {banner?<p className="impact-summary" role="status">{banner}</p>:null}
    <p className="note">需要审批的操作由另一位有资格的成员批准，再由发起人自己执行；发起人不能批准自己的请求。执行时还会重查批准人资格、期限和暂停；派发时照常检查全部闸门。</p>
    <h2>待我审批</h2>
    {to_decide.length===0?<Empty kind="none" title="没有待你审批的请求"/>:<DataTable label="待我审批" rows={to_decide} rowKey={r=>r.approval_id} columns={[
      ...common,{label:'发起人',cell:r=><><code>{r.requested_by}</code><br/><Time value={r.requested_at}/></>},
      {label:'审批',cell:r=><WithMember>{member=><DecideForms member={member} row={r}/>}</WithMember>}]}/>}
    <h2>待我执行</h2>
    {to_execute.length===0?<Empty kind="none" title="没有已批准待执行的请求"/>:<DataTable label="待我执行" rows={to_execute} rowKey={r=>r.approval_id} columns={[
      ...common,{label:'执行',cell:r=>{const operation=r.operation;
        return isOperation(operation)&&r.inputs?<WithMember>{member=><Form operation={operation} member={member} className="inline-action" title="执行"
          body={()=>r.inputs??null}><span/></Form>}</WithMember>:<span className="note">工作台不能执行这条请求（不是工作台发起的请求体），请用发起时的入口执行</span>;}}]}/>}
    <h2>我发起的</h2>
    {mine.length===0?<Empty kind="none" title="最近 30 天没有你发起的审批"/>:<DataTable label="我发起的" rows={mine} rowKey={r=>r.approval_id} columns={[
      ...common,{label:'状态',cell:r=><>{APPROVAL_STATUS_TEXT[r.status]}{r.consumed?<>{' · '}<strong className="approval-consumed">已执行</strong></>:null}<br/>
        <span className="note">{r.consumed?<>{resultText(r.result)}{r.consumed_at?<>，<Time value={r.consumed_at}/></>:null}；这次批准已用掉，不能再执行</>:resultText(r.result)}</span></>},
      {label:'撤回',cell:r=>r.status==='waiting_approval'?<WithMember>{member=><Form operation="cancel_approval" member={member} className="inline-action" title="撤回"
        body={()=>({approval_id:r.approval_id,reason:'发起人撤回'})}><span/></Form>}</WithMember>:null}]}/>}
    <WithMember>{member=><SoloModeForm member={member} enabled={solo_mode.enabled}/>}</WithMember>
  </section>;
}
