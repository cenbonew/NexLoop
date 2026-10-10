/**
 * NX-028 page integration: the governed write controls of slices 2/3 in each page's action slot.
 * A control is offered only when the member's role carries its Action (D2, readMe); the governed entry still decides every
 * write with the caller's current grants (403 is shown as such). After a write every workbench read is refetched, so the next
 * read shows the result. A 503 or unreadable response means the result is unknown: it is said so, and a retry reuses the same
 * Idempotency-Key (the governed entry answers a replay from its terminal outcome, nothing runs twice).
 */
import {useRef,useState,type FormEvent,type ReactNode} from 'react';
import {useMutation,useQuery,useQueryClient,type QueryClient} from '@tanstack/react-query';
import {COMMITMENT_STATUS_TEXT,OPEN_STATUSES,STATE_TEXT,UNKNOWN_RESULT,WRITE_TEXT,kindOf,newRequestKey,offers,readActions,readCommitment,readConsumer,readContact,
  readConversation,readMe,readPlans,readTakeovers,takeoverOf,workbenchAct,writeKindOf,readAlerts,type Me,type Operation,type Takeover} from './api';
import type {PageKey,Route} from './route';
import {Short} from './pages/Page';

export type SlotValue=ReactNode|((route:Route)=>ReactNode);
export type Slots=Partial<Record<PageKey,SlotValue>>;

/** Success text per operation: what happened, and what it does not mean. */
export const DONE_TEXT:Record<Operation,string>={
  set_control:'已提交；派发预判与计划状态已刷新。恢复不会重放暂停期间被拒的意图。',
  release_contact_restriction:'已解除；之前被拒的意图不会重放，计划将重新评估。',
  cancel_commitment:'已取消承诺。',extend_commitment:'已延期：原承诺已被新承诺取代。',attest_commitment:'已记录人工证明。',
  commitment_condition_met:'已记录条件满足。',mark_commitment_communication:'已标记为沟通类承诺。',
  request_plan_reevaluation:'已请求复评；是否启动复评 Run 仍由预检决定。',
  request_effect_query:'已请求查询执行结果；不会重发，也不会换新的幂等键。',
  take_over_conversation:'已接管：接管期间 Agent 不回复，顾客看到“人工客服处理中”。',
  hand_back_conversation:'已交还：只有最新一条未回复的来信恢复待回复，更早的记为接管期间已处理；计划将重新评估。',
  send_staff_reply:'已发送给顾客；不代表问题已解决或承诺已兑现。',
  silence_alert:'已静默：期间仍会评估和记录，只是不再提示；到期自动结束。',
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

function useWrite(operation:Operation){
  const cache=useQueryClient();const pending=useRef<{body:string;key:string}|null>(null);
  return useMutation({mutationFn:(body:Record<string,unknown>)=>governedWrite(cache,operation,body,pending)});
}

function Outcome({operation,write}:{operation:Operation;write:ReturnType<typeof useWrite>}){
  if(write.isPending)return <p role="status">正在提交…</p>;
  if(write.isSuccess)return <p className="note" role="status">{DONE_TEXT[operation]}</p>;
  if(write.isError){const kind=writeKindOf(write.error);
    return <p className={UNKNOWN_RESULT.includes(kind)?'pending':'error'} role="alert">{WRITE_TEXT[kind]}</p>;}
  return null;
}

function Reason({id,value,onChange,label='原因'}:{id:string;value:string;onChange:(v:string)=>void;label?:string}){
  return <div className="field"><label htmlFor={id}>{label}</label><textarea id={id} value={value} onChange={e=>onChange(e.target.value)} required maxLength={500} rows={2}/></div>;
}

/** A form for one operation: shown only when the role carries it. */
function Form({operation,title,member,children,body,disabled}:{operation:Operation;title:string;member:Me;children:ReactNode;body:()=>Record<string,unknown>|null;disabled?:boolean}){
  const write=useWrite(operation);
  if(!offers(member,operation))return null;
  function submit(event:FormEvent){event.preventDefault();const b=body();if(b)write.mutate(b);}
  return <form className="workbench-action" aria-label={title} onSubmit={submit}><h2>{title}</h2>{children}
    <button type="submit" disabled={disabled||write.isPending}>{write.isError&&UNKNOWN_RESULT.includes(writeKindOf(write.error))?'用同一请求重试':title}</button>
    <Outcome operation={operation} write={write}/></form>;
}

/** The member's role decides which controls are offered; without it nothing is guessed. */
function WithMember({children}:{children:(member:Me)=>ReactNode}){
  const member=useQuery({queryKey:['workbench','me'],queryFn:readMe,refetchOnWindowFocus:false});
  if(member.isPending)return <p role="status">{STATE_TEXT.loading}</p>;
  if(member.isError)return <p className="note" role="status">操作区暂不可用：{STATE_TEXT[kindOf(member.error)]}</p>;
  return <>{children(member.data)}</>;
}

function utc(local:string):string|null{const d=new Date(local);return Number.isNaN(d.getTime())?null:d.toISOString();}

// ---- pause / resume (owner, Control.set) ----
const SCOPES={tenant:'整个租户',consumer:'客户',role:'角色',strategy:'策略',action_type:'Action 类型'} as const;
export function ControlForm({member,scopeKind,scopeRef,paused}:{member:Me;scopeKind?:keyof typeof SCOPES;scopeRef?:string;paused?:boolean}){
  const [kind,setKind]=useState<keyof typeof SCOPES>(scopeKind??'tenant');const [ref,setRef]=useState(scopeRef??'*');
  const [pause,setPause]=useState(!paused);const [reason,setReason]=useState('');
  const fixed=scopeKind!==undefined;
  return <Form operation="set_control" title={fixed?(paused?'恢复主动联系':'暂停主动联系'):'暂停 / 恢复'} member={member}
    body={()=>({scope_kind:kind,scope_ref:ref,paused:fixed?!paused:pause,reason})}>
    {fixed?<p>范围：{SCOPES[kind]} {kind==='consumer'?<Short value={ref}/>:ref}</p>:<>
      <div className="field"><label htmlFor="control-scope">范围</label><select id="control-scope" value={kind} onChange={e=>{const k=e.target.value as keyof typeof SCOPES;setKind(k);setRef(k==='tenant'?'*':'');}}>
        {Object.entries(SCOPES).map(([k,v])=><option key={k} value={k}>{v}</option>)}</select></div>
      {kind!=='tenant'?<div className="field"><label htmlFor="control-ref">对象</label><input id="control-ref" value={ref} onChange={e=>setRef(e.target.value)} required maxLength={255}/></div>:null}
      <fieldset><legend>操作</legend><label><input type="radio" name="control-paused" checked={pause} onChange={()=>setPause(true)}/> 暂停</label>
        <label><input type="radio" name="control-paused" checked={!pause} onChange={()=>setPause(false)}/> 恢复</label></fieldset></>}
    <Reason id="control-reason" value={reason} onChange={setReason}/>
  </Form>;
}

// ---- contact restriction release (owner only, ADR-023 §2.4) ----
export function ReleaseForm({member,consumerId}:{member:Me;consumerId?:string}){
  const contact=useQuery({queryKey:['workbench','contact',false],queryFn:()=>readContact(false),enabled:!consumerId&&offers(member,'release_contact_restriction')});
  const active=consumerId?[consumerId]:(contact.data?.restrictions.filter(r=>r.active).map(r=>r.consumer_id)??[]);
  const [chosen,setChosen]=useState('');const [reason,setReason]=useState('');
  const target=consumerId??(chosen||active[0]||'');
  return <Form operation="release_contact_restriction" title="解除联系限制" member={member} disabled={!target} body={()=>target?{consumer_id:target,reason}:null}>
    {consumerId?null:active.length?<div className="field"><label htmlFor="release-consumer">客户</label><select id="release-consumer" value={target} onChange={e=>setChosen(e.target.value)}>
      {active.map(c=><option key={c} value={c}>{c.slice(0,12)}…</option>)}</select></div>:<p className="note">没有受限客户。</p>}
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
  const [reason,setReason]=useState('');const [due,setDue]=useState('');const [occurred,setOccurred]=useState('');
  return <WithMember>{member=>{
    if(!view.data)return null;
    if(!OPEN_STATUSES.includes(view.data.status))return <p className="note">承诺已结束（{COMMITMENT_STATUS_TEXT[view.data.status]??view.data.status}），不能再执行操作。</p>;
    const base={commitment_id:commitmentId};
    return <>
      <Reason id="commitment-reason" value={reason} onChange={setReason} label="原因（各项操作共用）"/>
      {view.data.status==='conditional'?<Form operation="commitment_condition_met" title="条件已满足" member={member} body={()=>({...base,reason})}>{null}</Form>:null}
      <Form operation="attest_commitment" title="人工证明已兑现" member={member} body={()=>{const at=utc(occurred);return at?{...base,occurred_at:at,reason}:null;}}>
        <div className="field"><label htmlFor="attest-at">兑现时间</label><input id="attest-at" type="datetime-local" value={occurred} onChange={e=>setOccurred(e.target.value)} required/></div></Form>
      <Form operation="extend_commitment" title="延期" member={member} body={()=>{const at=utc(due);return at?{...base,due_at:at,reason}:null;}}>
        <div className="field"><label htmlFor="extend-due">新期限</label><input id="extend-due" type="datetime-local" value={due} onChange={e=>setDue(e.target.value)} required/></div></Form>
      <Form operation="mark_commitment_communication" title="标记为沟通类承诺" member={member} body={()=>({...base,reason})}>{null}</Form>
      <Form operation="cancel_commitment" title="取消承诺" member={member} body={()=>({...base,reason})}>{null}</Form>
    </>;}}</WithMember>;
}

// ---- manual plan reevaluation / effect result query (D7) ----
export function ReevaluateForm({member}:{member:Me}){
  const plans=useQuery({queryKey:['workbench','plans',''],queryFn:()=>readPlans(),enabled:offers(member,'request_plan_reevaluation')});
  const [chosen,setChosen]=useState('');const [reason,setReason]=useState('');
  const ids=plans.data?.map(p=>p.plan_id)??[];const target=chosen||ids[0]||'';
  return <Form operation="request_plan_reevaluation" title="手动复评" member={member} disabled={!target} body={()=>target?{plan_id:target,reason}:null}>
    {ids.length?<div className="field"><label htmlFor="reevaluate-plan">计划</label><select id="reevaluate-plan" value={target} onChange={e=>setChosen(e.target.value)}>
      {plans.data!.map(p=><option key={p.plan_id} value={p.plan_id}>{p.plan_id.slice(0,8)}… v{p.version}（{p.status}）</option>)}</select></div>:<p className="note">暂无计划。</p>}
    <Reason id="reevaluate-reason" value={reason} onChange={setReason}/>
  </Form>;
}

export function EffectQueryForm({member}:{member:Me}){
  const unknown=useQuery({queryKey:['workbench','actions','unknown'],queryFn:()=>readActions('unknown'),enabled:offers(member,'request_effect_query')});
  const [chosen,setChosen]=useState('');const [reason,setReason]=useState('');
  const ids=unknown.data?.intents.map(i=>i.intent_id)??[];const target=chosen||ids[0]||'';
  return <Form operation="request_effect_query" title="查询执行结果" member={member} disabled={!target} body={()=>target?{intent_id:target,reason}:null}>
    <p className="note">对“待核对”的意图向渠道查询结果；不是重发。</p>
    {ids.length?<div className="field"><label htmlFor="query-intent">意图</label><select id="query-intent" value={target} onChange={e=>setChosen(e.target.value)}>
      {unknown.data!.intents.map(i=><option key={i.intent_id} value={i.intent_id}>{i.intent_id.slice(0,8)}… {i.action_name}</option>)}</select></div>:<p className="note">没有待核对的意图。</p>}
    <Reason id="query-reason" value={reason} onChange={setReason}/>
  </Form>;
}

// ---- takeover / hand-back / staff reply (slice 3, D3–D6) ----
export function takeoverText(t:Takeover):string{
  return `人工接管中（${t.scope_kind==='consumer'?'整个客户':'本会话'}）：${t.mine?'由我':'由 '+t.taken_by}接管，到期 ${t.expires_at}`;
}

function TakeoverControls({member,scopeKind,scopeRef,consumerId,conversationId}:{member:Me;scopeKind:'conversation'|'consumer';scopeRef:string;consumerId:string;conversationId?:string}){
  const takeovers=useQuery({queryKey:['workbench','takeovers'],queryFn:readTakeovers});
  const [minutes,setMinutes]=useState<number|null>(null);const [reason,setReason]=useState('');
  if(takeovers.isPending)return <p role="status">{STATE_TEXT.loading}</p>;
  if(takeovers.isError)return <p className="note" role="status">接管状态暂不可用：{STATE_TEXT[kindOf(takeovers.error)]}</p>;
  const {policy,takeovers:list}=takeovers.data;
  const current=conversationId?takeoverOf(list,conversationId,consumerId):list.find(t=>t.consumer_id===consumerId)??null;
  if(current)return <>
    <p className="warning" role="status">{takeoverText(current)}</p>
    <Form operation="hand_back_conversation" title="交还给 Agent" member={member} body={()=>({takeover_id:current.takeover_id,reason})}>
      <Reason id="handback-reason" value={reason} onChange={setReason}/></Form>
  </>;
  const value=minutes??Math.round(policy.default_seconds/60);
  return <Form operation="take_over_conversation" title={scopeKind==='consumer'?'接管整个客户':'接管本会话'} member={member}
    body={()=>({scope_kind:scopeKind,scope_ref:scopeRef,duration_seconds:value*60,reason})}>
    <div className="field"><label htmlFor="takeover-minutes">时长（分钟，最长 {Math.round(policy.max_seconds/60)}）</label>
      <input id="takeover-minutes" type="number" min={1} max={Math.round(policy.max_seconds/60)} value={value} onChange={e=>setMinutes(Number(e.target.value))} required/></div>
    <Reason id="takeover-reason" value={reason} onChange={setReason}/>
  </Form>;
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
  return <Form operation="silence_alert" title="静默告警" member={member} disabled={!chosen||!reason.trim()}
    body={()=>chosen?{rule_id:chosen.rule_id,selector:onlySelector?chosen.selector:null,until:silenceUntil(hours),reason}:null}>
    {options.length?<div className="field"><label htmlFor="silence-alert">告警</label><select id="silence-alert" value={chosen?.dedupe_key} onChange={e=>setKey(e.target.value)}>
      {options.map(a=><option key={a.dedupe_key} value={a.dedupe_key}>{a.rule_id}{a.selector?` · ${a.selector}`:''}</option>)}</select></div>
      :<p className="note">当前没有可静默的触发中告警。</p>}
    {chosen?.selector?<label><input type="checkbox" checked={onlySelector} onChange={e=>setOnlySelector(e.target.checked)}/> 只静默这一对象（{chosen.selector}）</label>:null}
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
  contact:()=><WithMember>{member=><ReleaseForm member={member}/>}</WithMember>,
};
