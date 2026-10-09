import {ApiError} from './api';

export type Score={lexical_similarity:number;core_term_containment:number;vector_cluster:number;rule_whitelist:number;weighted_total:number;best_match_ref?:string;threshold:number;config_version:string};
export type RecallHit={ref:string;method:string;score:number};
export type Decisions={enabled:false;reason:string;actions:('approve'|'merge_into'|'reject')[]};
export type QueueItem={candidate_id:string;kind:string;status:'pending_review';revision:number;display_name:string;recall:RecallHit[];merge_scores:Score;config_version:string;created_at:string;dependent_claim_count:number};
export type Evidence={claim_id:string;quote:string;source_message_id:string|null;span_start:number|null;span_end:number|null;predicate:string;resolution_state:string};
export type Similar={candidate_id:string;status:string;display_name:string;merge_scores:Score|null};
export type CandidateDetail=QueueItem&{evidence:Evidence[];similar:Similar[];cooldown:{candidate_id:string;cooldown_until:string}|null;decisions:Decisions};
/** forbidden: the queue is not visible to this session (no current ontology.schema.review). */
export type Queue={visible:false}|{visible:true;items:QueueItem[];decisions:Decisions};

const uuid=/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const kinds=['object_type','property','vocabulary_value','alias','object_instance'];
function object(value:unknown):Record<string,unknown>{if(!value||typeof value!=='object'||Array.isArray(value))throw new Error('响应无效');return value as Record<string,unknown>;}
function text(value:unknown):string{if(typeof value!=='string'||!value)throw new Error('响应无效');return value;}
function unit(value:unknown):number{if(typeof value!=='number'||!Number.isFinite(value)||value<0||value>1)throw new Error('响应无效');return value;}
function count(value:unknown):number{if(!Number.isSafeInteger(value)||Number(value)<0)throw new Error('响应无效');return Number(value);}
function score(value:unknown):Score{const v=object(value);return {lexical_similarity:unit(v.lexical_similarity),core_term_containment:unit(v.core_term_containment),vector_cluster:unit(v.vector_cluster),
  rule_whitelist:unit(v.rule_whitelist),weighted_total:unit(v.weighted_total),...(v.best_match_ref===undefined?{}:{best_match_ref:text(v.best_match_ref)}),threshold:unit(v.threshold),config_version:text(v.config_version)};}
function decisions(value:unknown):Decisions{
  const v=object(value);
  // Only the not-yet-enabled state exists until NX-044; anything else is not trusted as "enabled".
  if(v.enabled!==false||!Array.isArray(v.actions))throw new Error('响应无效');
  return {enabled:false,reason:text(v.reason),actions:v.actions.map(a=>{if(a!=='approve'&&a!=='merge_into'&&a!=='reject')throw new Error('响应无效');return a;})};
}
function displayName(candidate:Record<string,unknown>):string{const proposed=object(candidate.proposed);return text(proposed.display_name);}
export function queueItem(value:unknown):QueueItem{
  const v=object(value);const candidate=object(v.candidate);
  if(!uuid.test(text(v.candidate_id))||!kinds.includes(text(v.kind))||v.status!=='pending_review'||!Number.isSafeInteger(v.revision)||!Number.isFinite(Date.parse(text(v.created_at))))throw new Error('响应无效');
  const recall=Array.isArray(candidate.recall)?candidate.recall.map(h=>{const r=object(h);return {ref:text(r.ref),method:text(r.method),score:unit(r.score)};}):[];
  return {candidate_id:text(v.candidate_id),kind:text(v.kind),status:'pending_review',revision:Number(v.revision),display_name:displayName(candidate),recall,
    merge_scores:score(v.merge_scores),config_version:text(v.config_version),created_at:text(v.created_at),dependent_claim_count:count(v.dependent_claim_count)};
}
export function candidateDetail(value:unknown):CandidateDetail{
  const v=object(value);if(!Array.isArray(v.evidence)||!Array.isArray(v.similar))throw new Error('响应无效');
  const evidence=v.evidence.map(raw=>{const e=object(raw);const start=e.span_start===null?null:count(e.span_start);const end=e.span_end===null?null:count(e.span_end);
    if((start===null)!==(end===null)||(start!==null&&end!==null&&end<=start)||typeof e.quote!=='string')throw new Error('响应无效');
    return {claim_id:text(e.claim_id),quote:e.quote,source_message_id:e.source_message_id===null?null:text(e.source_message_id),span_start:start,span_end:end,predicate:text(e.predicate),resolution_state:text(e.resolution_state)};});
  const similar=v.similar.map(raw=>{const s=object(raw);return {candidate_id:text(s.candidate_id),status:text(s.status),display_name:text(object(s.proposed).display_name),merge_scores:s.merge_scores===null?null:score(s.merge_scores)};});
  const cooldown=v.cooldown===null||v.cooldown===undefined?null:(()=>{const c=object(v.cooldown);return {candidate_id:text(c.candidate_id),cooldown_until:text(c.cooldown_until)};})();
  return {...queueItem(v),evidence,similar,cooldown,decisions:decisions(v.decisions)};
}
async function request(path:string):Promise<unknown>{const response=await fetch(path,{credentials:'same-origin',cache:'no-store'});if(!response.ok)throw new ApiError(response.status);return response.json();}
export async function readQueue(limit=50):Promise<Queue>{
  try{const v=object(await request(`/api/v1/review/queue?limit=${limit}`));if(!Array.isArray(v.items))throw new Error('响应无效');return {visible:true,items:v.items.map(queueItem),decisions:decisions(v.decisions)};}
  catch(error){if(error instanceof ApiError&&error.status===403)return {visible:false};throw error;}
}
export async function readCandidate(candidateId:string):Promise<CandidateDetail>{
  if(!uuid.test(candidateId))throw new Error('候选编号无效');
  return candidateDetail(await request(`/api/v1/review/candidates/${candidateId}`));
}
export function reviewError(error:unknown):string{
  if(error instanceof ApiError){if(error.status===401)return '登录已失效，请重新登录。';if(error.status===403)return '你没有审核权限。';if(error.status===404)return '该候选已不在审核队列中。';if(error.status===422)return '请求无效。';}
  if(error instanceof Error&&error.message==='响应无效')return '审核数据格式无效，未显示任何内容。';
  return '审核服务暂不可用（权限或数据暂时无法核验），请稍后重试。';
}
export const KIND_LABELS:Record<string,string>={object_type:'新对象类型',property:'新属性',vocabulary_value:'新词表值',alias:'别名',object_instance:'新实例'};
export const DECISION_LABELS:Record<string,string>={approve:'批准发布',merge_into:'并入已有定义',reject:'拒绝'};
