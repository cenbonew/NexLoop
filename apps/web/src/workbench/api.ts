/**
 * NX-028 slice 1: owner workbench read client and page-state model (AT-045).
 * Every read is GET /api/v1/workbench/* on the workbench's own login realm (cookie __Host-nexloop_workbench).
 * A response that does not have the expected shape is rejected ("invalid"), never shown half-parsed; a block the
 * server could not provide is "forbidden" or "unavailable", never rendered as zero or "none".
 */
export type FailureKind='unauthenticated'|'forbidden'|'not_found'|'invalid_request'|'unavailable'|'invalid';
export class WorkbenchError extends Error{constructor(readonly kind:FailureKind){super(kind);}}
export type SectionStatus='ok'|'forbidden'|'unavailable';
export type Section<T>={status:'ok';data:T}|{status:'forbidden'|'unavailable';data:null};
export type WorkbenchSession={authenticated:true;tenant_id:string;principal_id:string;restricted:boolean};

export const STATE_TEXT:Record<FailureKind|'loading'|'empty',string>={
  loading:'正在加载…',
  empty:'暂无记录。',
  unauthenticated:'会话已失效，请重新登录工作台。',
  forbidden:'当前账号没有查看此内容的权限（403）。',
  not_found:'没有找到该对象，可能已被删除或不属于当前租户。',
  invalid_request:'请求参数无效，请检查筛选条件。',
  unavailable:'服务暂时不可用（503）：数据未加载，这不代表没有数据。',
  invalid:'服务器响应格式不符，已拒绝显示，以免误导。',
};
export const SECTION_TEXT:Record<Exclude<SectionStatus,'ok'>,string>={
  forbidden:'无权限查看此区块。',
  unavailable:'此区块暂不可用：未加载，不代表数量为零。',
};
export const RESTRICTED_TEXT='原文需授权（缺少该消息的读取权限）';
/** Effect intent states; "unknown" is "待核对", never a red failure (design §9). */
export const INTENT_STATE_TEXT:Record<string,string>={accepted:'已受理',dispatching:'派发中',unknown:'待核对',fulfilled:'渠道已完成',failed:'失败',confirmed:'已确认'};

export function failureOf(status:number):FailureKind{
  if(status===401)return 'unauthenticated';
  if(status===403)return 'forbidden';
  if(status===404)return 'not_found';
  if(status===422)return 'invalid_request';
  return 'unavailable';
}
export function kindOf(error:unknown):FailureKind{return error instanceof WorkbenchError?error.kind:'unavailable';}

function object(value:unknown):Record<string,unknown>{if(!value||typeof value!=='object'||Array.isArray(value))throw new WorkbenchError('invalid');return value as Record<string,unknown>;}
function list(value:unknown):unknown[]{if(!Array.isArray(value))throw new WorkbenchError('invalid');return value;}
function text(value:unknown):string{if(typeof value!=='string')throw new WorkbenchError('invalid');return value;}
function maybeText(value:unknown):string|null{return value===null||value===undefined?null:text(value);}
function count(value:unknown):number{if(!Number.isSafeInteger(value)||Number(value)<0)throw new WorkbenchError('invalid');return Number(value);}
function flag(value:unknown):boolean{if(typeof value!=='boolean')throw new WorkbenchError('invalid');return value;}
const hex64=/^[a-f0-9]{64}$/;
function id(value:unknown):string{const v=text(value);if(!hex64.test(v))throw new WorkbenchError('invalid');return v;}

async function get<T>(path:string,validate:(value:unknown)=>T):Promise<T>{
  let response:Response;
  try{response=await fetch(path,{credentials:'same-origin',cache:'no-store'});}catch{throw new WorkbenchError('unavailable');}
  if(!response.ok)throw new WorkbenchError(failureOf(response.status));
  let body:unknown;
  try{body=await response.json();}catch{throw new WorkbenchError('invalid');}
  try{return validate(body);}catch{throw new WorkbenchError('invalid');}
}

// ---- auth (own realm) ----
function session(value:unknown):WorkbenchSession{const v=object(value);if(v.authenticated!==true)throw new WorkbenchError('invalid');
  return {authenticated:true,tenant_id:text(v.tenant_id),principal_id:text(v.principal_id),restricted:flag(v.restricted)};}
export async function readWorkbenchSession():Promise<WorkbenchSession|null>{
  try{return await get('/api/v1/workbench/auth/session',session);}catch(error){if(kindOf(error)==='unauthenticated')return null;throw error;}
}
export async function workbenchLogin(username:string,password:string):Promise<WorkbenchSession>{
  const response=await fetch('/api/v1/workbench/auth/login',{method:'POST',credentials:'same-origin',cache:'no-store',headers:{'Content-Type':'application/json'},body:JSON.stringify({username,password})});
  if(!response.ok)throw new WorkbenchError(failureOf(response.status));
  return session(await response.json());
}
export async function workbenchLogout():Promise<void>{
  const csrf=await fetch('/api/v1/workbench/auth/csrf',{method:'POST',credentials:'same-origin',cache:'no-store'});
  if(!csrf.ok)throw new WorkbenchError(failureOf(csrf.status));
  const token=text(object(await csrf.json()).csrf_token);
  const response=await fetch('/api/v1/workbench/auth/logout',{method:'POST',credentials:'same-origin',cache:'no-store',headers:{'X-CSRF-Token':token}});
  if(!response.ok)throw new WorkbenchError(failureOf(response.status));
}

// ---- overview ----
export type Overview={goals:Section<{goals:{goal_id:string;goal_kind:string;objective:string;current_version:number;key_results:number}[];control_revision:number;paused_scopes:{scope_kind:string;scope_ref:string;reason:string}[]}>;
  commitments:Section<{open:number;breached:number;exceptions:{reason:string;count:number}[]}>;actions:Section<{count:number;oldest_age_seconds:number|null}>;
  contact:Section<{restricted:number;escalations:number}>;takeovers:Section<unknown>;backlog:Section<unknown>;commercial:Section<unknown>};
export const OVERVIEW_SECTIONS=['goals','commitments','actions','contact','takeovers','backlog','commercial'] as const;
export function section<T>(value:unknown,data:(value:unknown)=>T):Section<T>{
  const v=object(value);
  if(v.status==='ok')return {status:'ok',data:data(v.data)};
  if((v.status==='forbidden'||v.status==='unavailable')&&v.data===null)return {status:v.status,data:null};
  throw new WorkbenchError('invalid');
}
export function overview(value:unknown):Overview{
  const v=object(value);
  if(Object.keys(v).sort().join()!==[...OVERVIEW_SECTIONS].sort().join())throw new WorkbenchError('invalid');
  return {
    goals:section(v.goals,d=>{const g=object(d);return {goals:list(g.goals).map(x=>{const o=object(x);return {goal_id:text(o.goal_id),goal_kind:text(o.goal_kind),objective:text(o.objective),current_version:count(o.current_version),key_results:count(o.key_results)};}),
      control_revision:count(g.control_revision),paused_scopes:list(g.paused_scopes).map(x=>{const o=object(x);return {scope_kind:text(o.scope_kind),scope_ref:text(o.scope_ref),reason:text(o.reason)};})};}),
    commitments:section(v.commitments,d=>{const c=object(d);return {open:count(c.open),breached:count(c.breached),exceptions:list(c.exceptions).map(x=>{const o=object(x);return {reason:text(o.reason),count:count(o.count)};})};}),
    actions:section(v.actions,d=>{const a=object(d);return {count:count(a.count),oldest_age_seconds:a.oldest_age_seconds===null?null:count(a.oldest_age_seconds)};}),
    contact:section(v.contact,d=>{const c=object(d);return {restricted:count(c.restricted),escalations:count(c.escalations)};}),
    takeovers:section(v.takeovers,d=>d),backlog:section(v.backlog,d=>d),commercial:section(v.commercial,d=>d),
  };
}
export const readOverview=()=>get('/api/v1/workbench/overview',overview);

// ---- goals ----
export type Goal={goal_id:string;goal_kind:string;parent_goal_id:string|null;objective:string;current_version:number;priority:number;period_start:string;period_end:string;
  key_results:{kr_key:string;metric_id:string;metric_version:number;target:string;direction:string}[]};
export type Goals={goals:Goal[];control:{revision:number;scopes:{scope_kind:string;scope_ref:string;paused:boolean;revision:number;reason:string}[];
  events:{revision:number;event_kind:string;scope_kind:string;scope_ref:string;principal_id:string;recorded_at:string}[]};budgets:{budget_kind:string;unit:string;limit_amount:string;consumed:string}[]};
export function goals(value:unknown):Goals{
  const v=object(value);const c=object(v.control);
  return {goals:list(v.goals).map(x=>{const o=object(x);return {goal_id:text(o.goal_id),goal_kind:text(o.goal_kind),parent_goal_id:maybeText(o.parent_goal_id),objective:text(o.objective),
      current_version:count(o.current_version),priority:count(o.priority),period_start:text(o.period_start),period_end:text(o.period_end),
      key_results:list(o.key_results).map(k=>{const r=object(k);return {kr_key:text(r.kr_key),metric_id:text(r.metric_id),metric_version:count(r.metric_version),target:text(r.target),direction:text(r.direction)};})};}),
    control:{revision:count(c.revision),scopes:list(c.scopes).map(x=>{const o=object(x);return {scope_kind:text(o.scope_kind),scope_ref:text(o.scope_ref),paused:flag(o.paused),revision:count(o.revision),reason:text(o.reason)};}),
      events:list(c.events).map(x=>{const o=object(x);return {revision:count(o.revision),event_kind:text(o.event_kind),scope_kind:text(o.scope_kind),scope_ref:text(o.scope_ref),principal_id:text(o.principal_id),recorded_at:text(o.recorded_at)};})},
    budgets:list(v.budgets).map(x=>{const o=object(x);return {budget_kind:text(o.budget_kind),unit:text(o.unit),limit_amount:text(o.limit_amount),consumed:text(o.consumed)};})};
}
export const readGoals=()=>get('/api/v1/workbench/goals',goals);

// ---- consumers ----
export type ConsumerRow={consumer_id:string;restricted:boolean;open_commitments:number;active_plans:number;conversations:number};
export type ConsumerPage={items:ConsumerRow[];next_cursor:string|null};
export function consumers(value:unknown):ConsumerPage{
  const v=object(value);
  return {items:list(v.items).map(x=>{const o=object(x);return {consumer_id:id(o.consumer_id),restricted:flag(o.restricted),open_commitments:count(o.open_commitments),active_plans:count(o.active_plans),conversations:count(o.conversations)};}),
    next_cursor:v.next_cursor===null?null:id(v.next_cursor)};
}
export const readConsumers=(after?:string)=>get('/api/v1/workbench/consumers'+(after?'?after='+encodeURIComponent(after):''),consumers);
export type ConsumerDetail={consumer_id:string;paused:boolean;restriction:{active:boolean;control_revision:number;rule_id:string;restricted_at:string;released_at:string|null}|null;
  conversations:{conversation_id:string;last_sequence:number}[];commitments:string[];properties:{status:'ok';values:Record<string,unknown>}|{status:'forbidden';values:null}};
export function consumer(value:unknown):ConsumerDetail{
  const v=object(value);const p=object(v.properties);
  const restriction=v.restriction===null?null:(()=>{const r=object(v.restriction);return {active:flag(r.active),control_revision:count(r.control_revision),rule_id:text(r.rule_id),restricted_at:text(r.restricted_at),released_at:maybeText(r.released_at)};})();
  const properties=p.status==='ok'?{status:'ok' as const,values:object(p.values)}:p.status==='forbidden'&&p.values===null?{status:'forbidden' as const,values:null}:(()=>{throw new WorkbenchError('invalid');})();
  return {consumer_id:id(v.consumer_id),paused:flag(v.paused),restriction,conversations:list(v.conversations).map(x=>{const o=object(x);return {conversation_id:id(o.conversation_id),last_sequence:count(o.last_sequence)};}),
    commitments:list(v.commitments).map(id),properties};
}
export const readConsumer=(consumerId:string)=>get('/api/v1/workbench/consumers/'+consumerId,consumer);

// ---- conversation (NX-051 projection; content only with Message READ) ----
export type StaffMessage={id:string;sequence:number;accepted_at:string;direction:'inbound'|'outbound';sender_kind:string;reply_to_message_id:string|null;
  provider:{namespace:string;trust:string;sequence:number|null;skewed:boolean}|null;content:{status:'ok';actor:string|null;body:string|null}|{status:'restricted'}};
export type ConversationView={conversation_id:string;consumer_id:string;messages:StaffMessage[]};
export function conversation(value:unknown):ConversationView{
  const v=object(value);
  return {conversation_id:id(v.conversation_id),consumer_id:id(v.consumer_id),messages:list(v.messages).map(x=>{
    const o=object(x);const c=object(o.content);
    if(o.direction!=='inbound'&&o.direction!=='outbound')throw new WorkbenchError('invalid');
    const provider=o.provider===null||o.provider===undefined?null:(()=>{const p=object(o.provider);return {namespace:text(p.namespace),trust:text(p.trust),sequence:p.sequence===null?null:count(p.sequence),skewed:flag(p.skewed)};})();
    const content=c.status==='ok'?{status:'ok' as const,actor:maybeText(c.actor),body:maybeText(c.body)}:c.status==='restricted'?{status:'restricted' as const}:(()=>{throw new WorkbenchError('invalid');})();
    return {id:id(o.id),sequence:count(o.sequence),accepted_at:text(o.accepted_at),direction:o.direction,sender_kind:text(o.sender_kind),
      reply_to_message_id:o.reply_to_message_id===null||o.reply_to_message_id===undefined?null:id(o.reply_to_message_id),provider,content};
  })};
}
export const readConversation=(conversationId:string)=>get('/api/v1/workbench/conversations/'+conversationId,conversation);

// ---- plans ----
export type Plan={plan_id:string;version:number;consumer_id:string;goal_version_ref:string;status:string;created_at:string;steps:{step_key:string;expected_result:string;reassess_at:string|null}[];outcomes:{kind:string;recorded_at:string}[]};
export function plans(value:unknown):Plan[]{
  return list(object(value).plans).map(x=>{const o=object(x);return {plan_id:text(o.plan_id),version:count(o.version),consumer_id:id(o.consumer_id),goal_version_ref:text(o.goal_version_ref),
    status:text(o.status),created_at:text(o.created_at),steps:list(o.steps).map(s=>{const t=object(s);return {step_key:text(t.step_key),expected_result:text(t.expected_result),reassess_at:maybeText(t.reassess_at)};}),
    outcomes:list(o.outcomes).map(s=>{const t=object(s);return {kind:text(t.kind),recorded_at:text(t.recorded_at)};})};});
}
export const readPlans=(consumerId?:string)=>get('/api/v1/workbench/plans'+(consumerId?'?consumer_id='+consumerId:''),plans);

// ---- actions ----
export type Dispatch={status:'ok';prediction:{dispatchable:boolean;reason:string|null}}|{status:'unavailable'};
export type Intent={intent_id:string;consumer_id:string;action_name:string;action_version:number;state:string;created_at:string;age_seconds:number;attempts:number;
  observation:{provider_state:string;observed_at:string}|null;dispatch:Dispatch};
export type Actions={intents:Intent[];unknown:{count:number;oldest_age_seconds:number|null}};
export function actions(value:unknown):Actions{
  const v=object(value);const u=object(v.unknown);
  return {intents:list(v.intents).map(x=>{const o=object(x);const d=object(o.dispatch);
    const dispatch:Dispatch=d.status==='ok'?(()=>{const p=object(d.prediction);return {status:'ok' as const,prediction:{dispatchable:flag(p.dispatchable),reason:maybeText(p.reason)}};})():d.status==='unavailable'?{status:'unavailable'}:(()=>{throw new WorkbenchError('invalid');})();
    return {intent_id:text(o.intent_id),consumer_id:text(o.consumer_id),action_name:text(o.action_name),action_version:count(o.action_version),state:text(o.state),created_at:text(o.created_at),
      age_seconds:count(o.age_seconds),attempts:count(o.attempts),observation:o.observation===null?null:(()=>{const b=object(o.observation);return {provider_state:text(b.provider_state),observed_at:text(b.observed_at)};})(),dispatch};}),
    unknown:{count:count(u.count),oldest_age_seconds:u.oldest_age_seconds===null?null:count(u.oldest_age_seconds)}};
}
export const readActions=(state?:string)=>get('/api/v1/workbench/actions'+(state?'?state='+encodeURIComponent(state):''),actions);

// ---- commitments (commitment-view contract) ----
export type EvidenceItem={kind:string;ref:string;occurred_at:string};
export type CommitmentView={commitment_id:string;consumer_id:string;revision:number;status:string;properties:Record<string,unknown>;quote:string|null;
  quote_status:'ok'|'restricted'|'unavailable'|'none';origin:string;evidence:{requested:EvidenceItem[];delivered:EvidenceItem[];customer_confirmed:EvidenceItem[];problem_resolved:'unavailable'};
  events:{kind:string;recorded_at:string;principal_id:string|null}[];exceptions:{reason:string;raised_at:string}[]};
const QUOTE_STATUS=['ok','restricted','unavailable','none'];
function evidence(value:unknown):EvidenceItem[]{return list(value).map(x=>{const o=object(x);return {kind:text(o.kind),ref:text(o.ref),occurred_at:text(o.occurred_at)};});}
export function commitmentView(value:unknown):CommitmentView{
  const v=object(value);const p=object(v.properties);const e=object(v.evidence);
  if(!QUOTE_STATUS.includes(String(v.quote_status))||e.problem_resolved!=='unavailable')throw new WorkbenchError('invalid');
  return {commitment_id:id(v.commitment_id),consumer_id:id(v.consumer_id),revision:count(v.revision),status:text(p.status),properties:p,quote:maybeText(v.quote),
    quote_status:v.quote_status as CommitmentView['quote_status'],origin:text(v.origin),
    evidence:{requested:evidence(e.requested),delivered:evidence(e.delivered),customer_confirmed:evidence(e.customer_confirmed),problem_resolved:'unavailable'},
    events:list(v.events).map(x=>{const o=object(x);return {kind:text(o.kind),recorded_at:text(o.recorded_at),principal_id:maybeText(o.principal_id)};}),
    exceptions:list(v.exceptions).map(x=>{const o=object(x);return {reason:text(o.reason),raised_at:text(o.raised_at)};})};
}
export type Commitments={commitments:CommitmentView[];exceptions:{subject_ref:string;reason:string;raised_at:string}[]};
export function commitmentList(value:unknown):Commitments{
  const v=object(value);
  return {commitments:list(v.commitments).map(commitmentView),exceptions:list(v.exceptions).map(x=>{const o=object(x);return {subject_ref:text(o.subject_ref),reason:text(o.reason),raised_at:text(o.raised_at)};})};
}
export const readCommitments=(all=false)=>get('/api/v1/workbench/commitments'+(all?'?all=true':''),commitmentList);
export const readCommitment=(commitmentId:string)=>get('/api/v1/workbench/commitments/'+commitmentId,commitmentView);
/** Open vs ended (design §6). Status comes only from the object; the interface never infers it. */
export const OPEN_STATUSES=['conditional','open','in_progress','breached'];
export const COMMITMENT_STATUS_TEXT:Record<string,string>={conditional:'有条件',open:'未履行',in_progress:'处理中',breached:'已违约',fulfilled:'已履行',cancelled:'已取消',superseded:'已被取代'};

// ---- contact (contact-restriction-view contract) ----
export type Hit={message_id:string;rule_id:string;certainty:string;matched_text:string|null;matched_text_status:'ok'|'restricted';recorded_at:string};
export type Restriction=Hit&{consumer_id:string;active:boolean;control_revision:number;conversation_id:string;restricted_at:string;released_at:string|null;release_reason:string|null;hits:Hit[]};
export type Contact={restrictions:Restriction[];escalations:{message_id:string;consumer_id:string;reason:string;escalated_at:string}[]};
function hit(value:unknown):Hit{
  const o=object(value);
  if(o.matched_text_status!=='ok'&&o.matched_text_status!=='restricted')throw new WorkbenchError('invalid');
  if(o.matched_text_status==='restricted'&&o.matched_text!==null)throw new WorkbenchError('invalid');
  return {message_id:id(o.message_id),rule_id:text(o.rule_id),certainty:text(o.certainty),matched_text:maybeText(o.matched_text),matched_text_status:o.matched_text_status,recorded_at:text(o.recorded_at??o.restricted_at)};
}
export function contact(value:unknown):Contact{
  const v=object(value);
  return {restrictions:list(v.restrictions).map(x=>{const o=object(x);return {...hit(o),consumer_id:id(o.consumer_id),active:flag(o.active),control_revision:count(o.control_revision),
      conversation_id:id(o.conversation_id),restricted_at:text(o.restricted_at),released_at:maybeText(o.released_at),release_reason:maybeText(o.release_reason),hits:list(o.hits).map(hit)};}),
    escalations:list(v.escalations).map(x=>{const o=object(x);return {message_id:id(o.message_id),consumer_id:id(o.consumer_id),reason:text(o.reason),escalated_at:text(o.escalated_at)};})};
}
export const readContact=(all=false)=>get('/api/v1/workbench/contact'+(all?'?all=true':''),contact);
export function matchedText(item:Hit):string{return item.matched_text_status==='ok'&&item.matched_text?item.matched_text:`已命中规则 ${item.rule_id}（${RESTRICTED_TEXT}）`;}

// ---- settings (owner) ----
export type Settings={application_id:string;manifest_version:number;roles_version:number;members:{principal_id:string;role:string}[];roles:Record<string,string[]>};
export function settings(value:unknown):Settings{
  const v=object(value);const r=object(v.roles);
  return {application_id:text(v.application_id),manifest_version:count(v.manifest_version),roles_version:count(v.roles_version),
    members:list(v.members).map(x=>{const o=object(x);return {principal_id:text(o.principal_id),role:text(o.role)};}),
    roles:Object.fromEntries(Object.entries(r).map(([k,a])=>[k,list(a).map(text)]))};
}
export const readSettings=()=>get('/api/v1/workbench/settings',settings);
