/**
 * NX-028 page integration: the governed write controls of slices 2/3 in each page's action slot.
 * A control is offered only when the member's role carries its Action (D2, readMe); the governed entry still decides every
 * write with the caller's current grants (403 is shown as such). After a write every workbench read is refetched, so the next
 * read shows the result. A 503 or unreadable response means the result is unknown: it is said so, and a retry reuses the same
 * Idempotency-Key (the governed entry answers a replay from its terminal outcome, nothing runs twice).
 */
import {statusLabel} from './status-labels';
import {IdText,shorten} from './kit';
import {useEffect,useId,useRef,useState,type FormEvent,type ReactNode} from 'react';
import {useMutation,useQuery,useQueryClient,type QueryClient} from '@tanstack/react-query';
import {oneTimeSecret,approvalOutcome,refusalTextOf,COMMITMENT_STATUS_TEXT,OPEN_STATUSES,STATE_TEXT,UNKNOWN_RESULT,WRITE_TEXT,technical,kindOf,newRequestKey,offers,readActions,readCommitment,readConsumer,
  readConversation,readMe,readPlans,readTakeovers,takeoverOf,workbenchAct,writeKindOf,readAlerts,type Me,type Operation,type Takeover} from './api';
import type {PageKey,Route} from './route';
import {when} from './pages/Consumers';

export type SlotValue=ReactNode|((route:Route)=>ReactNode);
export type Slots=Partial<Record<PageKey,SlotValue>>;

/** Success text per operation: what happened, and what it does not mean. */
export const DONE_TEXT:Record<Operation,string>={
  publish_operator_persona:'已提交；人设版本状态已刷新。版本一经发布不可修改，改动请发布新版本。',
  assign_operator_persona:'已提交；这位客户下次对话起使用新的人设（进行中的对话不切换）。',
  publish_persona_rules:'已发布新的分群规则；受影响的客户在下次对话时更换人设并留下记录。',
  set_ai_disclosure:'已保存 AI 身份披露配置；合规责任由使用方承担。',
  dismiss_contact_preference:'已提交；这条偏好的采纳状态已更新，派发与上下文按新状态执行。',
  set_outreach_controls:'已保存外联控制；新设置的修订号已刷新。合规责任由使用方承担。',
  set_outreach_purpose:'已更新这个外联目的的启用状态；派发预演按新状态判定。',
  decide_outreach_approval:'已记录审批结论；批准的外联仍要在派发时通过全部闸门，拒绝即终止。',
  publish_semantic_release:'已发布新的语义版本；之后的语义读取与召回使用新版本。分级来自评估报告，不能手工修改。',
  set_evolution_stop:'已更新演进停止开关；停止期间不能开评估，也不能发布这个维度的候选。',
  rollback_semantic_release:'已回退：生成了一个更高的版本号，内容等于目标版本。已有 Claim 不会被改动，回退报告列出受影响的 Claim 供人工复核。',
  set_control:'已提交；派发预判与计划状态已刷新。恢复不会重放暂停期间被拒的意图。',
  release_contact_restriction:'已解除；之前被拒的意图不会重放，计划将重新评估。',
  cancel_commitment:'已取消承诺。',extend_commitment:'已延期：原承诺已被新承诺取代。',attest_commitment:'已记录人工证明。',
  commitment_condition_met:'已记录条件满足。',mark_commitment_communication:'已标记为沟通类承诺。',
  request_plan_reevaluation:'已请求复评；是否启动复评 Run 仍由预检决定。',
  request_effect_query:'已请求查询执行结果；不会重发，也不会换新的幂等键。',
  take_over_conversation:'已接管：接管期间 Agent 不回复，客户看到“人工客服处理中”。',
  hand_back_conversation:'已交还：只有最新一条未回复的来信恢复待回复，更早的记为接管期间已处理；计划将重新评估。',
  send_staff_reply:'已发送给客户；不代表问题已解决或承诺已兑现。',
  silence_alert:'已静默：期间仍会评估和记录，只是不再提示；到期自动结束。',
  set_real_dispatch:'已提交；开关以重新读取的状态为准。部署层 REAL_DISPATCH_ENABLED 不在工作台控制范围内。',
  decide_approval:'已记录审批结论；批准后由发起人执行，执行时还会重查批准人资格、期限和暂停。',
  cancel_approval:'已撤回这条审批请求。',
  set_approval_solo_mode:'已保存单人模式开关；是否生效以页面顶部的提示为准（需要过去 24 小时内只有你一人有审批资格）。',
  erase_consumer:'已开始擦除：先阻断联系和计划，删除由保留期服务逐步完成，不可撤销。',
  resolve_remediation:'已记录；只改变这条补救的状态，失败的 Action 与失败率不变，也不会重发。',
  switch_remediation:'已提交；补救开关以重新读取的状态为准。部署层 REMEDIATION_ENABLED 不在工作台控制范围内。',
  release_remediation_rule:'已解除熔断；这条规则从下一个失败起重新生成补救，已升级的补救不变。',
  resolve_extraction_dead_letter:'已记录；选择重新入队时已生成一条新的提取任务，由提取 worker 处理。死信记录本身保留不变。',
  invite_workbench_member:'已签发邀请；邀请链接中的令牌只在这里显示一次，请线下转交本人。同一用户名再签发会让旧邀请失效。',
  revoke_workbench_invitation:'已撤销这份邀请；令牌立即失效。',
  issue_workbench_reset:'已签发重置码；这位成员的现有会话已全部失效。重置码只在这里显示一次，30 分钟内有效，请线下转交本人。',
  change_workbench_member_role:'已改角色；这位成员的会话已失效，重新登录后按新角色使用。',
  suspend_workbench_member:'已停用；授权已撤销，会话已失效。恢复时按角色当前的模板重新授权。',
  resume_workbench_member:'已恢复；按角色当前的模板重新授权。',
  revoke_workbench_member:'已撤销成员；授权、配置和会话都已结束。账号保留，如需擦除个人数据请另行发起擦除。',
  promote_workbench_member:'已提升为负责人；这位成员的会话已失效。投票资格要等 24 小时后才生效。',
  demote_workbench_owner:'已降级；这位成员的会话、重置码和未接受的邀请都已结束。',
  suspend_workbench_owner:'已停用这位负责人；授权已撤销，会话已失效。',
  resume_workbench_owner:'已恢复这位负责人；投票资格要等 24 小时后才生效。',
  revoke_workbench_owner:'已撤销这位负责人；授权、配置和会话都已结束。',
  issue_workbench_owner_reset:'已签发重置码；这位负责人的现有会话已全部失效。重置码只在这里显示一次，30 分钟内有效，请线下转交本人。',
  invite_workbench_owner:'已签发负责人邀请；令牌只在这里显示一次，请线下转交本人。',
  erase_workbench_member:'已擦除：账号停用、密码清空、用户名与邮箱改为假名，邀请与重置记录已删除；不可撤销。失败登录记录中的用户名在 90 天后清除。',
};

/** One governed write as a plain function (the hook below and the tests use it). `pending` keeps the request key while the
 * result is unknown; a definite outcome (success, 4xx) clears it so the next submission is a new request. */
export async function governedWrite(cache:QueryClient,operation:Operation,body:Record<string,unknown>,pending:{current:{body:string;key:string}|null}){
  const text=JSON.stringify(body);
  if(!pending.current||pending.current.body!==text)pending.current={body:text,key:newRequestKey()};
  try{
    const result=await workbenchAct(operation,body,pending.current.key);
    pending.current=null;
    return result;
  }catch(error){
    if(!UNKNOWN_RESULT.includes(writeKindOf(error)))pending.current=null;
    throw error;
  }finally{
    // Whatever happened, the next read shows the current state (an unknown result may have taken effect).
    await cache.invalidateQueries({queryKey:['workbench']});
  }
}

function useWrite(operation:Operation,onDone?:()=>void){
  const cache=useQueryClient();const pending=useRef<{body:string;key:string}|null>(null);
  // onDone is a mutation-level callback: it still runs when the refetch after the write has already replaced this form
  // (take-over → hand-back), which a per-call mutate() callback would not.
  const done=useRef(onDone);done.current=onDone;
  // gcTime 0: a result (which may hold a one-time code or token) is not kept in the mutation cache once the form is gone.
  return useMutation({mutationFn:(body:Record<string,unknown>)=>governedWrite(cache,operation,body,pending),onSuccess:()=>done.current?.(),gcTime:0});
}

function Outcome({operation,write}:{operation:Operation;write:ReturnType<typeof useWrite>}){
  if(write.isPending)return <p role="status">正在提交…</p>;
  // NX-056: an operation under an approval policy answers with its approval state instead of the plain success text.
  if(write.isSuccess)return <WriteResult operation={operation} data={write.data} onHide={()=>write.reset()}/>;
  if(write.isError){const kind=writeKindOf(write.error);
    const detail=refusalTextOf(write.error);
    return <div className={UNKNOWN_RESULT.includes(kind)?'pending':'error'} role="alert"><p>{WRITE_TEXT[kind]}{detail?<>{' '}<span className="refusal-reason">{detail}</span></>:null}</p>
      <details className="state-details"><summary>技术细节</summary>{technical(kind)}</details></div>;}
  return null;
}

/** The success view of one write (also the approvals page's execution): the approval state or the success text; a replay's hint; a
 * fresh execution's one-time code or token (ADR-039 R3 §8.4). */
export function WriteResult({operation,data,onHide}:{operation:Operation;data:Record<string,unknown>|undefined;onHide?:()=>void}){
  const secret=oneTimeSecret(data);
  const replay=data?.secret==='not_shown_again'?<p className="impact-summary" role="alert">这次请求已经执行过，重置码或邀请令牌不会再次显示。
    如果没有拿到：先撤销这份邀请（重置码再签发时旧码自动作废），再重新签发。</p>:null;
  return <><p className="note" role="status">{approvalOutcome(data)??DONE_TEXT[operation]}</p>{replay}{secret?<OneTimeSecret {...secret} onHide={onHide}/>:null}</>;
}

/** NX-058: a reset code or an invitation token, shown once to the issuer (it is in no read, list or log; the page keeps it only while
 * this form is on screen). The invitation link carries the token in the fragment, which the accept page clears at once. */
export function OneTimeSecret({kind,value,onHide}:{kind:'code'|'token';value:string;onHide?:()=>void}){
  // Cleared 2 minutes after it was first shown (or at once with the button): the secret leaves the component state and the mutation
  // result. The deadline is fixed at the first render and the callback held in a ref, so a re-render or refetch never restarts it.
  const [shownAt]=useState(()=>Date.now());const hide=useRef(onHide);hide.current=onHide;
  useEffect(()=>{const timer=setTimeout(()=>hide.current?.(),Math.max(0,shownAt+120_000-Date.now()));return ()=>clearTimeout(timer);},[shownAt]);
  const origin=globalThis.location?.origin??'';
  const link=kind==='token'?origin+'/workbench/invitation#token='+encodeURIComponent(value):null;
  return <div className="one-time-secret" role="alert"><p><strong>{kind==='code'?'重置码':'邀请令牌'}（只显示这一次）</strong></p>
    <p><code className="id-code">{value}</code></p>
    {link?<p>邀请链接：<code className="id-code">{link}</code></p>:<p>重置页：<code className="id-code">{origin}/workbench/reset</code></p>}
    <p className="note">请线下（当面或电话）交给本人；离开这一页或 2 分钟后无法再次查看。</p>
    {onHide?<button type="button" onClick={onHide}>我已记下，隐藏</button>:null}</div>;
}

function Reason({id,value,onChange,label='原因'}:{id:string;value:string;onChange:(v:string)=>void;label?:string}){
  return <div className="field"><label htmlFor={id}>{label}</label><textarea id={id} value={value} onChange={e=>onChange(e.target.value)} required maxLength={500} rows={2}/></div>;
}

/** A form for one operation: shown only when the role carries it. `tone="danger"` marks an irreversible or revoking operation
 * (UX audit B4); `disabledReason` says, under the disabled button and tied to it, what is still missing (B6). */
export function Form({operation,title,member,children,body,disabled,disabledReason,onDone,onBusy,className,tone,level='h2'}:{operation:Operation;title:string;member:Me;children:ReactNode;body:()=>Record<string,unknown>|null;
  disabled?:boolean;disabledReason?:string;onDone?:()=>void;onBusy?:(busy:boolean)=>void;className?:string;tone?:'danger';
  /** The title's heading level: h3 for a form inside a titled section. */level?:'h2'|'h3'}){
  const write=useWrite(operation,onDone);
  const Heading=level;
  const reasonId=useId();
  // `onBusy`: a container that could unmount this form (a confirmation panel) learns that a request is in flight (L6 review of S5).
  const busy=useRef(onBusy);busy.current=onBusy;
  useEffect(()=>{busy.current?.(write.isPending);},[write.isPending]);
  if(!offers(member,operation))return null;
  function submit(event:FormEvent){event.preventDefault();const b=body();if(b)write.mutate(b);}
  const why=disabled&&!write.isPending&&disabledReason?disabledReason:null;
  return <form className={'workbench-action'+(className?' '+className:'')} aria-label={title} onSubmit={submit}><Heading>{title}</Heading>{children}
    <button type="submit" className={tone==='danger'?'btn-danger':undefined} disabled={disabled||write.isPending} aria-describedby={why?reasonId:undefined}>
      {write.isError&&UNKNOWN_RESULT.includes(writeKindOf(write.error))?'用同一请求重试':title}</button>
    {why?<p className="disabled-reason" id={reasonId}>{why}</p>:null}
    <Outcome operation={operation} write={write}/></form>;
}

/** UX audit B5: a row action that used to run on one click (and wrote a reason nobody typed). The trigger opens a confirmation in place:
 * what it does, the member's own reason (when the Action takes one), then the confirm button; 取消 or Escape closes it and returns focus
 * to the trigger. Shown only when the role carries the operation. */
export function ConfirmedForm({operation,member,trigger,title,impact,tone,withReason=true,reasonLabel='原因',body}:{operation:Operation;member:Me;trigger:string;title:string;
  impact:ReactNode;tone?:'danger';withReason?:boolean;reasonLabel?:string;body:(reason:string)=>Record<string,unknown>}){
  const [open,setOpen]=useState(false);const [reason,setReason]=useState('');
  // L6 review of S5: while the request is in flight the panel cannot close - closing would unmount the form, so its outcome and the
  // same-request retry of an unknown result would be lost. It closes once the result is shown.
  const [pending,setPending]=useState(false);
  const button=useRef<HTMLButtonElement>(null);const panel=useRef<HTMLDivElement>(null);const was=useRef(false);const id=useId();
  useEffect(()=>{if(open)panel.current?.querySelector<HTMLElement>('textarea,button[type=submit]')?.focus();else if(was.current)button.current?.focus();was.current=open;},[open]);
  if(!offers(member,operation))return null;
  if(!open)return <button type="button" ref={button} className={'confirm-trigger'+(tone==='danger'?' confirm-trigger-danger':'')} aria-expanded={false} onClick={()=>setOpen(true)}>{trigger}</button>;
  return <div className="confirm-panel" ref={panel} role="group" aria-label={title} onKeyDown={e=>{if(e.key==='Escape'){e.preventDefault();if(!pending)setOpen(false);}}}>
    <Form operation={operation} member={member} title={title} tone={tone} className="inline-confirm" disabled={withReason&&!reason.trim()}
      disabledReason={withReason?'填写原因后可确认。':undefined} onBusy={setPending} body={()=>withReason&&!reason.trim()?null:body(reason.trim())}>
      <p className="impact-summary" role="note">{impact}</p>
      {withReason?<Reason id={id+'-reason'} value={reason} onChange={setReason} label={reasonLabel}/>:null}
    </Form>
    <button type="button" className="btn-secondary" disabled={pending} onClick={()=>setOpen(false)}>{pending?'等待结果…':'取消'}</button>
  </div>;
}

/** The member's role decides which controls are offered; without it nothing is guessed. */
export function WithMember({children}:{children:(member:Me)=>ReactNode}){
  const member=useQuery({queryKey:['workbench','me'],queryFn:readMe,refetchOnWindowFocus:false});
  if(member.isPending)return null;  // C13: no "正在加载…" line at every form; the form appears once the role is known
  if(member.isError)return <p className="note" role="status">操作区暂不可用：{STATE_TEXT[kindOf(member.error)]}</p>;
  return <>{children(member.data)}</>;
}

function utc(local:string):string|null{const d=new Date(local);return Number.isNaN(d.getTime())?null:d.toISOString();}

// ---- remediation intents (NX-057 R1, owner) ----
/** Resolve one open remediation: handled (a person took it) or cancelled. Nothing else changes, nothing is sent again. */
export function RemediationResolveForm({member,remediationId,kind}:{member:Me;remediationId:string;kind?:string}){
  const [outcome,setOutcome]=useState<'handled'|'cancelled'|'wake'|''>('');const [reason,setReason]=useState('');
  return <Form operation="resolve_remediation" title="处理这条补救" member={member} disabled={!outcome}
    body={()=>outcome?{remediation_id:remediationId,outcome,reason}:null}>
    <fieldset className="choices"><legend>结论</legend>
      <label className="choice"><input type="radio" name={'remediation-'+remediationId} checked={outcome==='handled'} onChange={()=>setOutcome('handled')}/>标记已处理</label>
      {kind==='reevaluate'?<label className="choice"><input type="radio" name={'remediation-'+remediationId} checked={outcome==='wake'} onChange={()=>setOutcome('wake')}/>唤醒计划</label>:null}
      <label className="choice"><input type="radio" name={'remediation-'+remediationId} checked={outcome==='cancelled'} onChange={()=>setOutcome('cancelled')}/>取消</label></fieldset>
    <p className="impact-summary" role="note">{outcome==='wake'?'影响：唤醒等待它的计划，由新的重评估 Run 决定下一步；不会重发原来的 Action。':
      '影响：只改变这条补救的状态；失败的 Action、它的失败记录和失败率都不变，也不会重发。'}</p>
    <Reason id={'remediation-reason-'+remediationId} value={reason} onChange={setReason}/>
  </Form>;
}

// ---- extraction dead letters (0355, owner) ----
/** Resolve one dead-lettered claim-extraction job: requeue its Messages for the next extraction window, or dismiss it with a reason. */
export function ExtractionDeadLetterForm({member,taskId,existingClaims=0}:{member:Me;taskId:string;existingClaims?:number}){
  const [disposition,setDisposition]=useState<'requeue'|'dismiss'|''>('');const [reason,setReason]=useState('');
  return <Form operation="resolve_extraction_dead_letter" title="处理这条死信" member={member} disabled={!disposition}
    body={()=>disposition?{task_id:taskId,disposition,reason}:null}>
    <fieldset className="choices"><legend>处理方式</legend>
      <label className="choice"><input type="radio" name={'dead-letter-'+taskId} checked={disposition==='requeue'} onChange={()=>setDisposition('requeue')}/>重新入队</label>
      <label className="choice"><input type="radio" name={'dead-letter-'+taskId} checked={disposition==='dismiss'} onChange={()=>setDisposition('dismiss')}/>不再处理</label></fieldset>
    {existingClaims>0?<p className="note" role="note">这些消息已经提取出 {existingClaims} 条 Claim（之前的窗口部分成功过）。重新入队会把整个窗口再提取一次，可能产生重复的 Claim，一般选“不再处理”。</p>:null}
    <p className="impact-summary" role="note">{disposition==='requeue'?'影响：立即为这条任务的同一批消息生成一条新的提取任务，由提取 worker 处理；死信记录保留。':
      disposition==='dismiss'?'影响：只记录处理结论和原因，这些消息不会重新提取；死信记录保留。':
      '请选择处理方式。每条死信只能处理一次，死信记录本身始终保留。'}</p>
    <Reason id={'dead-letter-reason-'+taskId} value={reason} onChange={setReason}/>
  </Form>;
}

/** Release a rule the breaker held (design §9.3): it generates remediations again from the next failure. */
export function RemediationRuleReleaseForm({member,ruleId}:{member:Me;ruleId:string}){
  const [reason,setReason]=useState('');
  return <Form operation="release_remediation_rule" title="解除熔断" member={member} body={()=>({rule_id:ruleId,reason})}>
    <p className="impact-summary" role="note">影响：规则 <code className="id-code">{ruleId}</code> 从下一个失败起重新生成补救；熔断期间的失败不会补生成。</p>
    <Reason id={'remediation-release-'+ruleId} value={reason} onChange={setReason}/>
  </Form>;
}

/** The tenant's remediation switch: pausing takes effect at once; failures while paused get no remediation, now or later. */
export function RemediationSwitchForm({member,paused}:{member:Me;paused:boolean}){
  const [reason,setReason]=useState('');
  return <Form operation="switch_remediation" title={paused?'恢复补救':'暂停补救'} member={member} tone={paused?undefined:'danger'} body={()=>({paused:!paused,reason})}>
    <p className="impact-summary" role="note">影响：{paused?'之后的最终失败重新按规则生成补救；暂停期间的失败不会补生成。':'立即停止为新的最终失败生成补救；暂停期间的失败以后也不会补生成，已有的补救不受影响。'}</p>
    <Reason id="remediation-switch-reason" value={reason} onChange={setReason}/>
  </Form>;
}

// ---- pause / resume (owner, Control.set) ----
const SCOPES={tenant:'整个租户',consumer:'客户',role:'角色',strategy:'策略',action_type:'Action 类型'} as const;
/** What a pause/resume changes, said before it is submitted (F06): the scope is never assumed. */
export function controlImpact(kind:keyof typeof SCOPES|'',ref:string,pause:boolean|null):string|null{
  if(!kind||pause===null||(kind!=='tenant'&&!ref.trim()))return null;
  const target=kind==='tenant'?'整个租户（所有客户）':`${SCOPES[kind]} ${ref.length>16?ref.slice(0,12)+'…':ref}`;
  return pause?`将暂停 ${target} 的主动联系：之后的派发会被拒绝，来信必回不受影响。`:`将恢复 ${target} 的主动联系：暂停期间被拒的意图不会重放，计划将重新评估。`;
}
export function ControlForm({member,scopeKind,scopeRef,paused}:{member:Me;scopeKind?:keyof typeof SCOPES;scopeRef?:string;paused?:boolean}){
  const fixed=scopeKind!==undefined;
  const [kind,setKind]=useState<keyof typeof SCOPES|''>(scopeKind??'');const [ref,setRef]=useState(scopeRef??'');
  const [pause,setPause]=useState<boolean|null>(fixed?!paused:null);const [reason,setReason]=useState('');
  const impact=controlImpact(kind,kind==='tenant'?'*':ref,pause);
  return <Form operation="set_control" title={fixed?(paused?'恢复主动联系':'暂停主动联系'):pause===null?'暂停 / 恢复':pause?'暂停主动联系':'恢复主动联系'} member={member}
    disabled={!impact} body={()=>impact&&kind?({scope_kind:kind,scope_ref:kind==='tenant'?'*':ref,paused:pause,reason}):null}>
    {fixed?<p>范围：{SCOPES[scopeKind]} {scopeKind==='consumer'?<IdText value={ref}/>:ref}</p>:<>
      <div className="field"><label htmlFor="control-scope">范围</label><select id="control-scope" value={kind} required onChange={e=>{setKind(e.target.value as keyof typeof SCOPES|'');setRef('');}}>
        <option value="">请选择范围</option>{Object.entries(SCOPES).map(([k,v])=><option key={k} value={k}>{v}</option>)}</select></div>
      {kind&&kind!=='tenant'?<div className="field"><label htmlFor="control-ref">对象</label><input id="control-ref" value={ref} onChange={e=>setRef(e.target.value)} required maxLength={255}/></div>:null}
      <fieldset className="choices"><legend>操作</legend><label className="choice"><input type="radio" name="control-paused" checked={pause===true} onChange={()=>setPause(true)}/>暂停</label>
        <label className="choice"><input type="radio" name="control-paused" checked={pause===false} onChange={()=>setPause(false)}/>恢复</label></fieldset></>}
    {impact?<p className="impact-summary" role="note">影响：{impact}</p>:<p className="note">请先选择范围{fixed?'':'和操作'}。</p>}
    <Reason id="control-reason" value={reason} onChange={setReason}/>
  </Form>;
}

// ---- contact restriction release (owner only, ADR-023 §2.4) ----
/** Release one customer's restriction: entered from that customer's record (F06), never a preselected customer. */
export function ReleaseForm({member,consumerId,context}:{member:Me;consumerId:string;context?:ReactNode}){
  const [reason,setReason]=useState('');
  return <Form operation="release_contact_restriction" title="解除联系限制" member={member} body={()=>({consumer_id:consumerId,reason})}>
    <p>客户：<IdText value={consumerId}/></p>{context}
    <p className="impact-summary" role="note">影响：只解除这位客户的联系限制；之前被拒的意图不会重放，计划将重新评估。</p>
    <Reason id="release-reason" value={reason} onChange={setReason} label="解除原因（例如客户已确认可以联系）"/>
  </Form>;
}

/** Consumer detail: pause/resume this consumer, release its restriction (owner), take the whole consumer over. */
export function ConsumerControls({consumerId}:{consumerId:string}){
  const consumer=useQuery({queryKey:['workbench','consumer',consumerId],queryFn:()=>readConsumer(consumerId)});
  return <WithMember>{member=><>
    {consumer.data?<ControlForm member={member} scopeKind="consumer" scopeRef={consumerId} paused={consumer.data.paused}/>:null}
    {consumer.data?.restriction?.active?<ReleaseForm member={member} consumerId={consumerId}/>:null}
    <TakeoverControls member={member} scopeKind="consumer" scopeRef={consumerId} consumerId={consumerId}/>
  </>}</WithMember>;
}

// ---- commitments (NX-026 human Actions) ----
export function CommitmentControls({commitmentId}:{commitmentId:string}){
  const view=useQuery({queryKey:['workbench','commitment',commitmentId],queryFn:()=>readCommitment(commitmentId)});
  return <WithMember>{member=>{
    if(!view.data)return null;
    if(!OPEN_STATUSES.includes(view.data.status))return <p className="note">承诺已结束（{statusLabel('commitment_status',view.data.status)}），不能再执行操作。</p>;
    const subject=typeof view.data.properties.predicate==='string'?view.data.properties.predicate:null;
    const op=(operation:Operation,title:string,effect:string,extra?:{id:string;label:string;key:'occurred_at'|'due_at'})=>offers(member,operation)
      ?<CommitmentOperation key={operation} member={member} operation={operation} title={title} effect={effect} commitmentId={commitmentId} subject={subject} extra={extra}/>:null;
    return <div className="commitment-operations">
      {view.data.status==='conditional'?op('commitment_condition_met','条件已满足','记录条件已满足，承诺转为待兑现。'):null}
      {op('attest_commitment','人工证明已兑现','以人工证明记录兑现时间，承诺结束为已兑现。',{id:'attest-at',label:'兑现时间',key:'occurred_at'})}
      {op('extend_commitment','延期','原承诺被一条新期限的承诺取代。',{id:'extend-due',label:'新期限',key:'due_at'})}
      {op('mark_commitment_communication','标记为沟通类承诺','承诺改为沟通类，不再按交付追踪。')}
      {op('cancel_commitment','取消承诺','承诺结束为已取消。')}
    </div>;}}</WithMember>;
}

/** One commitment operation, closed until chosen: its own object, effect and reason (F02). */
function CommitmentOperation({member,operation,title,effect,commitmentId,subject,extra}:{member:Me;operation:Operation;title:string;effect:string;commitmentId:string;subject:string|null;
  extra?:{id:string;label:string;key:'occurred_at'|'due_at'}}){
  const [reason,setReason]=useState('');const [at,setAt]=useState('');
  return <details className="workbench-op"><summary>{title}</summary>
    <Form operation={operation} title={title} member={member} tone={operation==='cancel_commitment'?'danger':undefined} body={()=>{if(!extra)return {commitment_id:commitmentId,reason};const v=utc(at);return v?{commitment_id:commitmentId,[extra.key]:v,reason}:null;}}>
      <p>对象：承诺 {subject?`“${subject}” `:''}<IdText value={commitmentId}/></p>
      <p className="impact-summary" role="note">影响：{effect}</p>
      {extra?<div className="field"><label htmlFor={extra.id}>{extra.label}</label><input id={extra.id} type="datetime-local" value={at} onChange={e=>setAt(e.target.value)} required/></div>:null}
      <Reason id={`${operation}-reason`} value={reason} onChange={setReason}/>
    </Form></details>;
}

// ---- manual plan reevaluation / effect result query (D7) ----
export function ReevaluateForm({member}:{member:Me}){
  const plans=useQuery({queryKey:['workbench','plans',''],queryFn:()=>readPlans(),enabled:offers(member,'request_plan_reevaluation')});
  const [chosen,setChosen]=useState('');const [reason,setReason]=useState('');
  const ids=plans.data?.map(p=>p.plan_id)??[];const target=ids.includes(chosen)?chosen:'';
  if(plans.data&&!ids.length)return null;
  return <Form operation="request_plan_reevaluation" title="手动复评" member={member} disabled={!target} body={()=>target?{plan_id:target,reason}:null}>
    {ids.length?<div className="field"><label htmlFor="reevaluate-plan">计划</label><select id="reevaluate-plan" value={target} required onChange={e=>setChosen(e.target.value)}><option value="">请选择计划</option>
      {plans.data!.map(p=><option key={p.plan_id} value={p.plan_id}>{shorten(p.plan_id)} v{p.version}（{statusLabel('plan_status',p.status)}）</option>)}</select></div>:<p className="note">暂无计划。</p>}
    <Reason id="reevaluate-reason" value={reason} onChange={setReason}/>
  </Form>;
}

export function EffectQueryForm({member}:{member:Me}){
  const unknown=useQuery({queryKey:['workbench','actions','unknown'],queryFn:()=>readActions('unknown'),enabled:offers(member,'request_effect_query')});
  const [chosen,setChosen]=useState('');const [reason,setReason]=useState('');
  const ids=unknown.data?.intents.map(i=>i.intent_id)??[];const target=ids.includes(chosen)?chosen:'';
  if(unknown.data&&!ids.length)return null;
  return <Form operation="request_effect_query" title="查询执行结果" member={member} disabled={!target} body={()=>target?{intent_id:target,reason}:null}>
    <p className="note">对“待核对”的意图向渠道查询结果；不是重发。</p>
    {ids.length?<div className="field"><label htmlFor="query-intent">意图</label><select id="query-intent" value={target} required onChange={e=>setChosen(e.target.value)}><option value="">请选择待核对的意图</option>
      {unknown.data!.intents.map(i=><option key={i.intent_id} value={i.intent_id}>{i.intent_id.slice(0,8)}… {i.action_name}</option>)}</select></div>:<p className="note">没有待核对的意图。</p>}
    <Reason id="query-reason" value={reason} onChange={setReason}/>
  </Form>;
}

// ---- takeover / hand-back / staff reply (slice 3, D3–D6) ----
export function takeoverText(t:Takeover):string{
  return `人工接管中（${t.scope_kind==='consumer'?'整个客户':'本会话'}）：${t.mine?'由我':'由 '+t.taken_by}接管，到期 ${when(t.expires_at)}`;
}

function TakeoverControls({member,scopeKind,scopeRef,consumerId,conversationId}:{member:Me;scopeKind:'conversation'|'consumer';scopeRef:string;consumerId:string;conversationId?:string}){
  const takeovers=useQuery({queryKey:['workbench','takeovers'],queryFn:readTakeovers});
  const [minutes,setMinutes]=useState<number|null>(null);const [reason,setReason]=useState('');
  // The last takeover / hand-back result stays visible after the form it came from is replaced (desktop usability 2026-10-17).
  const [notice,setNotice]=useState<{operation:Operation;at:string}|null>(null);
  const done=(operation:Operation)=>()=>{setNotice({operation,at:new Date().toLocaleTimeString('zh-CN',{hour12:false})});setReason('');};
  if(takeovers.isPending)return <p role="status">{STATE_TEXT.loading}</p>;
  if(takeovers.isError)return <p className="note" role="status">接管状态暂不可用：{STATE_TEXT[kindOf(takeovers.error)]}</p>;
  const {policy,takeovers:list}=takeovers.data;
  const current=conversationId?takeoverOf(list,conversationId,consumerId):list.find(t=>t.consumer_id===consumerId)??null;
  const value=minutes??Math.round(policy.default_seconds/60);
  return <section className={'takeover-bar'+(current?' takeover-on':'')} aria-label="人工接管">
    <div className="takeover-status" role="status">
      <span className={'takeover-pill'+(current?' takeover-pill-on':'')}>{current?'人工接管中':'Agent 处理中'}</span>
      <span>{current?takeoverText(current):scopeKind==='consumer'?'该客户的会话由 Agent 回复。':'本会话由 Agent 回复；接管后 Agent 暂停回复，客户看到“人工客服处理中”。'}</span>
    </div>
    {notice?<p className={'takeover-notice'+(notice.operation==='hand_back_conversation'?' takeover-notice-back':'')} role="status">
      {notice.at} · {notice.operation==='hand_back_conversation'?'已交还给 Agent。':'已接管。'}{DONE_TEXT[notice.operation].replace(/^已(交还|接管)：/,'')}</p>:null}
    {current?<Form key="hand-back" operation="hand_back_conversation" title="交还给 Agent" member={member} className="takeover-form" onDone={done('hand_back_conversation')}
      body={()=>({takeover_id:current.takeover_id,reason})}>
      <Reason id="handback-reason" value={reason} onChange={setReason}/></Form>
    :<Form key="take-over" operation="take_over_conversation" title={scopeKind==='consumer'?'接管整个客户':'接管本会话'} member={member} className="takeover-form" onDone={done('take_over_conversation')}
      body={()=>({scope_kind:scopeKind,scope_ref:scopeRef,duration_seconds:value*60,reason})}>
      <div className="field"><label htmlFor="takeover-minutes">时长（分钟，最长 {Math.round(policy.max_seconds/60)}）</label>
        <input id="takeover-minutes" type="number" min={1} max={Math.round(policy.max_seconds/60)} value={value} onChange={e=>setMinutes(Number(e.target.value))} required/></div>
      <Reason id="takeover-reason" value={reason} onChange={setReason}/>
    </Form>}
  </section>;
}

/** Conversation page: takeover state and controls; while I hold the takeover, reply to one inbound message (native WebChat only). */
export function ConversationControls({conversationId}:{conversationId:string}){
  const conversation=useQuery({queryKey:['workbench','conversation',conversationId],queryFn:()=>readConversation(conversationId)});
  const takeovers=useQuery({queryKey:['workbench','takeovers'],queryFn:readTakeovers});
  const [replyTo,setReplyTo]=useState('');const [text,setText]=useState('');
  return <WithMember>{member=>{
    if(!conversation.data)return null;
    const consumerId=conversation.data.consumer_id;
    const current=takeovers.data?takeoverOf(takeovers.data.takeovers,conversationId,consumerId):null;
    const inbound=conversation.data.messages.filter(m=>m.direction==='inbound');
    const target=replyTo||inbound[inbound.length-1]?.id||'';
    return <>
      <TakeoverControls member={member} scopeKind="conversation" scopeRef={conversationId} consumerId={consumerId} conversationId={conversationId}/>
      {current&&!current.mine&&offers(member,'send_staff_reply')?<p className="note">只有发起接管的成员可以在接管期间回复。</p>:null}
      {current?.mine?<Form operation="send_staff_reply" title="以人工客服身份回复" member={member} disabled={!target||!text.trim()}
        body={()=>target&&text.trim()?{conversation_id:conversationId,reply_to:target,text}:null}>
        {inbound.length?<div className="field"><label htmlFor="staff-reply-to">回复哪条来信</label><select id="staff-reply-to" value={target} onChange={e=>setReplyTo(e.target.value)}>
          {inbound.map(m=><option key={m.id} value={m.id}>#{m.sequence} {m.content.status==='ok'&&m.content.body?m.content.body.slice(0,30):'（原文需授权）'}</option>)}</select></div>:<p className="note">暂无来信。</p>}
        <div className="field"><label htmlFor="staff-reply-text">回复内容</label><textarea id="staff-reply-text" value={text} onChange={e=>setText(e.target.value)} required maxLength={8192} rows={3}/></div>
        <p className="note">只限原生 WebChat 会话；受限客户只能回复绑定的来信且在时间窗内，每条来信至多一条回复。</p>
      </Form>:null}
    </>;}}</WithMember>;
}

/** Page slots (WorkbenchPage resolves a function against the current route). */
/** NX-030: silence one firing rule (optionally only its selector) for at most 7 days; owner only (the role decides the offer). */
export function silenceUntil(hours:number,now:number=Date.now()):string{
  const bounded=Math.min(Math.max(hours,1),7*24);return new Date(now+bounded*3600_000).toISOString().replace(/\.\d{3}Z$/,'Z');
}
export function SilenceForm({member}:{member:Me}){
  const firing=useQuery({queryKey:['workbench','alerts'],queryFn:readAlerts});
  const [key,setKey]=useState('');const [onlySelector,setOnlySelector]=useState(true);const [hours,setHours]=useState(24);const [reason,setReason]=useState('');
  const options=firing.data?.firing??[];const chosen=options.find(a=>a.dedupe_key===key)??options[0];
  if(firing.data&&!options.length)return null;
  return <Form operation="silence_alert" title="静默告警" member={member} disabled={!chosen||!reason.trim()}
    body={()=>chosen?{rule_id:chosen.rule_id,selector:onlySelector?chosen.selector:null,until:silenceUntil(hours),reason}:null}>
    {options.length?<div className="field"><label htmlFor="silence-alert">告警</label><select id="silence-alert" value={chosen?.dedupe_key} onChange={e=>setKey(e.target.value)}>
      {options.map(a=><option key={a.dedupe_key} value={a.dedupe_key}>{a.rule_id}{a.selector?` · ${a.selector}`:''}</option>)}</select></div>
      :<p className="note">当前没有可静默的触发中告警。</p>}
    {chosen?.selector?<label className="choice"><input type="checkbox" checked={onlySelector} onChange={e=>setOnlySelector(e.target.checked)}/>只静默这一对象（{chosen.selector}）</label>:null}
    <div className="field"><label htmlFor="silence-hours">时长（小时，最多 168）</label><input id="silence-hours" type="number" min={1} max={168} value={hours} onChange={e=>setHours(Number(e.target.value))}/></div>
    <Reason id="silence-reason" value={reason} onChange={setReason}/>
    <p className="note">静默期间仍会评估和记录；“告警系统不可用”不能静默。</p>
  </Form>;
}

export const GOVERNED_SLOTS:Slots={
  alerts:()=><WithMember>{member=><SilenceForm member={member}/>}</WithMember>,
  goals:()=><WithMember>{member=><ControlForm member={member}/>}</WithMember>,
  consumers:route=>route.sub==='conversation'&&route.id?<ConversationControls conversationId={route.id}/>:route.id?<ConsumerControls consumerId={route.id}/>:null,
  plans:()=><WithMember>{member=><ReevaluateForm member={member}/>}</WithMember>,
  actions:()=><WithMember>{member=><EffectQueryForm member={member}/>}</WithMember>,
  commitments:route=>route.id?<CommitmentControls commitmentId={route.id}/>:null,
  // contact: release is entered from one restricted customer's row on the page itself (F06), no page-level form.
};
