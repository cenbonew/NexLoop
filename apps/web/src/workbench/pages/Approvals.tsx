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
const isOperation=(value:string):value is Operation=>Object.prototype.hasOwnProperty.call(OPERATION_ACTION,value);

const common=[
  {label:'操作',main:true,cell:(r:ApprovalRow)=><><code>{r.operation}</code><br/><span className="note">{r.capability} · 策略 v{r.policy_version}</span></>},
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

export function Approvals(){
  const query=useQuery({queryKey:['workbench','approvals'],queryFn:readApprovals});
  if(query.isPending)return <section><h1>审批</h1><p role="status">正在加载…</p></section>;
  if(query.isError)return <section><h1>审批</h1><Empty kind={kindOf(query.error)==='forbidden'?'forbidden':'unavailable'}/></section>;
  const {to_decide,mine,to_execute}=query.data;
  return <section aria-labelledby="approvals-title"><h1 id="approvals-title">审批</h1>
    <p className="note">需要审批的操作由另一位有资格的成员批准，再由发起人自己执行；发起人不能批准自己的请求。执行时还会重查批准人资格、期限和暂停；派发时照常检查全部闸门。</p>
    <h2>待我审批</h2>
    {to_decide.length===0?<Empty kind="none" title="没有待你审批的请求"/>:<DataTable label="待我审批" rows={to_decide} rowKey={r=>r.approval_id} columns={[
      ...common,{label:'发起人',cell:r=><><code>{r.requested_by}</code><br/><Time value={r.requested_at}/></>},
      {label:'审批',cell:r=><WithMember>{member=><DecideForms member={member} row={r}/>}</WithMember>}]}/>}
    <h2>待我执行</h2>
    {to_execute.length===0?<Empty kind="none" title="没有已批准待执行的请求"/>:<DataTable label="待我执行" rows={to_execute} rowKey={r=>r.approval_id} columns={[
      ...common,{label:'执行',cell:r=>{const operation=r.operation;
        return isOperation(operation)&&r.inputs?<WithMember>{member=><Form operation={operation} member={member} className="inline-action" title="执行"
          body={()=>r.inputs??null}><span/></Form>}</WithMember>:<span className="note">工作台不提供这个操作</span>;}}]}/>}
    <h2>我发起的</h2>
    {mine.length===0?<Empty kind="none" title="最近 30 天没有你发起的审批"/>:<DataTable label="我发起的" rows={mine} rowKey={r=>r.approval_id} columns={[
      ...common,{label:'状态',cell:r=><>{APPROVAL_STATUS_TEXT[r.status]}<br/><span className="note">{resultText(r.result)}</span></>},
      {label:'撤回',cell:r=>r.status==='waiting_approval'?<WithMember>{member=><Form operation="cancel_approval" member={member} className="inline-action" title="撤回"
        body={()=>({approval_id:r.approval_id,reason:'发起人撤回'})}><span/></Form>}</WithMember>:null}]}/>}
  </section>;
}
