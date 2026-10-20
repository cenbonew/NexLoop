/**
 * Status labels and tones (workbench UI spec §7): every status code or enum value on a page goes through here and shows its Chinese
 * label from L5's labels.ts (source of truth docs/design/workbench-glossary.md); the raw code stays in the badge's title only. A
 * code the glossary does not list shows 未知状态 (never the code) and is remembered in `unknownCodes()` so tests and the dev console
 * can name it. Tones follow the spec: ok ✓ / pending ● / limited ! / fail ✕ / neutral —.
 */
import * as L from './labels';
import {REFUSAL_TEXT} from './api';

export type Tone='ok'|'fail'|'pending'|'limited'|'neutral';
export const UNKNOWN_LABEL='未知状态';

/** The domains a page may ask for; each is one labels.ts map. Add a domain here, not a map in a page. */
export const DOMAINS={
  commitment_status:L.COMMITMENT_STATUS,commitment_event:L.COMMITMENT_EVENT,commitment_evidence:L.COMMITMENT_EVIDENCE,commitment_exception:L.COMMITMENT_EXCEPTION,
  claim_kind:L.CLAIM_EPISTEMIC_KIND,claim_resolution:L.CLAIM_RESOLUTION,recall_index:L.RECALL_INDEX_KIND,candidate_kind:L.CANDIDATE_KIND,candidate_status:L.CANDIDATE_STATUS,
  merge_configuration:L.MERGE_CONFIGURATION,artifact_purpose:L.ARTIFACT_PURPOSE,job_status:L.JOB_STATUS,queue:L.QUEUE_NAME,feed:L.FEED_NAME,process_sample:L.PROCESS_SAMPLE,
  intent_state:L.INTENT_STATE,outbox_state:L.OUTBOX_STATE,provider_state:L.PROVIDER_STATE,delivery_state:L.DELIVERY_STATE,
  scope_kind:L.SCOPE_KIND,control_event:L.CONTROL_EVENT,escalation_reason:L.ESCALATION_REASON,data_mode:L.DATA_MODE,
  plan_status:L.PLAN_STATUS,plan_outcome:L.PLAN_OUTCOME,goal_kind:L.GOAL_KIND,budget_kind:L.BUDGET_KIND,cost_kind:L.COST_KIND,
  alert_severity:L.ALERT_SEVERITY,backup_repo:L.BACKUP_REPO,review_decision:L.REVIEW_DECISION,review_outcome:L.REVIEW_OUTCOME,
  audit_target:L.AUDIT_TARGET,member_role:L.MEMBER_ROLE,
  // Dispatch refusal codes: the existing api labels and the glossary's three additions (NXC07, NXR01, NXE01).
  refusal_code:{...REFUSAL_TEXT,...L.REFUSAL_CODE_ADDITIONS} as L.LabelMap,
} as const satisfies Record<string,L.LabelMap>;
export type Domain=keyof typeof DOMAINS;

/** Tones per domain for the codes that are not neutral (a code absent here is neutral). */
const TONES:Partial<Record<Domain,Record<string,Tone>>>={
  commitment_status:{fulfilled:'ok',breached:'fail',open:'pending',in_progress:'pending',conditional:'pending',cancelled:'neutral',superseded:'neutral'},
  commitment_exception:Object.fromEntries(Object.keys(L.COMMITMENT_EXCEPTION).map(k=>[k,k==='breached'?'fail':'limited'])),
  job_status:{succeeded:'ok',failed:'fail',dead_lettered:'fail',pending:'pending',running:'pending',retry_wait:'limited'},
  intent_state:{fulfilled:'ok',confirmed:'ok',observed_fulfilled:'ok',failed:'fail',accepted:'pending',dispatching:'pending',unknown:'limited'},
  outbox_state:{fulfilled:'ok',failed:'fail',pending:'pending',leased:'pending',query_pending:'limited',unknown:'limited'},
  delivery_state:{delivered:'ok',failed:'fail',persisted:'pending',dispatching:'pending',provider_accepted:'pending',unknown:'limited'},
  provider_state:{fulfilled:'ok',accepted:'pending',not_found:'limited'},
  plan_status:{active:'pending',stale:'limited',invalidated:'fail',superseded:'neutral',closed:'neutral'},
  control_event:{pause:'limited',contact_restricted:'limited',takeover:'limited',resume:'ok',contact_released:'ok',handback:'ok'},
  escalation_reason:Object.fromEntries(Object.keys(L.ESCALATION_REASON).map(k=>[k,'limited'])),
  candidate_status:{},alert_severity:{critical:'fail',warning:'limited'},
  refusal_code:Object.fromEntries(Object.keys({...REFUSAL_TEXT,...L.REFUSAL_CODE_ADDITIONS}).map(k=>[k,'limited'])),
};

const seen=new Set<string>();
/** Every `domain:code` asked for that the glossary does not list (tests and the TODO list read it). */
export function unknownCodes():string[]{return [...seen].sort();}

export function known(domain:Domain,code:string|null|undefined):boolean{
  return code!=null&&Object.prototype.hasOwnProperty.call(DOMAINS[domain],code);
}
/** The Chinese label of a code; 未知状态 for a code the glossary does not list (remembered, and warned in dev builds). */
export function statusLabel(domain:Domain,code:string|null|undefined):string{
  if(known(domain,code))return (DOMAINS[domain] as L.LabelMap)[code!];
  const key=`${domain}:${code??''}`;
  if(!seen.has(key)){seen.add(key);if((import.meta as {env?:{DEV?:boolean}}).env?.DEV)console.warn('workbench: no label for',key);}
  return UNKNOWN_LABEL;
}
export function statusTone(domain:Domain,code:string|null|undefined):Tone{
  return (code!=null&&TONES[domain]?.[code])||'neutral';
}
