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
  contact:Section<{restricted:number;escalations:number}>;takeovers:Section<Takeover[]>;backlog:Section<Backlog>;commercial:Section<Commercial>};
/** NX-030 M06/M07 (overview block): queues, work feeds and the extraction feed from the SQL metrics snapshot; counts and ages only. */
export type Backlog={queues:Record<string,Record<string,number|null>>;feeds:Record<string,Record<string,number|null>>;extraction:{pending_messages:number;oldest_pending_seconds:number|null}};
export function backlog(value:unknown):Backlog{const v=object(value);const ex=object(v.extraction);
  return {queues:nested(v.queues),feeds:nested(v.feeds),extraction:{pending_messages:count(ex.pending_messages),oldest_pending_seconds:maybeCount(ex.oldest_pending_seconds)}};}
/** Overview “商业事件与费用” block. The backend serves it unavailable today; when wired it carries the NX-030 snapshot cost
 * shape (budgets and the last 24 h of NX-027 cost entries): codes, decimal strings and counts only. */
export type Commercial={budgets:Record<string,{limit:string;unit:string;consumed:string;ratio:number|null}>;
  last_24h:{cost_kind:string;data_mode:string;currency:string;amount:string;entries:number;unpriced:number}[]};
function decimalText(value:unknown):string{const t=text(value);if(!/^-?\d+(\.\d+)?$/.test(t))throw new WorkbenchError('invalid');return t;}
export function commercial(value:unknown):Commercial{const v=object(value);
  return {budgets:Object.fromEntries(Object.entries(object(v.budgets)).map(([k,x])=>{const b=object(x);
      const ratio=b.ratio===null?null:Number(decimalText(typeof b.ratio==='number'?String(b.ratio):b.ratio));
      return [k,{limit:decimalText(b.limit),unit:text(b.unit),consumed:decimalText(b.consumed),ratio}];})),
    last_24h:list(v.last_24h).map(x=>{const o=object(x);return {cost_kind:text(o.cost_kind),data_mode:text(o.data_mode),currency:text(o.currency),amount:decimalText(o.amount),
      entries:count(o.entries),unpriced:count(o.unpriced)};})};}
export const ageText=(seconds:number|null|undefined)=>seconds===null||seconds===undefined?'—':seconds<120?`${seconds} 秒`:seconds<7200?`${Math.round(seconds/60)} 分钟`:`${Math.round(seconds/3600)} 小时`;
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
    takeovers:section(v.takeovers,d=>takeovers(d).takeovers),backlog:section(v.backlog,backlog),commercial:section(v.commercial,commercial),
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
/** The customer's name state (walkthrough F04): one vocabulary on every page. `restricted` (no Consumer read, or the owner restricted the
 * name) and `missing` (readable, not recorded) show the stable alias; `failed` says the read failed and may be retried. A restriction
 * never says whether a name exists. Older servers' withheld / forbidden / absent map to the same states. */
export type DisplayName={status:'ok';name:string}|{status:'restricted';reason:'forbidden'|'withheld'}|{status:'missing'}|{status:'failed'};
export function displayName(value:unknown):DisplayName{
  if(value===undefined)return {status:'missing'};
  const o=object(value);
  if(o.status==='ok')return {status:'ok',name:text(o.name)};
  if(o.status==='restricted'&&(o.reason==='forbidden'||o.reason==='withheld'))return {status:'restricted',reason:o.reason};
  if(o.status==='missing'||o.status==='failed')return {status:o.status};
  if(o.status==='withheld'||o.status==='forbidden')return {status:'restricted',reason:o.status};
  if(o.status==='absent')return {status:'missing'};
  throw new WorkbenchError('invalid');
}
/** A short alias derived from the opaque id: stable, not personal data, and not the full id. */
export function consumerAlias(consumerId:string):string{return `客户 ${consumerId.slice(0,4)}·${consumerId.slice(-3)}`;}
export const DISPLAY_NOTE:Record<'restricted'|'missing'|'failed',string>={restricted:'名称受限',missing:'未登记名称',failed:'名称加载失败'};
/** The readable label of a customer: the name, or the alias with its state (for titles, filters and plain text). */
export function consumerLabel(consumerId:string,display:DisplayName):string{
  return display.status==='ok'?display.name:`${consumerAlias(consumerId)}（${DISPLAY_NOTE[display.status]}）`;
}
export type ConsumerRow={consumer_id:string;restricted:boolean;open_commitments:number;active_plans:number;conversations:number;display:DisplayName};
export type ConsumerPage={items:ConsumerRow[];next_cursor:string|null;total:number|null;tenant_total:number|null};
const total=(value:unknown)=>value===undefined||value===null?null:count(value);
export function consumers(value:unknown):ConsumerPage{
  const v=object(value);
  return {items:list(v.items).map(x=>{const o=object(x);return {consumer_id:id(o.consumer_id),restricted:flag(o.restricted),open_commitments:count(o.open_commitments),active_plans:count(o.active_plans),conversations:count(o.conversations),display:displayName(o.display)};}),
    next_cursor:v.next_cursor===null||v.next_cursor===undefined?null:/^\d+$/.test(String(v.next_cursor))?String(v.next_cursor):id(v.next_cursor),total:total(v.total),tenant_total:total(v.tenant_total)};
}
/** F09: list filters and sort (server side, on the list's own counts); `cursor` is the server's offset or, for the plain list, the id cursor. */
export type ConsumerQuery={restricted?:boolean;has_open_commitments?:boolean;sort?:'id'|'open_commitments'|'conversations'|'active_plans';order?:'asc'|'desc';cursor?:string};
export function consumerQueryString(q:ConsumerQuery,limit=50):string{
  const p=new URLSearchParams();
  if(q.restricted!==undefined)p.set('restricted',String(q.restricted));
  if(q.has_open_commitments!==undefined)p.set('has_open_commitments',String(q.has_open_commitments));
  p.set('sort',q.sort??'id');if(q.order)p.set('order',q.order);if(q.cursor)p.set('cursor',q.cursor);p.set('limit',String(limit));
  return p.toString();
}
export const readConsumers=(q:ConsumerQuery={},limit=50)=>get('/api/v1/workbench/consumers?'+consumerQueryString(q,limit),consumers);
export type ConsumerDetail={consumer_id:string;paused:boolean;restriction:{active:boolean;control_revision:number;rule_id:string;restricted_at:string;released_at:string|null}|null;
  display:DisplayName;conversations:{conversation_id:string;last_sequence:number;last_message_at:string|null}[];commitments:string[];properties:{status:'ok';values:Record<string,unknown>;withheld:string[]}|{status:'forbidden';values:null;withheld:string[]}};
export function consumer(value:unknown):ConsumerDetail{
  const v=object(value);const p=object(v.properties);
  const restriction=v.restriction===null?null:(()=>{const r=object(v.restriction);return {active:flag(r.active),control_revision:count(r.control_revision),rule_id:text(r.rule_id),restricted_at:text(r.restricted_at),released_at:maybeText(r.released_at)};})();
  // ADR-025: owner-restricted property groups are listed by name as withheld, never shown.
  const withheld=list(p.withheld??[]).map(text);
  const properties=p.status==='ok'?{status:'ok' as const,values:object(p.values),withheld}:p.status==='forbidden'&&p.values===null?{status:'forbidden' as const,values:null,withheld}:(()=>{throw new WorkbenchError('invalid');})();
  return {consumer_id:id(v.consumer_id),paused:flag(v.paused),restriction,display:displayName(v.display),conversations:list(v.conversations).map(x=>{const o=object(x);return {conversation_id:id(o.conversation_id),last_sequence:count(o.last_sequence),last_message_at:o.last_message_at===undefined?null:maybeText(o.last_message_at)};}),
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

// ---- the customer's recent exchanges across conversations (owner feedback 2026-10-18) ----
export type HistoryItem={id:string;conversation_id:string;sequence:number;accepted_at:string;direction:'inbound'|'outbound';sender_kind:string;
  content:{status:'ok';actor:string|null;body:string|null}|{status:'restricted'}};
export type ConsumerHistory={consumer_id:string;conversations:number;items:HistoryItem[];next_cursor:string|null};
export function consumerHistory(value:unknown):ConsumerHistory{
  const v=object(value);
  return {consumer_id:id(v.consumer_id),conversations:count(v.conversations),next_cursor:maybeText(v.next_cursor),items:list(v.items).map(x=>{
    const o=object(x);const c=object(o.content);
    if(o.direction!=='inbound'&&o.direction!=='outbound')throw new WorkbenchError('invalid');
    const content=c.status==='ok'?{status:'ok' as const,actor:maybeText(c.actor),body:maybeText(c.body)}:c.status==='restricted'?{status:'restricted' as const}:(()=>{throw new WorkbenchError('invalid');})();
    return {id:id(o.id),conversation_id:id(o.conversation_id),sequence:count(o.sequence),accepted_at:text(o.accepted_at),direction:o.direction,sender_kind:text(o.sender_kind),content};})};
}
export const HISTORY_PAGE=10;
export const readConsumerHistory=(consumerId:string,before?:string)=>
  get('/api/v1/workbench/consumers/'+consumerId+'/history?limit='+HISTORY_PAGE+(before?'&before='+encodeURIComponent(before):''),consumerHistory);

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
// ---- remediation intents (NX-057 R1, remediation_intents) ----
export type Remediation={remediation_id:string;source_intent_id:string;source_action:string;source_action_version:number;consumer_id:string|null;
  failure_kind:string;failed_at:string;rule_id:string;rule_version:number;kind:string;mode:string;state:string;due_by:string;attempts:number;
  last_code:string|null;created_at:string;plan_id:string|null};
export type HeldRule={rule_id:string;reason_code:string;observed:number;held_at:string};
export type Remediations={remediations:Remediation[];hidden:number;paused:boolean;switch_revision:number;rate_capped:{hour:string;over_count:number}[];
  held_rules:HeldRule[]};
export function remediations(value:unknown):Remediations{
  const v=object(value);
  return {remediations:list(v.remediations).map(x=>{const o=object(x);
    return {remediation_id:text(o.remediation_id),source_intent_id:text(o.source_intent_id),source_action:text(o.source_action),
      source_action_version:count(o.source_action_version),consumer_id:maybeText(o.consumer_id),failure_kind:text(o.failure_kind),failed_at:text(o.failed_at),
      rule_id:text(o.rule_id),rule_version:count(o.rule_version),kind:text(o.kind),mode:text(o.mode),state:text(o.state),due_by:text(o.due_by),
      attempts:count(o.attempts),last_code:maybeText(o.last_code),created_at:text(o.created_at),plan_id:maybeText(o.plan_id)};}),
    hidden:count(v.hidden),paused:flag(v.paused),switch_revision:count(v.switch_revision),
    rate_capped:list(v.rate_capped).map(x=>{const o=object(x);return {hour:text(o.hour),over_count:count(o.over_count)};}),
    held_rules:list(v.held_rules).map(x=>{const o=object(x);return {rule_id:text(o.rule_id),reason_code:text(o.reason_code),observed:count(o.observed),held_at:text(o.held_at)};})};
}
/** NX-057 R4: one remediation's evidence chain (ids, codes and times only; the Consumer may be withheld). */
export type RemediationEvidence={remediation:{remediation_id:string;kind:string;mode:string;state:string;last_code:string|null;due_by:string;created_at:string;
    origin_remediation_id:string|null};
  source_intent:{intent_id:string;action_name:string;action_version:number;state:string;created_at:string;consumer_id:string|null};
  failure:{failure_id:string;reason_code:string;recorded_at:string;evidence:Record<string,string>};
  claim:{state:string;status:string|null;finalized_at:string|null}|null;
  rule:{rule_id:string;manifest_version:number;digest:string};
  plan:{plan_id:string;woken_at:string|null;consumed_by_run:string|null}|null;
  events:{from_state:string|null;to_state:string;code:string;actor_kind:string;recorded_at:string}[]};
export function remediationEvidence(value:unknown):RemediationEvidence{
  const v=object(value);const q=object(v.remediation);const i=object(v.source_intent);const f=object(v.failure);const r=object(v.rule);
  const evidence=object(f.evidence);
  return {remediation:{remediation_id:text(q.remediation_id),kind:text(q.kind),mode:text(q.mode),state:text(q.state),last_code:maybeText(q.last_code),
      due_by:text(q.due_by),created_at:text(q.created_at),origin_remediation_id:maybeText(q.origin_remediation_id)},
    source_intent:{intent_id:text(i.intent_id),action_name:text(i.action_name),action_version:count(i.action_version),state:text(i.state),
      created_at:text(i.created_at),consumer_id:maybeText(i.consumer_id)},
    failure:{failure_id:text(f.failure_id),reason_code:text(f.reason_code),recorded_at:text(f.recorded_at),
      evidence:Object.fromEntries(Object.entries(evidence).map(([k,x])=>[k,String(x)]))},
    claim:v.claim===null?null:(()=>{const c=object(v.claim);return {state:text(c.state),status:maybeText(c.status),finalized_at:maybeText(c.finalized_at)};})(),
    rule:{rule_id:text(r.rule_id),manifest_version:count(r.manifest_version),digest:text(r.digest)},
    plan:v.plan===null?null:(()=>{const p=object(v.plan);return {plan_id:text(p.plan_id),woken_at:maybeText(p.woken_at),consumed_by_run:maybeText(p.consumed_by_run)};})(),
    events:list(v.events).map(x=>{const e=object(x);return {from_state:maybeText(e.from_state),to_state:text(e.to_state),code:text(e.code),
      actor_kind:text(e.actor_kind),recorded_at:text(e.recorded_at)};})};
}
export const readRemediationEvidence=(id:string)=>get('/api/v1/workbench/remediations/'+encodeURIComponent(id),remediationEvidence);
export const readRemediations=(state?:string)=>get('/api/v1/workbench/remediations'+(state?'?state='+encodeURIComponent(state):''),remediations);
/** One customer's most recent governed Actions (orders, deliveries, queries), for the customer summary. */
export const readConsumerActions=(consumerId:string,limit=5)=>get('/api/v1/workbench/actions?consumer_id='+consumerId+'&limit='+limit,actions);

// ---- commitments (commitment-view contract) ----
export type EvidenceItem={kind:string;ref:string;occurred_at:string};
export type CommitmentView={commitment_id:string;consumer_id:string;revision:number;status:string;properties:Record<string,unknown>;quote:string|null;
  quote_status:'ok'|'restricted'|'unavailable'|'none';origin:string;evidence:{requested:EvidenceItem[];delivered:EvidenceItem[];customer_confirmed:EvidenceItem[];problem_resolved:'unavailable'};
  events:{kind:string;recorded_at:string;principal_id:string|null}[];exceptions:{reason:string;raised_at:string}[];consumer_display:DisplayName|null};
const QUOTE_STATUS=['ok','restricted','unavailable','none'];
function evidence(value:unknown):EvidenceItem[]{return list(value).map(x=>{const o=object(x);return {kind:text(o.kind),ref:text(o.ref),occurred_at:text(o.occurred_at)};});}
export function commitmentView(value:unknown):CommitmentView{
  const v=object(value);const p=object(v.properties);const e=object(v.evidence);
  if(!QUOTE_STATUS.includes(String(v.quote_status))||e.problem_resolved!=='unavailable')throw new WorkbenchError('invalid');
  return {commitment_id:id(v.commitment_id),consumer_id:id(v.consumer_id),revision:count(v.revision),status:text(p.status),properties:p,quote:maybeText(v.quote),
    quote_status:v.quote_status as CommitmentView['quote_status'],origin:text(v.origin),
    evidence:{requested:evidence(e.requested),delivered:evidence(e.delivered),customer_confirmed:evidence(e.customer_confirmed),problem_resolved:'unavailable'},
    events:list(v.events).map(x=>{const o=object(x);return {kind:text(o.kind),recorded_at:text(o.recorded_at),principal_id:maybeText(o.principal_id)};}),
    exceptions:list(v.exceptions).map(x=>{const o=object(x);return {reason:text(o.reason),raised_at:text(o.raised_at)};}),
    // D3 (optional in commitment-view): absent from older servers, then the page shows the alias without a state.
    consumer_display:v.consumer_display===undefined?null:displayName(v.consumer_display)};
}
export type CommitmentCounts={open:number;ended:number;exceptions_in_scope:number;exceptions_all:number};
export type CommitmentException={subject_ref:string;consumer_id:string|null;reason:string;raised_at:string};
export type Commitments={commitments:CommitmentView[];exceptions:CommitmentException[];counts:CommitmentCounts|null;total:number|null;next_cursor:string|null};
export function commitmentList(value:unknown):Commitments{
  const v=object(value);const c=v.counts===undefined?null:object(v.counts);
  return {commitments:list(v.commitments).map(commitmentView),
    exceptions:list(v.exceptions).map(x=>{const o=object(x);return {subject_ref:text(o.subject_ref),consumer_id:o.consumer_id===undefined||o.consumer_id===null?null:id(o.consumer_id),reason:text(o.reason),raised_at:text(o.raised_at)};}),
    counts:c?{open:count(c.open),ended:count(c.ended),exceptions_in_scope:count(c.exceptions_in_scope),exceptions_all:count(c.exceptions_all)}:null,
    total:total(v.total),next_cursor:v.next_cursor===null||v.next_cursor===undefined?null:/^\d+$/.test(String(v.next_cursor))?String(v.next_cursor):(()=>{throw new WorkbenchError('invalid');})()};
}
/** F05/F09: the scope of the commitment list; list, counts and exceptions always come from the same scope (server side). */
export type CommitmentQuery={status_group?:'open'|'ended'|'all';consumer_id?:string;late?:boolean;due_after?:string;due_before?:string;
  sort?:'due_at'|'registered_at'|'status';order?:'asc'|'desc';cursor?:string};
export function commitmentQueryString(q:CommitmentQuery,limit=50):string{
  const p=new URLSearchParams();p.set('status_group',q.status_group??'open');
  for(const k of ['consumer_id','due_after','due_before','sort','order','cursor'] as const){const x=q[k];if(x)p.set(k,x);}
  if(q.late!==undefined)p.set('late',String(q.late));p.set('limit',String(limit));
  return p.toString();
}
export const readCommitments=(q:CommitmentQuery={},limit=50)=>get('/api/v1/workbench/commitments?'+commitmentQueryString(q,limit),commitmentList);
export const readConsumerCommitments=(consumerId:string)=>get('/api/v1/workbench/commitments?'+commitmentQueryString({status_group:'all',consumer_id:consumerId},200),commitmentList);
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
/** NX-058: the owner's members view - labels, usernames, roles, statuses, the vote cooldown's end; unexpired invitations; the last 90
 * days of reset codes. Never a token, code, digest or email (the server leaves them out; a row that carried one is refused here). */
export type MemberStatus='invited'|'active'|'suspended'|'revoked';
export type MemberRow={principal_id:string;label:string;username:string|null;role:string|null;member_status:MemberStatus;account_status:string|null;
  vote_eligible_from:string|null;erased:boolean};
export type InvitationRow={invitation_id:string;username:string;role:string;issued_by:string;issued_at:string;expires_at:string};
export type ResetRow={reset_id:string;target_principal:string;issued_by:string;issued_at:string;expires_at:string;state:string;completed_at:string|null;kind:'member'|'owner'};
export type Members={members:MemberRow[];invitations:InvitationRow[];resets:ResetRow[]};
const exactKeys=(o:Record<string,unknown>,keys:string[])=>{if(Object.keys(o).sort().join()!==[...keys].sort().join())throw new WorkbenchError('invalid');return o;};
const memberStatus=(v:unknown):MemberStatus=>{const t=text(v);if(!['invited','active','suspended','revoked'].includes(t))throw new WorkbenchError('invalid');return t as MemberStatus;};
export function members(value:unknown):Members{const v=exactKeys(object(value),['members','invitations','resets']);
  return {members:list(v.members).map(x=>{const o=exactKeys(object(x),['principal_id','label','username','role','member_status','account_status','vote_eligible_from','erased']);
      const label=text(o.label);if(!/^p-[0-9a-f]{8}$/.test(label))throw new WorkbenchError('invalid');
      return {principal_id:text(o.principal_id),label,username:maybeText(o.username),role:maybeText(o.role),member_status:memberStatus(o.member_status),
        account_status:maybeText(o.account_status),vote_eligible_from:maybeText(o.vote_eligible_from),erased:flag(o.erased)};}),
    invitations:list(v.invitations).map(x=>{const o=exactKeys(object(x),['invitation_id','username','role','issued_by','issued_at','expires_at']);
      return {invitation_id:text(o.invitation_id),username:text(o.username),role:text(o.role),issued_by:text(o.issued_by),issued_at:text(o.issued_at),expires_at:text(o.expires_at)};}),
    resets:list(v.resets).map(x=>{const o=exactKeys(object(x),['reset_id','target_principal','issued_by','issued_at','expires_at','state','completed_at','kind']);
      const kind=text(o.kind);if(kind!=='member'&&kind!=='owner')throw new WorkbenchError('invalid');
      return {reset_id:text(o.reset_id),target_principal:text(o.target_principal),issued_by:text(o.issued_by),issued_at:text(o.issued_at),expires_at:text(o.expires_at),
        state:text(o.state),completed_at:maybeText(o.completed_at),kind};})};}
export const readMembers=()=>get('/api/v1/workbench/members',members);
/** A one-time secret in a write's result (a reset code or an invitation token): shown once to the issuer, never stored by the page. */
export function oneTimeSecret(result:Record<string,unknown>|undefined):{kind:'code'|'token';value:string}|null{
  if(!result)return null;
  if(typeof result.code==='string'&&result.code.length>=16)return {kind:'code',value:result.code};
  if(typeof result.token==='string'&&result.token.length>=16)return {kind:'token',value:result.token};
  return null;
}

// ---- member-read audit (ADR-025, owner only) ----
export type AuditRow={audit_id:number;principal_id:string;role:string;object_kind:'message'|'consumer';target_resource:string;read_purpose:string;read_at:string};
export function audit(value:unknown):AuditRow[]{
  return list(object(value).items).map(x=>{const o=object(x);if(o.object_kind!=='message'&&o.object_kind!=='consumer')throw new WorkbenchError('invalid');
    return {audit_id:count(o.audit_id),principal_id:text(o.principal_id),role:text(o.role),object_kind:o.object_kind,target_resource:text(o.target_resource),read_purpose:text(o.read_purpose),read_at:text(o.read_at)};});
}
export const readAudit=()=>get('/api/v1/workbench/audit',audit);

// ---- reviewer evidence (NX-028 known limitation; ADR-025 reviewer derivation) ----
const uuidPattern=/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
function candidateId(value:unknown):string{const v=text(value);if(!uuidPattern.test(v))throw new WorkbenchError('invalid');return v;}
export type ReviewItem={candidate_id:string;kind:string;display_name:string|null;created_at:string;evidence_count:number};
export function reviewQueue(value:unknown):ReviewItem[]{
  return list(object(value).items).map(x=>{const o=object(x);return {candidate_id:candidateId(o.candidate_id),kind:text(o.kind),display_name:maybeText(o.display_name),created_at:text(o.created_at),evidence_count:count(o.evidence_count)};});
}
export type ReviewEvidence={candidate_id:string;status:'ended'}|{candidate_id:string;status:'pending_review';kind:string;display_name:string|null;
  evidence:{claim_id:string;predicate:string|null;source_message_id:string|null;content:{status:'ok';body:string|null}|{status:'restricted'}}[]};
export function reviewEvidence(value:unknown):ReviewEvidence{
  const v=object(value);
  if(v.status==='ended'){if(list(v.evidence).length)throw new WorkbenchError('invalid');return {candidate_id:candidateId(v.candidate_id),status:'ended'};}
  if(v.status!=='pending_review')throw new WorkbenchError('invalid');
  return {candidate_id:candidateId(v.candidate_id),status:'pending_review',kind:text(v.kind),display_name:maybeText(v.display_name),evidence:list(v.evidence).map(x=>{
    const o=object(x);const c=object(o.content);
    const content=c.status==='ok'?{status:'ok' as const,body:maybeText(c.body)}:c.status==='restricted'?{status:'restricted' as const}:(()=>{throw new WorkbenchError('invalid');})();
    return {claim_id:text(o.claim_id),predicate:maybeText(o.predicate),source_message_id:o.source_message_id===null||o.source_message_id===undefined?null:id(o.source_message_id),content};})};
}
export const readReviewQueue=()=>get('/api/v1/workbench/review',reviewQueue);
export const readReviewEvidence=(candidate:string)=>get('/api/v1/workbench/review/'+candidate,reviewEvidence);
export const REVIEW_ENDED_TEXT='审核已结束，证据原文不再可读。';

// ---- me: the member's role (NX-028 page integration, 0160; display only, every write is decided by SQL) ----
export type Role='owner'|'operator'|'reviewer';
export type Me={principal_id:string;role:Role;actions:string[]};
export function me(value:unknown):Me{
  const v=object(value);
  if(v.role!=='owner'&&v.role!=='operator'&&v.role!=='reviewer')throw new WorkbenchError('invalid');
  return {principal_id:text(v.principal_id),role:v.role,actions:list(v.actions).map(text)};
}
export const readMe=()=>get('/api/v1/workbench/me',me);

// ---- takeovers in effect (slice 3, 0126/0160) ----
export type Takeover={takeover_id:string;scope_kind:'conversation'|'consumer';scope_ref:string;consumer_id:string;taken_by:string;mine:boolean;reason:string;started_at:string;expires_at:string};
export type Takeovers={takeovers:Takeover[];policy:{default_seconds:number;max_seconds:number}};
const uuid=/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
export function takeovers(value:unknown):Takeovers{
  const v=object(value);
  if(v.status!=='ok')throw new WorkbenchError('unavailable');
  const p=object(v.policy);
  return {policy:{default_seconds:count(p.default_seconds),max_seconds:count(p.max_seconds)},takeovers:list(v.takeovers).map(x=>{const o=object(x);
    if(o.scope_kind!=='conversation'&&o.scope_kind!=='consumer')throw new WorkbenchError('invalid');
    const takeover_id=text(o.takeover_id);if(!uuid.test(takeover_id))throw new WorkbenchError('invalid');
    return {takeover_id,scope_kind:o.scope_kind,scope_ref:id(o.scope_ref),consumer_id:id(o.consumer_id),taken_by:text(o.taken_by),mine:flag(o.mine),reason:text(o.reason),
      started_at:text(o.started_at),expires_at:text(o.expires_at)};})};
}
export const readTakeovers=()=>get('/api/v1/workbench/takeovers',takeovers);
/** The takeover covering a conversation: the conversation itself or its whole consumer (the same rule dispatch uses). */
export function takeoverOf(list:Takeover[],conversationId:string,consumerId:string):Takeover|null{
  return list.find(t=>(t.scope_kind==='conversation'&&t.scope_ref===conversationId)||(t.scope_kind==='consumer'&&t.scope_ref===consumerId))??null;
}

// ---- governed writes: POST /api/v1/workbench/actions/{operation} (slice 2/3) ----
/** Operation → the Action resource a role must carry to be offered the control (workbench-roles; SQL still decides). */
export const OPERATION_ACTION={
  set_control:'eios:action:Control.set:1',release_contact_restriction:'eios:action:Contact.release:1',
  cancel_commitment:'eios:action:Commitment.cancel:1',extend_commitment:'eios:action:Commitment.extend:1',attest_commitment:'eios:action:Commitment.attest:1',
  commitment_condition_met:'eios:action:Commitment.condition_met:1',mark_commitment_communication:'eios:action:Commitment.mark_communication:1',
  request_plan_reevaluation:'eios:action:nexloop.plan.request_reevaluation:1',request_effect_query:'eios:action:nexloop.service.query_request:1',
  take_over_conversation:'eios:action:nexloop.conversation.takeover:1',hand_back_conversation:'eios:action:nexloop.conversation.handback:1',
  send_staff_reply:'eios:action:nexloop.message.staff_send:1',
  // NX-030: owner only (workbench-roles v4).
  silence_alert:'eios:action:nexloop.alert.silence:1',
  // 0220: owner only; the real dispatch switches (tenant and Action layers).
  set_real_dispatch:'eios:action:nexloop.real_dispatch.set:1',
  // 0227/0228: operator persona and communication preferences (owner only).
  publish_operator_persona:'eios:action:nexloop.operator_persona.publish:1',assign_operator_persona:'eios:action:nexloop.operator_persona.assign:1',
  publish_persona_rules:'eios:action:nexloop.operator_persona.rules_publish:1',set_ai_disclosure:'eios:action:nexloop.ai_disclosure.set:1',
  dismiss_contact_preference:'eios:action:nexloop.contact_preference.dismiss:1',
  // 0232: the tenant's outreach controls (owner only).
  set_outreach_controls:'eios:action:nexloop.outreach_controls.set:1',
  // Outreach slice 3: enable or disable a purpose of the purpose list (owner only).
  set_outreach_purpose:'eios:action:nexloop.outreach_purposes.set:1',
  // Outreach slice 6: approve or reject a proactive send waiting for approval (never the requester's own).
  decide_outreach_approval:'eios:action:nexloop.outreach.approve:1',
  // NX-039 V4 (D39-8): publish a semantic release from an evaluated candidate, or roll back to an earlier version (owner only).
  publish_semantic_release:'eios:action:nexloop.semantic_release.publish:1',rollback_semantic_release:'eios:action:nexloop.semantic_release.rollback:1',
  // NX-039 V6: the release scope's stop switch for one evolution dimension (owner only).
  set_evolution_stop:'eios:action:nexloop.semantic_release.stop:1',
  // NX-056: approve or reject another person's request (never one's own), or withdraw one's own waiting request.
  decide_approval:'eios:action:nexloop.approvals.decide:1',cancel_approval:'eios:action:nexloop.approvals.cancel:1',
  // NX-056 A7: the owner's single-person mode switch (with a reason).
  set_approval_solo_mode:'eios:action:nexloop.approvals.solo_mode.set:1',
  // NX-056 A5: an owner-started erasure, executed from the approvals page after approval (owner only). A hold release is not a
  // workbench operation: it is executed through the client it was requested from.
  erase_consumer:'eios:action:nexloop.consumer.erase:1',
  // NX-057 R1 (remediation_intents): resolve an open remediation intent, and pause or resume the tenant's remediation (owner only).
  resolve_remediation:'eios:action:nexloop.remediation.resolve:1',switch_remediation:'eios:action:nexloop.remediation.switch:1',
  // NX-057 R2 (remediation_wake): release a rule the breaker held (owner only).
  release_remediation_rule:'eios:action:nexloop.remediation.rule_release:1',
  // Extraction dead letters (0355): requeue or dismiss one dead-lettered claim-extraction job (owner only).
  resolve_extraction_dead_letter:'eios:action:nexloop.claim_extraction.dead_letter_resolve:1',
  // NX-058 (owner only): workbench members. Operators and reviewers directly; owner targets, owner invitations, owner reset codes and the
  // erasure of a departed member under NX-056 approval (executed from the approvals page after another owner approves).
  invite_workbench_member:'eios:action:nexloop.workbench_member.invite:1',revoke_workbench_invitation:'eios:action:nexloop.workbench_member.revoke_invitation:1',
  issue_workbench_reset:'eios:action:nexloop.workbench_member.issue_reset:1',change_workbench_member_role:'eios:action:nexloop.workbench_member.change_role:1',
  suspend_workbench_member:'eios:action:nexloop.workbench_member.suspend:1',resume_workbench_member:'eios:action:nexloop.workbench_member.resume:1',
  revoke_workbench_member:'eios:action:nexloop.workbench_member.revoke:1',promote_workbench_member:'eios:action:nexloop.workbench_member.promote_owner:1',
  demote_workbench_owner:'eios:action:nexloop.workbench_member.demote_owner:1',suspend_workbench_owner:'eios:action:nexloop.workbench_member.suspend_owner:1',
  resume_workbench_owner:'eios:action:nexloop.workbench_member.resume_owner:1',revoke_workbench_owner:'eios:action:nexloop.workbench_member.revoke_owner:1',
  issue_workbench_owner_reset:'eios:action:nexloop.workbench_member.issue_owner_reset:1',invite_workbench_owner:'eios:action:nexloop.workbench_member.invite_owner:1',
  erase_workbench_member:'eios:action:nexloop.workbench_member.erase_member:1',
} as const;
export type Operation=keyof typeof OPERATION_ACTION;
export function offers(member:Me|undefined,operation:Operation):boolean{return !!member&&member.actions.includes(OPERATION_ACTION[operation]);}
/** The knowledge pages (NX-028 D2 revision 2026-10-18): the reviewer decides (ontology.schema.review); the owner reads only
 * (nexloop.knowledge.read, 0213). The server decides every read and decision; this only chooses what to offer. */
export const REVIEW_ACTION='eios:action:ontology.schema.review:1';
export const KNOWLEDGE_READ_ACTION='eios:action:nexloop.knowledge.read:1';
export function reviews(member:Me|undefined):boolean{return !!member&&member.actions.includes(REVIEW_ACTION);}
export function readsKnowledgeOnly(member:Me|undefined):boolean{return !!member&&!reviews(member)&&member.actions.includes(KNOWLEDGE_READ_ACTION);}

export type WriteFailure=FailureKind|'not_allowed_in_state'|'conflict'|'request_timeout'|'not_sent';
export class WorkbenchWriteError extends Error{constructor(readonly kind:WriteFailure){super(kind);}}
/** Exact outcome text (AT-045). 503/network/invalid response: the result is not known, never shown as failed or done. */
export const WRITE_TEXT:Record<WriteFailure,string>={
  unauthenticated:'会话已失效，请重新登录工作台；操作未提交。',
  forbidden:'当前账号无权执行此操作（403），未执行。',
  not_found:'本租户未发布此操作或对象不存在（404），未执行。',
  invalid_request:'填写内容无效（422），未执行。',
  not_allowed_in_state:'当前状态不允许此操作（409），未执行；请查看最新状态。',
  conflict:'与同时进行的其他操作冲突（409），未执行；请刷新后重试。',
  request_timeout:'请求超时（408），未提交。',
  not_sent:'暂时无法连接服务，操作未提交；请稍后重试。',
  unavailable:'服务暂时不可用（503），无法确认是否已执行。请稍后刷新查看结果；重试使用同一请求号，不会重复执行。',
  invalid:'已提交，但服务器响应格式不符，结果未确认；请刷新查看。',
};
/** Unknown result: keep the same Idempotency-Key for a retry (the governed entry answers a replay from its terminal outcome). */
export const UNKNOWN_RESULT:WriteFailure[]=['unavailable','invalid'];
export function writeFailureOf(status:number,code:unknown):WriteFailure{
  if(status===409)return code==='conflict'?'conflict':'not_allowed_in_state';
  if(status===408)return 'request_timeout';
  return failureOf(status);
}
export function newRequestKey():string{
  const bytes=new Uint8Array(16);crypto.getRandomValues(bytes);
  return 'wb-'+Array.from(bytes,b=>b.toString(16).padStart(2,'0')).join('');
}
export async function workbenchAct(operation:Operation,body:Record<string,unknown>,key:string):Promise<Record<string,unknown>>{
  let token:string;
  try{
    const csrf=await fetch('/api/v1/workbench/auth/csrf',{method:'POST',credentials:'same-origin',cache:'no-store'});
    if(!csrf.ok)throw new WorkbenchWriteError(csrf.status===401?'unauthenticated':'not_sent');
    token=text(object(await csrf.json()).csrf_token);
  }catch(error){if(error instanceof WorkbenchWriteError)throw error;throw new WorkbenchWriteError('not_sent');}
  let response:Response;
  try{response=await fetch('/api/v1/workbench/actions/'+operation,{method:'POST',credentials:'same-origin',cache:'no-store',
    headers:{'Content-Type':'application/json','X-CSRF-Token':token,'Idempotency-Key':key},body:JSON.stringify(body)});}
  catch{throw new WorkbenchWriteError('unavailable');}
  let value:unknown;
  try{value=await response.json();}catch{throw new WorkbenchWriteError(response.ok?'invalid':writeFailureOf(response.status,null));}
  if(!response.ok)throw new WorkbenchWriteError(writeFailureOf(response.status,value&&typeof value==='object'?(value as Record<string,unknown>).code:null));
  try{return object(value);}catch{throw new WorkbenchWriteError('invalid');}
}
export function writeKindOf(error:unknown):WriteFailure{return error instanceof WorkbenchWriteError?error.kind:'unavailable';}


// ---- NX-030: alerts, operations status (metrics snapshot) and the owner's audit ----
export type Severity='warning'|'critical';
export type FiringAlert={dedupe_key:string;rule_id:string;severity:Severity;selector:string|null;value:string|null;first_fired_at:string;last_fired_at:string;fire_count:number;silenced_until:string|null};
export type AlertEvent={event_id:number;rule_id:string;severity:Severity;kind:'firing'|'resolved';selector:string|null;value:string|null;threshold:string|null;recorded_at:string};
export type Alerts={firing:FiringAlert[];events:AlertEvent[];silences:{rule_id:string;selector:string|null;until:string}[];evaluator:{last_evaluated_at:string|null;rules_version:number|null;stale:boolean}};
function severity(value:unknown):Severity{if(value!=='warning'&&value!=='critical')throw new WorkbenchError('invalid');return value;}
export function alerts(value:unknown):Alerts{
  const v=object(value);const e=object(v.evaluator);
  if(v.real_world_only!==true)throw new WorkbenchError('invalid');
  return {firing:list(v.firing).map(x=>{const o=object(x);return {dedupe_key:text(o.dedupe_key),rule_id:text(o.rule_id),severity:severity(o.severity),selector:maybeText(o.selector),
      value:maybeText(o.value),first_fired_at:text(o.first_fired_at),last_fired_at:text(o.last_fired_at),fire_count:count(o.fire_count),silenced_until:maybeText(o.silenced_until)};}),
    events:list(v.events).map(x=>{const o=object(x);if(o.kind!=='firing'&&o.kind!=='resolved')throw new WorkbenchError('invalid');
      return {event_id:count(o.event_id),rule_id:text(o.rule_id),severity:severity(o.severity),kind:o.kind,selector:maybeText(o.selector),value:maybeText(o.value),
        threshold:maybeText(o.threshold),recorded_at:text(o.recorded_at)};}),
    silences:list(v.silences??[]).map(x=>{const o=object(x);return {rule_id:text(o.rule_id),selector:maybeText(o.selector),until:text(o.until)};}),
    evaluator:{last_evaluated_at:maybeText(e.last_evaluated_at),rules_version:e.rules_version===null?null:count(e.rules_version),stale:flag(e.stale)}};
}
export const readAlerts=()=>get('/api/v1/workbench/alerts',alerts);
/** A silenced alert is still evaluated and recorded; the page only marks it. The evaluator's own staleness cannot be silenced. */
export const SEVERITY_TEXT:Record<Severity,string>={warning:'警告',critical:'严重'};
export const EVALUATOR_STALE_TEXT='告警系统不可用：评估器超过 5 分钟没有运行，下面的列表可能已过期（这一条不能静默）。';

/** The SQL metrics snapshot: codes, counts, ages and ratios only. Process sections may be unavailable (never shown as 0). */
export type Process={status:'unavailable'}|{status:'ok';by_service:Record<string,{sum:Record<string,number>;max:Record<string,number>}>;timeout_rate?:number;p95_ms_max?:number};
export type Metrics={refusals:{last_5m:Record<string,number>;last_hour:Record<string,number>};effects:{by_state:Record<string,number>;unknown:{count:number;oldest_age_seconds:number|null;over_24h:number}};
  replies:{pending:{count:number;oldest_age_seconds:number|null};escalations_last_hour:Record<string,number>;escalations_total:number};
  commitments:{exceptions:Record<string,number>;exceptions_last_hour:Record<string,number>};
  queues:Record<string,Record<string,number|null>>;feeds:Record<string,Record<string,number|null>>;extraction:{pending_messages:number;oldest_pending_seconds:number|null};
  guard:Process;host:Process;pool:Process;connections:{total:number};backup:Backup;computed_at:string;
  /** 0216 (absent from an older backend: null, shown as 不可用) */ candidates:Candidates|null;
  /** W4 0224 (absent: null) */ data_assets:DataAssets|null};
/** NX-035 M12 backup block (runtime.nexloop_backup_metrics): numbers and states only. A metric without an observation is absent
 * (null here) and is shown as “无”, never as 0; no PG backup recorded at all is status unavailable (AT-045). */
export const BACKUP_FIELDS=['age_seconds','full_backup_age_seconds','wal_last_archived_age_seconds','journal_export_lag_seconds','file_backup_age_seconds',
  'drill_age_seconds','drill_rpo_seconds','drill_rto_seconds'] as const;
export type BackupField=typeof BACKUP_FIELDS[number];
export type Backup={status:'unavailable'}|({status:'ok';verify_failed:number|null;wal_archive_failing:number|null;repo_backup_age_seconds:Record<string,number>}&Record<BackupField,number|null>);
function backup(value:unknown):Backup{
  const o=object(value);if(o.status==='unavailable')return {status:'unavailable'};if(o.status!=='ok')throw new WorkbenchError('invalid');
  const num=(x:unknown)=>{if(x===undefined||x===null)return null;if(typeof x!=='number'||!Number.isFinite(x)||x<0)throw new WorkbenchError('invalid');return x;};
  const repos=Object.fromEntries(Object.entries(object(o.repo_backup_age_seconds??{})).map(([k,v])=>{const n=num(v);if(n===null)throw new WorkbenchError('invalid');return [k,n];}));
  return {status:'ok',verify_failed:num(o.verify_failed),wal_archive_failing:num(o.wal_archive_failing),repo_backup_age_seconds:repos,
    ...Object.fromEntries(BACKUP_FIELDS.map(f=>[f,num(o[f])])) as Record<BackupField,number|null>};
}
export const BACKUP_UNAVAILABLE_TEXT='没有任何已记录的数据库备份：未加载，不代表没有备份。';
export const BACKUP_LABELS:Record<BackupField,string>={age_seconds:'最近一次已校验的数据库备份',full_backup_age_seconds:'最近一次完整备份',
  wal_last_archived_age_seconds:'最近一次 WAL 归档',journal_export_lag_seconds:'恢复日志导出延迟',file_backup_age_seconds:'最近一次文件快照',
  drill_age_seconds:'距上次通过的恢复演练',drill_rpo_seconds:'最近一次演练的 RPO',drill_rto_seconds:'最近一次演练的 RTO'};
function counts(value:unknown):Record<string,number>{const o=object(value);return Object.fromEntries(Object.entries(o).map(([k,v])=>[k,count(v)]));}
function maybeCount(value:unknown):number|null{return value===null||value===undefined?null:count(value);}
function nested(value:unknown):Record<string,Record<string,number|null>>{const o=object(value);return Object.fromEntries(Object.entries(o).map(([k,v])=>[k,Object.fromEntries(Object.entries(object(v)).map(([f,n])=>[f,maybeCount(n)]))]));}
function processSection(value:unknown):Process{
  const o=object(value);if(o.status==='unavailable')return {status:'unavailable'};if(o.status!=='ok')throw new WorkbenchError('invalid');
  const by=object(o.by_service);
  return {status:'ok',by_service:Object.fromEntries(Object.entries(by).map(([k,v])=>{const s=object(v);
      const nums=(x:unknown)=>Object.fromEntries(Object.entries(object(x??{})).map(([f,n])=>{if(typeof n!=='number'||!Number.isFinite(n))throw new WorkbenchError('invalid');return [f,n];}));
      return [k,{sum:nums(s.sum),max:nums(s.max)}];})),
    ...(typeof o.timeout_rate==='number'?{timeout_rate:o.timeout_rate}:{}),...(typeof o.p95_ms_max==='number'?{p95_ms_max:o.p95_ms_max}:{})};
}
// ---- W4 data assets (L6 0225) and the merge configuration state (0216): counts, profile ids and times only ----
export type Candidates={staged:number;merge_configuration_active:boolean;staged_without_configuration:number};
export type DataAssets={claims:{total:number;by_epistemic_kind:Record<string,number>;by_resolution_state:Record<string,number>};
  extraction_runs:{total:number;last_at:string|null};extraction_jobs:Record<string,number>;
  recall:{entries:Record<string,number>;profiles:{profile_id:string;dimension:number;created_at:string;vectors:number}[];
    backfills:{profile_id:string;runs:number;first_at:string|null;last_at:string|null;last_queued:number|null;last_definition_types:number|null}[]};
  embedding_budget:{status:'unavailable';reason:string}|{status:'ok';[key:string]:unknown}};
function candidates(value:unknown):Candidates{const o=object(value);
  return {staged:count(o.staged),merge_configuration_active:flag(o.merge_configuration_active),staged_without_configuration:count(o.staged_without_configuration)};}
function dataAssets(value:unknown):DataAssets{
  const o=object(value);const c=object(o.claims);const r=object(o.extraction_runs);const rc=object(o.recall);const b=object(o.embedding_budget);
  if(b.status!=='unavailable'&&b.status!=='ok')throw new WorkbenchError('invalid');
  return {claims:{total:count(c.total),by_epistemic_kind:counts(c.by_epistemic_kind),by_resolution_state:counts(c.by_resolution_state)},
    extraction_runs:{total:count(r.total),last_at:maybeText(r.last_at)},extraction_jobs:counts(o.extraction_jobs),
    recall:{entries:counts(rc.entries),profiles:list(rc.profiles).map(x=>{const p=object(x);return {profile_id:text(p.profile_id),dimension:count(p.dimension),created_at:text(p.created_at),vectors:count(p.vectors)};}),
      backfills:list(rc.backfills).map(x=>{const p=object(x);return {profile_id:text(p.profile_id),runs:count(p.runs),first_at:maybeText(p.first_at),last_at:maybeText(p.last_at),
        last_queued:maybeCount(p.last_queued),last_definition_types:maybeCount(p.last_definition_types)};})},
    embedding_budget:b.status==='unavailable'?{status:'unavailable',reason:text(b.reason)}:{...b,status:'ok'}};
}
export function metrics(value:unknown):Metrics{
  const v=object(value);const r=object(v.refusals);const ef=object(v.effects);const u=object(ef.unknown);const rp=object(v.replies);const pend=object(rp.pending);
  const cm=object(v.commitments);const ex=object(v.extraction);const cn=object(v.connections);
  return {refusals:{last_5m:counts(r.last_5m),last_hour:counts(r.last_hour)},
    effects:{by_state:counts(ef.by_state),unknown:{count:count(u.count),oldest_age_seconds:maybeCount(u.oldest_age_seconds),over_24h:count(u.over_24h)}},
    replies:{pending:{count:count(pend.count),oldest_age_seconds:maybeCount(pend.oldest_age_seconds)},escalations_last_hour:counts(rp.escalations_last_hour),escalations_total:count(rp.escalations_total)},
    commitments:{exceptions:counts(cm.exceptions),exceptions_last_hour:counts(cm.exceptions_last_hour)},
    queues:nested(v.queues),feeds:nested(v.feeds),extraction:{pending_messages:count(ex.pending_messages),oldest_pending_seconds:maybeCount(ex.oldest_pending_seconds)},
    guard:processSection(v.guard),host:processSection(v.host),pool:processSection(v.pool),connections:{total:count(cn.total)},backup:backup(v.backup),computed_at:text(v.computed_at),
    candidates:v.candidates===undefined?null:candidates(v.candidates),data_assets:v.data_assets===undefined?null:dataAssets(v.data_assets)};
}
export const readMetrics=()=>get('/api/v1/workbench/metrics',metrics);
/** The real dispatch switches from the same metrics read (states and revisions only, 0153). */
export const readRealDispatchSwitches=()=>get('/api/v1/workbench/metrics',value=>object(value).real_dispatch);
export const PROCESS_UNAVAILABLE_TEXT='最近 5 分钟没有样本：未加载，不代表为零。';
export const REFUSAL_TEXT:Record<string,string>={NXC01:'已暂停',NXC02:'控制已变更（需复评）',NXC03:'目标已改版',NXC04:'对象已变更',NXC05:'客户联系受限',NXC06:'人工接管中',
  NXB01:'预算耗尽',NXB02:'预算记账冲突',NXB03:'预算未配置',NXM01:'观测冲突'};

/** Owner-only audit: who did which governed human Action when, and ADR-025 member reads. IDs, kinds and times only. */
export type AuditItem={category:'action'|'read';action:string;principal_id:string;role?:string;target_kind:string;target_ref:string|null;intent_id:string|null;occurred_at:string};
export function humanActions(value:unknown):AuditItem[]{
  return list(object(value).items).map(x=>{const o=object(x);if(o.category!=='action'&&o.category!=='read')throw new WorkbenchError('invalid');
    return {category:o.category,action:text(o.action),principal_id:text(o.principal_id),...(o.role===undefined?{}:{role:text(o.role)}),target_kind:text(o.target_kind),
      target_ref:maybeText(o.target_ref),intent_id:maybeText(o.intent_id),occurred_at:text(o.occurred_at)};});
}
export const readHumanActions=()=>get('/api/v1/workbench/human-actions',humanActions);

// ---- Enterprise world model W1 (0230; packages/contracts/workbench-world-model.schema.json): published model metadata, read only ----
export type WorldProperty={name:string;withheld:true}|{name:string;withheld:false;value_type:string;required:boolean;nullable:boolean;group:string|null;visibility:string;description:string};
export type WorldObjectType={name:string;version:number;display_name:string;description:string;title_property:string;primary_key:string[];objects:number;properties:WorldProperty[]};
export type WorldRelation={name:string;version:number;source_type:string;target_type:string;cardinality:'one_to_one'|'one_to_many'|'many_to_one'|'many_to_many';inverse_name:string;description:string;property_count:number};
export type WorldEvent={name:string;version:number;object_type:string;description:string;payload_property_count:number};
export type WorldAction={resource_id:string;name:string;version:number;risk_level:string|null;approval_mode:string|null;change_scope:string|null;has_side_effects:boolean;
  object_types:string[];required_scopes:string[];contract_digest:string|null};
export type WorldFunction={resource_id:string;name:string;version:number;applies_to:(string|null)[];required_scopes:string[];contract_digest:string|null};
export type WorldModel={world:string;computed_at:string;object_types:WorldObjectType[];relation_types:WorldRelation[];event_types:WorldEvent[];actions:WorldAction[];functions:WorldFunction[]};
const CARDINALITIES=['one_to_one','one_to_many','many_to_one','many_to_many'] as const;
const texts=(v:unknown)=>list(v).map(text);
function version(v:unknown):number{const n=count(v);if(n<1)throw new WorkbenchError('invalid');return n;}
export function worldModel(value:unknown):WorldModel{
  const v=object(value);
  const property=(x:unknown):WorldProperty=>{const o=object(x);if(flag(o.withheld)){if(Object.keys(o).length!==2)throw new WorkbenchError('invalid');return {name:text(o.name),withheld:true};}
    return {name:text(o.name),withheld:false,value_type:text(o.value_type),required:flag(o.required),nullable:flag(o.nullable),group:maybeText(o.group),visibility:text(o.visibility),description:text(o.description)};};
  return {world:text(v.world),computed_at:text(v.computed_at),
    object_types:list(v.object_types).map(x=>{const o=object(x);return {name:text(o.name),version:version(o.version),display_name:text(o.display_name),description:text(o.description),
      title_property:text(o.title_property),primary_key:texts(o.primary_key),objects:count(o.objects),properties:list(o.properties).map(property)};}),
    relation_types:list(v.relation_types).map(x=>{const o=object(x);const c=text(o.cardinality);if(!(CARDINALITIES as readonly string[]).includes(c))throw new WorkbenchError('invalid');
      return {name:text(o.name),version:version(o.version),source_type:text(o.source_type),target_type:text(o.target_type),cardinality:c as WorldRelation['cardinality'],
        inverse_name:text(o.inverse_name),description:text(o.description),property_count:count(o.property_count)};}),
    event_types:list(v.event_types).map(x=>{const o=object(x);return {name:text(o.name),version:version(o.version),object_type:text(o.object_type),description:text(o.description),
      payload_property_count:count(o.payload_property_count)};}),
    actions:list(v.actions).map(x=>{const o=object(x);return {resource_id:text(o.resource_id),name:text(o.name),version:version(o.version),risk_level:maybeText(o.risk_level),
      approval_mode:maybeText(o.approval_mode),change_scope:maybeText(o.change_scope),has_side_effects:flag(o.has_side_effects),object_types:texts(o.object_types),
      required_scopes:texts(o.required_scopes),contract_digest:maybeText(o.contract_digest)};}),
    functions:list(v.functions).map(x=>{const o=object(x);return {resource_id:text(o.resource_id),name:text(o.name),version:version(o.version),
      applies_to:list(o.applies_to).map(maybeText),required_scopes:texts(o.required_scopes),contract_digest:maybeText(o.contract_digest)};})};
}
export const readWorldModel=()=>get('/api/v1/workbench/world-model',worldModel);

// ---- World model W3: the capability catalog (L6 0222; packages/contracts/workbench-capabilities.schema.json), metadata only ----
export type CatalogCapability={capability:string;operation:string;subject_rule:'human'|'agent_or_human';source_task:string;registered_at:string};
export type CatalogAction={resource_id:string;stable_name:string;version:number;capability:string|null;capability_version:string|null;has_side_effects:boolean;active:boolean;
  effect_category:'customer_contact'|'non_contact_service'|null;effect_category_declared:boolean;effect_manifest_version:number|null};
export type CatalogChannel={namespace:string;trust:'server'|'signed'|'client';skew_seconds:number};
export type Capabilities={capabilities:CatalogCapability[];actions:CatalogAction[];channels:CatalogChannel[];routes:{delivery_action:string;active_offerings:number}[];
  provider_routes:string;real_dispatch:unknown};
function oneOfText<T extends string>(value:unknown,allowed:readonly T[]):T{const v=text(value);if(!(allowed as readonly string[]).includes(v))throw new WorkbenchError('invalid');return v as T;}
export function capabilities(value:unknown):Capabilities{
  const v=object(value);
  return {capabilities:list(v.capabilities).map(x=>{const o=object(x);return {capability:text(o.capability),operation:text(o.operation),
      subject_rule:oneOfText(o.subject_rule,['human','agent_or_human'] as const),source_task:text(o.source_task),registered_at:text(o.registered_at)};}),
    actions:list(v.actions).map(x=>{const o=object(x);return {resource_id:text(o.resource_id),stable_name:text(o.stable_name),version:count(o.version),
      capability:maybeText(o.capability),capability_version:maybeText(o.capability_version),has_side_effects:flag(o.has_side_effects),active:flag(o.active),
      effect_category:o.effect_category===null?null:oneOfText(o.effect_category,['customer_contact','non_contact_service'] as const),
      effect_category_declared:flag(o.effect_category_declared),effect_manifest_version:o.effect_manifest_version===null?null:count(o.effect_manifest_version)};}),
    channels:list(v.channels).map(x=>{const o=object(x);return {namespace:text(o.namespace),trust:oneOfText(o.trust,['server','signed','client'] as const),skew_seconds:count(o.skew_seconds)};}),
    routes:list(v.routes).map(x=>{const o=object(x);return {delivery_action:text(o.delivery_action),active_offerings:count(o.active_offerings)};}),
    provider_routes:text(v.provider_routes),real_dispatch:object(v.real_dispatch)};
}
export const readCapabilities=()=>get('/api/v1/workbench/capabilities',capabilities);

// ---- World model W2 (L6 0223/0224, ADR-035): instances and the depth-1 neighbourhood; identifiers only, each READ checked ----
/** D-G1 (owner 2026-10-20): one schema property of an object as the member may see it. `withheld`: named only (an owner-restricted
 * group, a property the role does not derive, or a value naming a Consumer being erased); the value never reaches the client. */
export type PropertyValue={name:string;status:'ok'|'withheld'|'missing';reason?:'restricted'|'not_permitted'|'erasing';value?:string;valueType?:'text'|'json';truncated?:boolean};
export type ObjectRow={object_id:string;schema_version:number;revision:number;updated_at:string;properties:PropertyValue[]};
export type ObjectValues={type_name:string;object_id:string;schema_version:number;revision:number;properties:PropertyValue[]};
export type ObjectPage={type_name:string;items:ObjectRow[];invisible:number;total:number;next_after:string|null};
export type NeighborGroup={direction:'outgoing'|'incoming';via:string;type_name:string;items:{type_name:string;object_id:string}[];invisible:number;collapsed:number};
export type ObjectNeighbors={type_name:string;object_id:string;depth:1;group_limit:number;groups:NeighborGroup[]};
const TYPE_NAME=/^[A-Z][A-Za-z0-9_]{0,63}$/,OBJECT_ID=/^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$/;
const typeName=(v:unknown)=>{const t=text(v);if(!TYPE_NAME.test(t))throw new WorkbenchError('invalid');return t;};
const objectId=(v:unknown)=>{const t=text(v);if(!OBJECT_ID.test(t))throw new WorkbenchError('invalid');return t;};
const PROPERTY_NAME=/^[A-Za-z][A-Za-z0-9_]{0,63}$/;
export function propertyValues(value:unknown):PropertyValue[]{
  return (value===undefined?[]:list(value)).map(x=>{const o=object(x);const name=text(o.name);if(!PROPERTY_NAME.test(name))throw new WorkbenchError('invalid');
    const status=oneOfText(o.status,['ok','withheld','missing'] as const);
    if(status==='withheld')return {name,status,reason:oneOfText(o.reason,['restricted','not_permitted','erasing'] as const)};
    if(status==='missing')return {name,status};
    return {name,status,value:text(o.value),valueType:oneOfText(o.value_type,['text','json'] as const),...(o.truncated===true?{truncated:true}:{})};});
}
export function objectPage(value:unknown):ObjectPage{
  const v=object(value);
  return {type_name:typeName(v.type_name),items:list(v.items).map(x=>{const o=object(x);return {object_id:objectId(o.object_id),schema_version:count(o.schema_version),
    revision:count(o.revision),updated_at:text(o.updated_at),properties:propertyValues(o.properties)};}),invisible:count(v.invisible),total:count(v.total),next_after:v.next_after===null?null:objectId(v.next_after)};
}
export function objectValues(value:unknown):ObjectValues{
  const v=object(value);
  return {type_name:typeName(v.type_name),object_id:objectId(v.object_id),schema_version:count(v.schema_version),revision:count(v.revision),properties:propertyValues(v.properties)};
}
export function objectNeighbors(value:unknown):ObjectNeighbors{
  const v=object(value);if(v.depth!==1)throw new WorkbenchError('invalid');
  return {type_name:typeName(v.type_name),object_id:objectId(v.object_id),depth:1,group_limit:count(v.group_limit),
    groups:list(v.groups).map(x=>{const o=object(x);const d=text(o.direction);if(d!=='outgoing'&&d!=='incoming')throw new WorkbenchError('invalid');
      return {direction:d,via:text(o.via),type_name:typeName(o.type_name),items:list(o.items).map(i=>{const p=object(i);return {type_name:typeName(p.type_name),object_id:objectId(p.object_id)};}),
        invisible:count(o.invisible),collapsed:count(o.collapsed)};})};
}
export const readObjects=(type:string,after?:string)=>get(`/api/v1/workbench/objects/${encodeURIComponent(type)}${after?'?after='+encodeURIComponent(after):''}`,objectPage);
export const readObjectValues=(type:string,id:string)=>get(`/api/v1/workbench/objects/${encodeURIComponent(type)}/${encodeURIComponent(id)}`,objectValues);
// Capability gap P2: the Object View an object detail of one type uses (layout only), or null (the default layout stays).
export type ObjectView={stable_name:string;version:number;object_type:string;title_property:string|null;sections:{name:string;title:string;fields:string[]}[];relations:string[]};
export function objectView(value:unknown):ObjectView|null{
  const v=object(value);if(v.view===null)return null;const o=object(v.view);
  const name=(x:unknown)=>{const t=text(x);if(!PROPERTY_NAME.test(t))throw new WorkbenchError('invalid');return t;};
  const sections=list(o.sections).map(x=>{const s=object(x);return {name:text(s.name),title:text(s.title),fields:list(s.fields).map(name)};});
  if(!sections.length)throw new WorkbenchError('invalid');
  return {stable_name:text(o.stable_name),version:count(o.version),object_type:typeName(o.object_type),title_property:o.title_property==null?null:name(o.title_property),
    sections,relations:list(o.relations).map(x=>text(x))};
}
export const readObjectView=(type:string)=>get(`/api/v1/workbench/object-views/${encodeURIComponent(type)}`,objectView);
export const readObjectNeighbors=(type:string,id:string)=>get(`/api/v1/workbench/objects/${encodeURIComponent(type)}/${encodeURIComponent(id)}/neighbors`,objectNeighbors);

// ---- NX-039 V4 semantic release (owner only): the current version, the history with rollback reports, the publishable candidates ----
export type ReleaseItem={kind:'alias'|'semantic_note';ref:string;body:string};
export type RollbackReport={from_version:string;to_version:string;removed_aliases:number;claim_count:number;claim_ids:string[];dropped_erased_items:number};
export type SemanticRelease={version:string;reason:'publish'|'rollback';candidate_id:string|null;change_class:'patch'|'minor'|null;rolled_back_to:string|null;
  rollback_report:RollbackReport|null;owner_reason:string;recorded_at:string;items:ReleaseItem[]};
export type PublishableCandidate={candidate_id:string;change_class:'patch'|'minor';change_kind:'alias'|'description';target_refs:string[];term_hashes:number;parent_ok:boolean};
export type SemanticReleases={current:string|null;releases:SemanticRelease[];publishable:PublishableCandidate[]};
const VERSION=/^\d{1,4}\.\d{1,4}\.\d{1,6}$/;
function releaseVersion(value:unknown):string{const v=text(value);if(!VERSION.test(v))throw new WorkbenchError('invalid');return v;}
export function semanticReleases(value:unknown):SemanticReleases{
  const o=object(value);
  return {current:o.current===null?null:releaseVersion(o.current),
    releases:list(o.releases).map(x=>{const r=object(x);const report=r.rollback_report===null||r.rollback_report===undefined?null:object(r.rollback_report);
      return {version:releaseVersion(r.version),reason:oneOfText(r.reason,['publish','rollback'] as const),candidate_id:maybeText(r.candidate_id),
        change_class:r.change_class===null?null:oneOfText(r.change_class,['patch','minor'] as const),rolled_back_to:r.rolled_back_to===null?null:releaseVersion(r.rolled_back_to),
        rollback_report:report?{from_version:releaseVersion(report.from_version),to_version:releaseVersion(report.to_version),removed_aliases:count(report.removed_aliases),
          claim_count:count(report.claim_count),claim_ids:list(report.claim_ids).map(id),dropped_erased_items:count(report.dropped_erased_items)}:null,
        owner_reason:text(r.owner_reason),recorded_at:text(r.recorded_at),
        items:list(r.items).map(i=>{const v=object(i);return {kind:oneOfText(v.kind,['alias','semantic_note'] as const),ref:text(v.ref),body:text(v.body)};})};}),
    publishable:list(o.publishable).map(x=>{const c=object(x);return {candidate_id:text(c.candidate_id),change_class:oneOfText(c.change_class,['patch','minor'] as const),
      change_kind:oneOfText(c.change_kind,['alias','description'] as const),target_refs:list(c.target_refs).map(text),term_hashes:count(c.term_hashes),parent_ok:flag(c.parent_ok)};})};
}
export const readSemanticReleases=()=>get('/api/v1/workbench/semantic-releases',semanticReleases);

// ---- NX-039 V6 review read (owner only): the latest report per candidate (codes, numbers, digests), the stops, the release watch ----
const SHA=/^[0-9a-f]{64}$/;
const rate=(v:unknown):number|null=>{if(v===null||v===undefined)return null;if(typeof v!=='number'||!Number.isFinite(v))throw new WorkbenchError('invalid');return v;};
const digestOf=(v:unknown)=>{const t=text(v);if(!SHA.test(t))throw new WorkbenchError('invalid');return t;};
export type ReviewSide={cases:number|null;parent:number|null;candidate:number|null};
export type ReviewCandidate={candidate_id:string;status:string;end_reason:string|null;dimension:string;change_kind:string|null;report_digest:string;level:'L0'|'L1';
  conclusion:string;reason:string|null;change_class:string|null;hard_gates_version:string|null;gates:{gate:string;result:'pass'|'fail'}[];
  scores:{design:ReviewSide;validation:ReviewSide;ambiguity_rate:number|null;regressions:number|null;missing_cases:number|null};
  non_inferiority:{primary_metric:string;margin:number;ties_accept:boolean}|null;cases:number;cases_digest:string;published_version:string|null;recorded_at:string};
export type ReleaseWatch={release_ref:string;published_at:string;watch_until:string;accesses_before:number;accesses_after:number;miss_rate_before:number;miss_rate_after:number;
  ambiguity_rate_before:number;ambiguity_rate_after:number;judged:boolean;regressing:boolean;suggestion:'rollback'|null};
export type SemanticReview={candidates:ReviewCandidate[];stops:{dimension:string;open:boolean;stopped:boolean;revision:number;changed_at:string|null}[];
  watch:{active:number;regressing:number;releases:ReleaseWatch[]}};
const side=(v:unknown):ReviewSide=>{const o=v&&typeof v==='object'?v as Record<string,unknown>:{};const n=(x:unknown)=>x===null||x===undefined?null:count(x);
  return {cases:n(o.cases),parent:rate(o.parent),candidate:rate(o.candidate)};};
export function semanticReview(value:unknown):SemanticReview{
  const o=object(value);
  return {candidates:list(o.candidates).map(x=>{const c=object(x);const s=object(c.scores);const ni=c.non_inferiority==null?null:object(c.non_inferiority);
      const n=(v:unknown)=>v===null||v===undefined?null:count(v);
      return {candidate_id:text(c.candidate_id),status:text(c.status),end_reason:maybeText(c.end_reason),dimension:text(c.dimension),change_kind:maybeText(c.change_kind),
        report_digest:digestOf(c.report_digest),level:oneOfText(c.level,['L0','L1'] as const),conclusion:text(c.conclusion),reason:maybeText(c.reason),change_class:maybeText(c.change_class),
        hard_gates_version:maybeText(c.hard_gates_version),
        gates:list(c.gates).map(g=>{const v=object(g);if(Object.keys(v).sort().join()!=='gate,result')throw new WorkbenchError('invalid');
          return {gate:text(v.gate),result:oneOfText(v.result,['pass','fail'] as const)};}),
        scores:{design:side(s.design),validation:side(s.validation),ambiguity_rate:rate(s.ambiguity_rate),regressions:n(s.regressions),missing_cases:n(s.missing_cases)},
        non_inferiority:ni?{primary_metric:text(ni.primary_metric),margin:rate(ni.margin)??0,ties_accept:flag(ni.ties_accept)}:null,
        cases:count(c.cases),cases_digest:digestOf(c.cases_digest),published_version:c.published_version==null?null:releaseVersion(c.published_version),recorded_at:text(c.recorded_at)};}),
    stops:list(o.stops).map(x=>{const v=object(x);return {dimension:text(v.dimension),open:flag(v.open),stopped:flag(v.stopped),revision:count(v.revision),changed_at:maybeText(v.changed_at)};}),
    watch:(()=>{const w=object(o.watch);return {active:count(w.active),regressing:count(w.regressing),releases:list(w.releases).map(x=>{const r=object(x);
      return {release_ref:text(r.release_ref),published_at:text(r.published_at),watch_until:text(r.watch_until),accesses_before:count(r.accesses_before),accesses_after:count(r.accesses_after),
        miss_rate_before:rate(r.miss_rate_before)??0,miss_rate_after:rate(r.miss_rate_after)??0,ambiguity_rate_before:rate(r.ambiguity_rate_before)??0,
        ambiguity_rate_after:rate(r.ambiguity_rate_after)??0,judged:flag(r.judged),regressing:flag(r.regressing),suggestion:r.suggestion==null?null:oneOfText(r.suggestion,['rollback'] as const)};})};})()};
}
export const readSemanticReview=()=>get('/api/v1/workbench/semantic-review',semanticReview);

// ---- Object Set (ADR-036): definitions and a preview page evaluated at read time; each object READ-checked, restricted properties withheld ----
export const OBJECT_SET_OPERATORS=['eq','ne','gt','gte','lt','lte','in','not_in'] as const;
export type ObjectSetValue=string|number|boolean;
export type ObjectSetFilter={property:string;operator:typeof OBJECT_SET_OPERATORS[number];value:ObjectSetValue|ObjectSetValue[]};
export type ObjectSetSummary={stable_name:string;version:number;versions:number;object_type:string;object_type_version:number;filters:ObjectSetFilter[];
  sort:{property:string;direction:'asc'|'desc'}[];projection:string[];published_at:string;reason:string};
export type ObjectSetMember={object_id:string;values:Record<string,unknown>;withheld:string[]};
export type ObjectSetPage={stable_name:string;version:number;object_type:string;status:'ok'|'withheld';items:ObjectSetMember[];total:number|null;
  invisible:number;projection:string[];next_offset:number|null};
function scalar(value:unknown):ObjectSetValue{if(typeof value==='string'||typeof value==='boolean'||(typeof value==='number'&&Number.isFinite(value)))return value;throw new WorkbenchError('invalid');}
export function objectSets(value:unknown):ObjectSetSummary[]{
  return list(object(value).object_sets).map(x=>{const o=object(x);
    return {stable_name:text(o.stable_name),version:count(o.version),versions:count(o.versions),object_type:text(o.object_type),object_type_version:count(o.object_type_version),
      filters:list(o.filters).map(f=>{const v=object(f);const operator=text(v.operator);
        if(!(OBJECT_SET_OPERATORS as readonly string[]).includes(operator))throw new WorkbenchError('invalid');
        return {property:text(v.property),operator:operator as ObjectSetFilter['operator'],value:Array.isArray(v.value)?v.value.map(scalar):scalar(v.value)};}),
      sort:list(o.sort).map(s=>{const v=object(s);const direction=text(v.direction);if(direction!=='asc'&&direction!=='desc')throw new WorkbenchError('invalid');
        return {property:text(v.property),direction};}),
      projection:list(o.projection).map(text),published_at:text(o.published_at),reason:text(o.reason)};});
}
export function objectSetPage(value:unknown):ObjectSetPage{
  const o=object(value);const status=text(o.status);if(status!=='ok'&&status!=='withheld')throw new WorkbenchError('invalid');
  return {stable_name:text(o.stable_name),version:count(o.version),object_type:text(o.object_type),status,
    items:status==='ok'?list(o.items).map(x=>{const v=object(x);return {object_id:text(v.object_id),values:object(v.values),withheld:list(v.withheld).map(text)};}):[],
    total:o.total===null?null:count(o.total),invisible:status==='ok'?count(o.invisible):0,projection:status==='ok'?list(o.projection).map(text):[],
    next_offset:o.next_offset===null||o.next_offset===undefined?null:count(o.next_offset)};
}
export const readObjectSets=()=>get('/api/v1/workbench/object-sets',objectSets);
export const readObjectSetMembers=(name:string,offset=0)=>get(`/api/v1/workbench/object-sets/${encodeURIComponent(name)}/members?offset=${offset}&limit=25`,objectSetPage);

// ---- W4 Artifact list (L6 0226; packages/contracts/workbench-artifacts.schema.json): metadata only, newest first, audited ----
export type ArtifactRow={artifact_id:string;purpose:'message_context'|'fallback_context'|'role_context'|'other';media_type:string;size_bytes:number;sha256_prefix:string;
  created_at:string;retention_until:string;run_id:string|null;consumer_id:string|null};
export type ArtifactPage={items:ArtifactRow[];total:number;hidden:number;next_before:string|null};
const PURPOSES=['message_context','fallback_context','role_context','other'] as const;
export function artifactPage(value:unknown):ArtifactPage{
  const v=object(value);
  const match=(re:RegExp,x:unknown)=>{const t=text(x);if(!re.test(t))throw new WorkbenchError('invalid');return t;};
  return {items:list(v.items).map(x=>{const o=object(x);const p=text(o.purpose);if(!(PURPOSES as readonly string[]).includes(p))throw new WorkbenchError('invalid');
      return {artifact_id:match(/^[a-f0-9]{32}$/,o.artifact_id),purpose:p as ArtifactRow['purpose'],media_type:text(o.media_type),size_bytes:count(o.size_bytes),
        sha256_prefix:match(/^[a-f0-9]{12}$/,o.sha256_prefix),created_at:text(o.created_at),retention_until:text(o.retention_until),
        run_id:o.run_id===null?null:match(/^[0-9a-f-]{36}$/,o.run_id),consumer_id:o.consumer_id===null?null:match(/^[a-f0-9]{64}$/,o.consumer_id)};}),
    total:count(v.total),hidden:count(v.hidden),next_before:v.next_before===null?null:match(/^[0-9][0-9T:. +-]{9,39}\|[a-f0-9]{32}$/,v.next_before)};
}
export const readArtifacts=(before?:string)=>get('/api/v1/workbench/artifacts'+(before?'?before='+encodeURIComponent(before):''),artifactPage);

// ---- operator persona (0227/0229: every member reads; writes are owner Actions) and preferences (0228: owner only) ----
export type PersonaDocument={display_name:string;signature?:string;self_intro?:string;tone:string;values?:string[];
  expression:{max_chars:number;emoji:'none'|'few'|'allowed';vocabulary?:string;forbidden_phrases?:string[];language?:string};
  scenarios?:string[];not_for?:string[];ai_disclosure?:{statement?:string;deflect_statement?:string}};
export type PersonaVersion={persona_id:string;version:number;status:'published'|'retired';display_name:string;created_at:string;document:PersonaDocument};
export type PersonaRule={when:{role_id:string}|{property:string;equals:string};persona_id:string};
export type PersonaRules={version:number;default_persona_id:string|null;rules:PersonaRule[];published_at:string};
export type AiDisclosure={mode:'none'|'first_contact'|'always';when_asked:'deflect'|'handoff';block_human_claims:boolean;content_label:boolean;revision:number};
export type PersonaSource='owner_assign'|'rule'|'default';
export type ConsumerPersona={desired:{persona_id:string|null;source:PersonaSource|null;rules_version:number|null}|null;
  settled:{persona_id:string;source:PersonaSource;updated_at:string}|null};
export type PersonaOverview={personas:PersonaVersion[];rules:PersonaRules|null;ai_disclosure:AiDisclosure;consumer:ConsumerPersona|null};
export const PERSONA_SOURCE_TEXT:Record<PersonaSource,string>={owner_assign:'负责人指定',rule:'分群规则',default:'租户默认'};
const PERSONA_ID=/^[a-z][a-z0-9_-]{0,63}$/;
function personaId(value:unknown):string{const v=text(value);if(!PERSONA_ID.test(v))throw new WorkbenchError('invalid');return v;}
function oneOf<T extends string>(value:unknown,options:readonly T[]):T{if(!options.includes(value as T))throw new WorkbenchError('invalid');return value as T;}
function strings(value:unknown):string[]{return list(value).map(text);}
export function personaDocument(value:unknown):PersonaDocument{
  const v=object(value),e=object(v.expression);
  const doc:PersonaDocument={display_name:text(v.display_name),tone:text(v.tone),expression:{max_chars:count(e.max_chars),emoji:oneOf(e.emoji,['none','few','allowed'] as const)}};
  if(v.signature!==undefined)doc.signature=text(v.signature);if(v.self_intro!==undefined)doc.self_intro=text(v.self_intro);
  if(v.values!==undefined)doc.values=strings(v.values);if(v.scenarios!==undefined)doc.scenarios=strings(v.scenarios);if(v.not_for!==undefined)doc.not_for=strings(v.not_for);
  if(e.vocabulary!==undefined)doc.expression.vocabulary=text(e.vocabulary);if(e.language!==undefined)doc.expression.language=text(e.language);
  if(e.forbidden_phrases!==undefined)doc.expression.forbidden_phrases=strings(e.forbidden_phrases);
  if(v.ai_disclosure!==undefined){const a=object(v.ai_disclosure);doc.ai_disclosure={};
    if(a.statement!==undefined)doc.ai_disclosure.statement=text(a.statement);if(a.deflect_statement!==undefined)doc.ai_disclosure.deflect_statement=text(a.deflect_statement);}
  return doc;
}
function personaRule(value:unknown):PersonaRule{
  const v=object(value),w=object(v.when);
  return {when:'role_id' in w?{role_id:id(w.role_id)}:{property:text(w.property),equals:text(w.equals)},persona_id:personaId(v.persona_id)};
}
export function personaOverview(value:unknown):PersonaOverview{
  const v=object(value),a=object(v.ai_disclosure);
  const personas=list(v.personas).map(x=>{const p=object(x);return {persona_id:personaId(p.persona_id),version:count(p.version),status:oneOf(p.status,['published','retired'] as const),
    display_name:text(p.display_name),created_at:text(p.created_at),document:personaDocument(p.document)};});
  const rules=v.rules===null?null:(()=>{const r=object(v.rules);return {version:count(r.version),default_persona_id:r.default_persona_id===null?null:personaId(r.default_persona_id),
    rules:list(r.rules).map(personaRule),published_at:text(r.published_at)};})();
  const sources=['owner_assign','rule','default'] as const;
  const consumer=v.consumer===null||v.consumer===undefined?null:(()=>{const c=object(v.consumer);
    const d=c.desired===null?null:object(c.desired),st=c.settled===null?null:object(c.settled);
    return {desired:d&&{persona_id:d.persona_id===null?null:personaId(d.persona_id),source:d.source===null?null:oneOf(d.source,sources),rules_version:d.rules_version===null?null:count(d.rules_version)},
      settled:st&&{persona_id:personaId(st.persona_id),source:oneOf(st.source,sources),updated_at:text(st.updated_at)}};})();
  return {personas,rules,consumer,ai_disclosure:{mode:oneOf(a.mode,['none','first_contact','always'] as const),when_asked:oneOf(a.when_asked,['deflect','handoff'] as const),
    block_human_claims:flag(a.block_human_claims),content_label:flag(a.content_label),revision:count(a.revision)}};
}
export function readOperatorPersonas(consumerId?:string):Promise<PersonaOverview>{
  return get('/api/v1/workbench/operator-personas'+(consumerId?'?consumer_id='+encodeURIComponent(consumerId):''),personaOverview);
}
export type ContactPreference={claim_id:string;dimension:'quiet_hours'|'frequency'|'channel'|'topic'|'tone';polarity:'affirmed'|'negated';value:string|number;dismissed:boolean};
export const PREFERENCE_TEXT:Record<ContactPreference['dimension'],string>={quiet_hours:'勿扰时段',frequency:'每 7 天最多联系次数',channel:'联系渠道',topic:'联系主题',tone:'沟通语气'};
export function contactPreferences(value:unknown):ContactPreference[]{
  return list(object(value).preferences).map(x=>{const p=object(x);const v=p.value;
    if(typeof v!=='string'&&!Number.isSafeInteger(v))throw new WorkbenchError('invalid');
    return {claim_id:text(p.claim_id),dimension:oneOf(p.dimension,['quiet_hours','frequency','channel','topic','tone'] as const),polarity:oneOf(p.polarity,['affirmed','negated'] as const),
      value:v as string|number,dismissed:flag(p.dismissed)};});
}
export function readContactPreferences(consumerId:string):Promise<ContactPreference[]>{
  return get('/api/v1/workbench/consumers/'+encodeURIComponent(consumerId)+'/contact-preferences',contactPreferences);
}


// ---- outreach controls (0232: owner only) ----
export type ConsentMode='opt_out'|'opt_in';
export type OutreachSettings={time_zone:string;quiet_hours:string|null;per_24h:number;per_7d:number;
  marketing:{enabled:boolean;consent_mode:ConsentMode;unsubscribe_notice:string;per_7d:number;sender_label:string}};
export type OutreachControls={settings:OutreachSettings&{revision:number;source:'tenant'|'default'};
  events:{revision:number;consent_mode_confirmed:ConsentMode|null;marketing_enabled:boolean;recorded_at:string}[]};
const QUIET=/^([01][0-9]|2[0-3]):[0-5][0-9]-([01][0-9]|2[0-3]):[0-5][0-9]$/;
export function outreachControls(value:unknown):OutreachControls{
  const v=object(value),s=object(v.settings),m=object(s.marketing);
  const consent=(x:unknown):ConsentMode=>{if(x!=='opt_out'&&x!=='opt_in')throw new WorkbenchError('invalid');return x;};
  const quiet=s.quiet_hours===null?null:text(s.quiet_hours);if(quiet!==null&&!QUIET.test(quiet))throw new WorkbenchError('invalid');
  const source=s.source==='tenant'||s.source==='default'?s.source:(()=>{throw new WorkbenchError('invalid');})();
  return {settings:{time_zone:text(s.time_zone),quiet_hours:quiet,per_24h:count(s.per_24h),per_7d:count(s.per_7d),revision:count(s.revision),source,
    marketing:{enabled:flag(m.enabled),consent_mode:consent(m.consent_mode),unsubscribe_notice:text(m.unsubscribe_notice),per_7d:count(m.per_7d),sender_label:text(m.sender_label)}},
    events:list(v.events).map(x=>{const e=object(x);return {revision:count(e.revision),consent_mode_confirmed:e.consent_mode_confirmed===null?null:consent(e.consent_mode_confirmed),
      marketing_enabled:flag(e.marketing_enabled),recorded_at:text(e.recorded_at)};})};
}
export function readOutreachControls():Promise<OutreachControls>{return get('/api/v1/workbench/outreach-controls',outreachControls);}


// ---- outreach purposes (outreach slice 3: owner only) ----
export type OutreachPurpose={purpose_id:string;enabled:boolean;revision:number;kind:'commitment_followup'|'service_followup'|'plan_defined'|'marketing';
  trigger:string;topic:string;frequency_class:'service'|'marketing';requires_consent:boolean;requires_approval:boolean;max_chars:number;unsubscribe_notice_required:boolean};
export function outreachPurposes(value:unknown):{list_version:number;purposes:OutreachPurpose[]}{
  const v=object(value);
  return {list_version:count(v.list_version),purposes:list(v.purposes).map(x=>{const p=object(x),d=object(p.definition),r=object(d.content_rules);
    if(p.listed!==true||d.purpose_id!==p.purpose_id)throw new WorkbenchError('invalid');
    const kind=d.kind;if(kind!=='commitment_followup'&&kind!=='service_followup'&&kind!=='plan_defined'&&kind!=='marketing')throw new WorkbenchError('invalid');
    const fc=d.frequency_class;if(fc!=='service'&&fc!=='marketing')throw new WorkbenchError('invalid');
    return {purpose_id:text(p.purpose_id),enabled:flag(p.enabled),revision:count(p.revision),kind,trigger:text(d.trigger),topic:text(d.topic),frequency_class:fc,
      requires_consent:flag(d.requires_consent),requires_approval:flag(d.requires_approval),max_chars:count(r.max_chars),unsubscribe_notice_required:flag(r.unsubscribe_notice_required)};})};
}
export function readOutreachPurposes(){return get('/api/v1/workbench/outreach-purposes',outreachPurposes);}


// ---- outreach approvals (outreach slice 6: owner only; ids, purpose, times and requester, never the body) ----
export type OutreachApproval={intent_id:string;purpose_id:string;requested_at:string;expires_at:string;requested_by:string};
export function outreachApprovals(value:unknown):{window_seconds:number;pending:OutreachApproval[]}{
  const v=object(value);
  return {window_seconds:count(v.window_seconds),pending:list(v.pending).map(x=>{const p=object(x);
    if(Object.keys(p).sort().join()!=='expires_at,intent_id,purpose_id,requested_at,requested_by'||!/^outreach:[0-9a-f]{64}$/.test(text(p.intent_id)))throw new WorkbenchError('invalid');
    return {intent_id:text(p.intent_id),purpose_id:text(p.purpose_id),requested_at:text(p.requested_at),expires_at:text(p.expires_at),requested_by:text(p.requested_by)};})};
}
export function readOutreachApprovals(){return get('/api/v1/workbench/outreach-approvals',outreachApprovals);}


// ---- data health (D-G5: NexLoop's own data flows; owner only; counts, ratios, ages and codes, never a body) ----
export type HealthStatus={status:'ok'|'attention';reasons:string[]};
const ratio=(v:unknown):number|null=>{if(v===null||v===undefined)return null;if(typeof v!=='number'||!Number.isFinite(v)||v<0||v>1)throw new WorkbenchError('invalid');return v;};
function healthStatus(o:Record<string,unknown>):HealthStatus{
  const s=o.status;if(s!=='ok'&&s!=='attention')throw new WorkbenchError('invalid');
  const reasons=list(o.reasons).map(text);if(reasons.some(r=>!/^[a-z_]{1,64}$/.test(r))||(s==='ok')!==(reasons.length===0))throw new WorkbenchError('invalid');
  return {status:s,reasons};
}
/** 0355: an unresolved dead letter (no owner decision, not fully covered by a later success of its conversation); ids, codes and counts
 * only (existing_claims: Claims already recorded from its Messages). */
export type DeadLetterItem={task_id:string;code:string|null;attempts:number;max_attempts:number;conversation_id:string|null;message_count:number;existing_claims:number;created_at:string;dead_lettered_at:string};
function deadLetterItem(value:unknown):DeadLetterItem{
  const o=object(value);
  if(Object.keys(o).sort().join()!=='attempts,code,conversation_id,created_at,dead_lettered_at,existing_claims,max_attempts,message_count,task_id')throw new WorkbenchError('invalid');
  const code=maybeText(o.code);if(code!==null&&!/^[a-z0-9_]{1,64}$/.test(code))throw new WorkbenchError('invalid');
  return {task_id:text(o.task_id),code,attempts:count(o.attempts),max_attempts:count(o.max_attempts),conversation_id:maybeText(o.conversation_id),
    message_count:count(o.message_count),existing_claims:count(o.existing_claims),created_at:text(o.created_at),dead_lettered_at:text(o.dead_lettered_at)};
}
export type DataHealth={computed_at:string;attention:number;
  extraction:HealthStatus&{feed_pending:number;oldest_pending_seconds:number;jobs:Record<string,number>;dead_lettered:number;dead_lettered_total:number;
    unresolved_dead_letters:number;dead_letter_items:DeadLetterItem[];failed_24h:number;finished_24h:number;failure_ratio_24h:number|null};
  recall:HealthStatus&{active_profile:string|null;entries:number;missing_embeddings:number;other_profile_vectors:number;coverage_ratio:number|null;last_backfill_at:string|null};
  reflow:HealthStatus&{pending:number;waiting:number;oldest_seconds:number};
  ingestion:HealthStatus&{outbox_pending:number;commercial_receipts_24h:Record<string,number>;commercial_rejected_24h:number;commercial_rejected_ratio_24h:number|null;
    commercial_late_24h:number;commercial_max_latency_seconds_24h:number};
  artifacts:HealthStatus&{orphans:number;retention_passed:number;stuck_deleting:number};
  erasure:HealthStatus&{open:number;oldest_open_seconds:number;waiting_unknown_effects:number;purge_items_retrying:number;completed_with_holds:number}};
export function dataHealth(value:unknown):DataHealth{
  const v=object(value);
  if(Object.keys(v).sort().join()!=='artifacts,attention,computed_at,erasure,extraction,ingestion,recall,reflow')throw new WorkbenchError('invalid');
  const x=object(v.extraction),r=object(v.recall),f=object(v.reflow),i=object(v.ingestion),a=object(v.artifacts),e=object(v.erasure);
  return {computed_at:text(v.computed_at),attention:count(v.attention),
    extraction:{...healthStatus(x),feed_pending:count(x.feed_pending),oldest_pending_seconds:count(x.oldest_pending_seconds),jobs:counts(x.jobs),dead_lettered:count(x.dead_lettered),
      dead_lettered_total:count(x.dead_lettered_total),unresolved_dead_letters:count(x.unresolved_dead_letters),dead_letter_items:list(x.dead_letter_items).map(deadLetterItem),
      failed_24h:count(x.failed_24h),finished_24h:count(x.finished_24h),failure_ratio_24h:ratio(x.failure_ratio_24h)},
    recall:{...healthStatus(r),active_profile:maybeText(r.active_profile),entries:count(r.entries),missing_embeddings:count(r.missing_embeddings),
      other_profile_vectors:count(r.other_profile_vectors),coverage_ratio:ratio(r.coverage_ratio),last_backfill_at:maybeText(r.last_backfill_at)},
    reflow:{...healthStatus(f),pending:count(f.pending),waiting:count(f.waiting),oldest_seconds:count(f.oldest_seconds)},
    ingestion:{...healthStatus(i),outbox_pending:count(i.outbox_pending),commercial_receipts_24h:counts(i.commercial_receipts_24h),commercial_rejected_24h:count(i.commercial_rejected_24h),
      commercial_rejected_ratio_24h:ratio(i.commercial_rejected_ratio_24h),commercial_late_24h:count(i.commercial_late_24h),
      commercial_max_latency_seconds_24h:count(i.commercial_max_latency_seconds_24h)},
    artifacts:{...healthStatus(a),orphans:count(a.orphans),retention_passed:count(a.retention_passed),stuck_deleting:count(a.stuck_deleting)},
    erasure:{...healthStatus(e),open:count(e.open),oldest_open_seconds:count(e.oldest_open_seconds),waiting_unknown_effects:count(e.waiting_unknown_effects),
      purge_items_retrying:count(e.purge_items_retrying),completed_with_holds:count(e.completed_with_holds)}};
}
export function readDataHealth(){return get('/api/v1/workbench/data-health',dataHealth);}


// ---- outreach metrics and evidence (outreach slice 8: owner only; counts, ratios, ids, digests and codes, never the body) ----
const share=(v:unknown):number|null=>{if(v===null||v===undefined)return null;if(typeof v!=='number'||!Number.isFinite(v)||v<0||v>1)throw new WorkbenchError('invalid');return v;};
const code=(v:unknown):string=>{const t=text(v);if(!/^[a-z][a-z0-9_.]{0,63}$/.test(t))throw new WorkbenchError('invalid');return t;};
const maybeCode=(v:unknown):string|null=>v===null||v===undefined?null:code(v);
const codeCounts=(v:unknown):Record<string,number>=>{const o=counts(v);for(const k of Object.keys(o))code(k);return o;};
export type OutreachMetrics={computed_at:string;window_days:number;candidates:number;accepted_7d:number;pending_approval:number;approvals_7d:Record<string,number>;
  sent_7d:number;fulfilled_7d:number;refused_7d:Record<string,number>;by_purpose_7d:Record<string,{accepted:number;sent:number;refused:number}>;frequency_capped_7d:number;
  marketing:{enabled:boolean;consent_mode:string;sent_7d:number;unsubscribes_7d:number;min_volume:number;unsubscribe_rate_7d:number|null;allowance_per_7d:number;consumers_at_allowance:number};
  consent:{granted_7d:number;withdrawn_7d:number;consumers_with_record:number;consumers_consented:number;consent_rate:number|null}};
export function outreachMetrics(value:unknown):OutreachMetrics{
  const v=object(value),m=object(v.marketing),k=object(v.consent),p=object(v.by_purpose_7d);
  return {computed_at:text(v.computed_at),window_days:count(v.window_days),candidates:count(v.candidates),accepted_7d:count(v.accepted_7d),pending_approval:count(v.pending_approval),
    approvals_7d:codeCounts(v.approvals_7d),sent_7d:count(v.sent_7d),fulfilled_7d:count(v.fulfilled_7d),refused_7d:codeCounts(v.refused_7d),
    by_purpose_7d:Object.fromEntries(Object.entries(p).map(([key,x])=>{const o=object(x);return [code(key),{accepted:count(o.accepted),sent:count(o.sent),refused:count(o.refused)}];})),
    frequency_capped_7d:count(v.frequency_capped_7d),
    marketing:{enabled:flag(m.enabled),consent_mode:code(m.consent_mode),sent_7d:count(m.sent_7d),unsubscribes_7d:count(m.unsubscribes_7d),min_volume:count(m.min_volume),
      unsubscribe_rate_7d:share(m.unsubscribe_rate_7d),allowance_per_7d:count(m.allowance_per_7d),consumers_at_allowance:count(m.consumers_at_allowance)},
    consent:{granted_7d:count(k.granted_7d),withdrawn_7d:count(k.withdrawn_7d),consumers_with_record:count(k.consumers_with_record),consumers_consented:count(k.consumers_consented),
      consent_rate:share(k.consent_rate)}};
}
export function readOutreachMetrics(){return get('/api/v1/workbench/outreach-metrics',outreachMetrics);}

export type OutreachEvidenceItem={intent_id:string;consumer_id:string;persona:string;purpose_id:string;initiator:string;origin:string;state:string;refusal_code:string|null;
  requested_at:string;finished_at:string|null};
export function outreachEvidenceList(value:unknown):{synthetic_only:boolean;items:OutreachEvidenceItem[]}{
  const v=object(value);
  return {synthetic_only:flag(v.synthetic_only),items:list(v.items).map(x=>{const o=object(x);
    if(!/^outreach:[0-9a-f]{64}$/.test(text(o.intent_id)))throw new WorkbenchError('invalid');
    return {intent_id:text(o.intent_id),consumer_id:text(o.consumer_id),persona:code(o.persona),purpose_id:code(o.purpose_id),initiator:code(o.initiator),origin:code(o.origin),
      state:code(o.state),refusal_code:maybeCode(o.refusal_code),requested_at:text(o.requested_at),finished_at:maybeText(o.finished_at)};})};
}
export function readOutreachEvidenceList(syntheticOnly:boolean){
  return get('/api/v1/workbench/outreach-evidence'+(syntheticOnly?'?synthetic_only=true':''),outreachEvidenceList);}

export type OutreachGateRow={gate:string;result:string;detail:unknown};
export type OutreachEvidence={found:true;trigger:{origin_kind:string;origin:string;origin_ref:string;initiator:string;run_id:string|null};
  request:{intent_id:string;consumer_id:string;persona:string;purpose_id:string;purpose_version:number;topic:string;frequency_class:string;channel:string;body_digest:string;body_chars:number;requested_at:string};
  approval:{requested_at:string;expires_at:string;decision:string|null;decided_at:string|null;decided_by:string|null;requested_by:string}|null;
  gates:{stage:'refused'|'admitted'|'not_yet';code:string|null;at:string|null;gates:OutreachGateRow[]|'all_passed'};
  dispatch:{state:string;admitted_at:string|null;finished_at:string|null;provider_reference:string|null;provider:string};
  events:{action:string;at:string}[];
  responses:{optouts:{kind:string;source:string;answers_this_send:boolean;at:string}[];consents:{kind:string;source:string;at:string}[];contact_restricted:boolean}}|{found:false};
export function outreachEvidence(value:unknown):OutreachEvidence{
  const v=object(value);if(v.found===false)return {found:false};if(v.found!==true)throw new WorkbenchError('invalid');
  const t=object(v.trigger),q=object(v.request),g=object(v.gates),d=object(v.dispatch),r=object(v.responses);
  if('body' in q||'text' in q)throw new WorkbenchError('invalid');
  const stage=g.stage;if(stage!=='refused'&&stage!=='admitted'&&stage!=='not_yet')throw new WorkbenchError('invalid');
  const a=v.approval===null?null:object(v.approval);
  return {found:true,
    trigger:{origin_kind:code(t.origin_kind),origin:code(t.origin),origin_ref:text(t.origin_ref),initiator:code(t.initiator),run_id:maybeText(t.run_id)},
    request:{intent_id:text(q.intent_id),consumer_id:text(q.consumer_id),persona:code(q.persona),purpose_id:code(q.purpose_id),purpose_version:count(q.purpose_version),topic:code(q.topic),
      frequency_class:code(q.frequency_class),channel:code(q.channel),body_digest:text(q.body_digest),body_chars:count(q.body_chars),requested_at:text(q.requested_at)},
    approval:a&&{requested_at:text(a.requested_at),expires_at:text(a.expires_at),decision:maybeCode(a.decision),decided_at:maybeText(a.decided_at),decided_by:maybeText(a.decided_by),
      requested_by:text(a.requested_by)},
    gates:{stage,code:maybeCode(g.code),at:maybeText(g.at),gates:g.gates==='all_passed'?'all_passed':list(g.gates).map(x=>{const o=object(x);return {gate:code(o.gate),result:code(o.result),detail:o.detail};})},
    dispatch:{state:code(d.state),admitted_at:maybeText(d.admitted_at),finished_at:maybeText(d.finished_at),provider_reference:maybeText(d.provider_reference),provider:code(d.provider)},
    events:list(v.events).map(x=>{const o=object(x);return {action:code(o.action),at:text(o.at)};}),
    responses:{optouts:list(r.optouts).map(x=>{const o=object(x);return {kind:code(o.kind),source:code(o.source),answers_this_send:flag(o.answers_this_send),at:text(o.at)};}),
      consents:list(r.consents).map(x=>{const o=object(x);return {kind:code(o.kind),source:code(o.source),at:text(o.at)};}),contact_restricted:flag(r.contact_restricted)}};
}
export function readOutreachEvidence(intentId:string){return get('/api/v1/workbench/outreach-evidence?intent_id='+encodeURIComponent(intentId),outreachEvidence);}


// ---- unified approvals (NX-056 A4: every member; the rows are the caller's own, never another person's body) ----
export type ApprovalStatus='waiting_approval'|'approved'|'rejected'|'cancelled'|'expired';
export type ApprovalRow={approval_id:string;capability:string;operation:string;action:string;requested_by:string;requested_at:string;due_by:string;
  status:ApprovalStatus;digest:string;policy_version:number;quorum:number;approvals:number;summary:Record<string,string|number|boolean|null>;
  result?:string|null;inputs?:Record<string,unknown>|null;target_member:TargetMember|null};
/** ADR-039 §6 (R1): the member a request acts on, rendered by the server for owners only; hidden for everyone else. */
export type TargetMember={kind:'member';label:string;username:string|null;account_status:string|null;member_status:string;role:string|null}
  |{kind:'invitee';username:string;same_username_exists:boolean}|{kind:'hidden'}|{kind:'unresolved'}|{kind:'erased'};
function targetMember(value:unknown):TargetMember|null{
  if(value===null)return null;
  const t=object(value);const keys=Object.keys(t).sort().join();
  if(t.kind==='member'&&keys==='account_status,kind,label,member_status,role,username'&&/^p-[0-9a-f]{8}$/.test(text(t.label)))
    return {kind:'member',label:text(t.label),username:maybeText(t.username),account_status:maybeText(t.account_status),member_status:text(t.member_status),role:maybeText(t.role)};
  if(t.kind==='invitee'&&keys==='kind,same_username_exists,username')return {kind:'invitee',username:text(t.username),same_username_exists:flag(t.same_username_exists)};
  if((t.kind==='hidden'||t.kind==='unresolved'||t.kind==='erased')&&keys==='kind')return {kind:t.kind};
  throw new WorkbenchError('invalid');
}
export type Approvals={to_decide:ApprovalRow[];mine:ApprovalRow[];to_execute:ApprovalRow[];solo_mode:{enabled:boolean;others_eligible:boolean}};
const APPROVAL_KEYS='action,approval_id,approvals,capability,digest,due_by,operation,policy_version,quorum,requested_at,requested_by,status,summary,target_member';
const APPROVAL_STATUS:ApprovalStatus[]=['waiting_approval','approved','rejected','cancelled','expired'];
function approvalRow(value:unknown,extra:''|'result'|'inputs'):ApprovalRow{
  const r=object(value);
  if(Object.keys(r).sort().join()!==[...APPROVAL_KEYS.split(','),...(extra?[extra]:[])].sort().join()
    ||!/^[0-9a-f-]{36}$/.test(text(r.approval_id))||!/^[0-9a-f]{12}$/.test(text(r.digest))||!APPROVAL_STATUS.includes(text(r.status) as ApprovalStatus))
    throw new WorkbenchError('invalid');
  const summary=object(r.summary);
  for(const v of Object.values(summary))if(v!==null&&!['string','number','boolean'].includes(typeof v))throw new WorkbenchError('invalid');
  const row:ApprovalRow={approval_id:text(r.approval_id),capability:text(r.capability),operation:text(r.operation),action:text(r.action),
    requested_by:text(r.requested_by),requested_at:text(r.requested_at),due_by:text(r.due_by),status:text(r.status) as ApprovalStatus,digest:text(r.digest),
    policy_version:count(r.policy_version),quorum:count(r.quorum),approvals:count(r.approvals),summary:summary as ApprovalRow['summary'],
    target_member:targetMember(r.target_member)};
  if(extra==='result')row.result=maybeText(r.result);
  if(extra==='inputs')row.inputs=r.inputs===null?null:object(r.inputs);
  return row;
}
export function approvals(value:unknown):Approvals{
  const v=object(value);
  if(Object.keys(v).sort().join()!=='mine,solo_mode,to_decide,to_execute')throw new WorkbenchError('invalid');
  const solo=object(v.solo_mode);
  if(Object.keys(solo).sort().join()!=='enabled,others_eligible')throw new WorkbenchError('invalid');
  return {to_decide:list(v.to_decide).map(x=>approvalRow(x,'')),mine:list(v.mine).map(x=>approvalRow(x,'result')),
    to_execute:list(v.to_execute).map(x=>approvalRow(x,'inputs')),solo_mode:{enabled:flag(solo.enabled),others_eligible:flag(solo.others_eligible)}};
}
export function readApprovals(){return get('/api/v1/workbench/approvals',approvals);}
/** What a governed write's answer says about its approval (design §4): waiting, executed with it, or why not. Null when no policy applied. */
export function approvalOutcome(result:Record<string,unknown>|undefined):string|null{
  if(!result)return null;
  if(result.approval==='required'||(result.status==='waiting_approval'&&result.approval_id))
    return result.reason==='approver_no_longer_eligible'?'批准人已不再具备审批资格，这条请求回到等待审批。':'这个操作需要另一位成员批准：已提交审批，可在“审批”页查看；批准后由你在那里执行。';
  if(result.status==='execution_refused')return '已按批准执行，但被拒绝（'+String(result.code??'')+'）；这次批准已用掉，需要重新发起。';
  if(result.approval==='consumed')return '已按批准执行；这次批准已用掉。';
  if(result.status==='approval_consumed')return '这条请求已经按批准执行过（结果 '+String(result.result??'')+'），没有再次执行。';
  if(result.status==='policy_changed')return '审批策略已更新，原批准作废；请重新提交，按新策略审批。';
  return null;
}

