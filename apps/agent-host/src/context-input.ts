/** Typed context interpretation. Authenticity additionally requires fresh PG guard. */
import {createHash} from 'node:crypto';
import {RuntimeError,validateRunCommand,type RunCommand} from './runtime-adapter.js';
export const CONTEXT_PROTOCOL='nexloop.context-pack.v1';
export const CONTEXT_PROTOCOL_V2='nexloop.context-pack.v2';
export type ContextProtocol=typeof CONTEXT_PROTOCOL|typeof CONTEXT_PROTOCOL_V2;
export type ContextAttestation={artifact_ref:string;sha256:string;command_binding_digest:string};
const fields=['schema_version','run_id','request_id','tenant_id','world_id','mode','consumer_ref','goal_version_ref','role_ref','runtime_owner_epoch','runtime_profile','trigger_event_id','budget','not_after'] as const;
const hex64=/^[a-f0-9]{64}$/,hex32=/^[a-f0-9]{32}$/,uuid=/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/;
const fail=():never=>{throw new RuntimeError('runtime_context_invalid');};
function exact(value:unknown,keys:readonly string[]):Record<string,unknown>{
  if(!value||typeof value!=='object'||Array.isArray(value)||Object.getPrototypeOf(value)!==Object.prototype)return fail();
  const row=value as Record<string,unknown>;
  if(Object.keys(row).length!==keys.length||keys.some(key=>!Object.hasOwn(row,key)))return fail();return row;
}
function text(value:unknown,maximum:number,pattern?:RegExp):string{
  if(typeof value!=='string'||!value||[...value].length>maximum||value.includes('\u0000')||/[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/u.test(value)||pattern&&!pattern.test(value))return fail();return value;
}
function integer(value:unknown,minimum=1):number{if(typeof value!=='number'||!Number.isSafeInteger(value)||value<minimum)return fail();return value;}
export function canonicalContextJSON(value:unknown):string{
  const normalize=(row:unknown,depth:number):unknown=>{
    if(depth>12)return fail();
    if(row===null||typeof row==='string'||typeof row==='boolean')return row;
    if(typeof row==='number'){if(!Number.isSafeInteger(row))return fail();return row;}
    if(Array.isArray(row))return row.map(item=>normalize(item,depth+1));
    if(row&&typeof row==='object'&&Object.getPrototypeOf(row)===Object.prototype)return Object.fromEntries(Object.entries(row).sort(([a],[b])=>a<b?-1:a>b?1:0).map(([key,item])=>[key,normalize(item,depth+1)]));
    return fail();
  };
  return JSON.stringify(normalize(value,0));
}
export function contextCommandDigest(command:RunCommand):string{
  const bound=Object.fromEntries(fields.map(key=>[key,command[key]]));return createHash('sha256').update(canonicalContextJSON(bound),'utf8').digest('hex');
}
export function validateContextAttestation(command:RunCommand,value:unknown):ContextAttestation{
  const attestation=exact(value,['artifact_ref','sha256','command_binding_digest']);
  text(attestation.sha256,64,hex64);text(attestation.command_binding_digest,64,hex64);
  if(attestation.artifact_ref!==command.context_manifest_ref||attestation.command_binding_digest!==contextCommandDigest(command))return fail();
  return attestation as ContextAttestation;
}
export function validateContextInput(input:unknown,untrustedCommand:unknown,untrustedAttestation:unknown,expectedProtocol?:ContextProtocol):{body:string;run_id:string}{
  const command=validateRunCommand(untrustedCommand);
  if(typeof input!=='string'||Buffer.byteLength(input,'utf8')>65536)return fail();
  let parsed:unknown;try{parsed=JSON.parse(input);}catch{return fail();}
  if(!parsed||typeof parsed!=='object'||Array.isArray(parsed))return fail();
  const protocol=(parsed as Record<string,unknown>).schema_version;
  if(protocol!==CONTEXT_PROTOCOL&&protocol!==CONTEXT_PROTOCOL_V2||expectedProtocol!==undefined&&protocol!==expectedProtocol)return fail();
  const pack=exact(parsed,protocol===CONTEXT_PROTOCOL_V2?['schema_version','bindings','user_statement','formal_facts','current_constraints','supply']:['schema_version','bindings','user_statement','formal_facts','current_constraints']);
  const binding=exact(pack.bindings,['tenant_id','world_id','run_id','source_principal','context_id','namespace','artifact_id','command_digest']);
  const tenant=text(binding.tenant_id,36,uuid),run=text(binding.run_id,36,uuid),source=text(binding.source_principal,512);
  text(binding.context_id,36,uuid);text(binding.namespace,64,hex64);text(binding.artifact_id,32,hex32);text(binding.command_digest,64,hex64);
  if(tenant!==command.tenant_id||run!==command.run_id||binding.world_id!=='real'||command.world_id!=='real'||command.mode!=='real'||command.credential_ref!=='run:'+run)return fail();
  const digest=contextCommandDigest(command),artifact='artifact:'+binding.artifact_id;
  if(binding.command_digest!==digest||command.context_manifest_ref!==artifact)return fail();
  if(binding.namespace!==createHash('sha256').update(tenant+'\u0000real','utf8').digest('hex'))return fail();
  const artifactId=createHash('sha256').update(canonicalContextJSON([tenant,'real',source,'context:'+run]),'utf8').digest('hex').slice(0,32);
  if(binding.artifact_id!==artifactId)return fail();
  const statement=exact(pack.user_statement,['message_id','conversation_id','sequence','body','provenance']);
  const message=text(statement.message_id,64,hex64);text(statement.conversation_id,64,hex64);integer(statement.sequence);
  const body=text(statement.body,8192);if(statement.provenance!=='eios:object:'+message)return fail();
  if(!Array.isArray(pack.formal_facts)||pack.formal_facts.length!==4)return fail();
  const facts=new Map<string,{id:string;revision:number}>();
  for(const item of pack.formal_facts){
    const fact=exact(item,['type','id','revision','provenance']);
    if(typeof fact.type!=='string'||!['Consumer','Goal','PlanStep','EffectControl'].includes(fact.type)||facts.has(fact.type))return fail();
    const id=text(fact.id,64,hex64),revision=integer(fact.revision);if(fact.provenance!=='eios:object:'+id)return fail();facts.set(fact.type,{id,revision});
  }
  if(command.consumer_ref!=='consumer:'+facts.get('Consumer')!.id)return fail();
  const goal=/^goal:([a-f0-9]{64}):revision:([1-9][0-9]*):step:([1-9][0-9]*):control:([1-9][0-9]*)$/.exec(command.goal_version_ref);
  if(!goal||goal[1]!==facts.get('Goal')!.id||Number(goal[2])!==facts.get('Goal')!.revision||Number(goal[3])!==facts.get('PlanStep')!.revision||Number(goal[4])!==facts.get('EffectControl')!.revision)return fail();
  const constraints=exact(pack.current_constraints,['action','allow_effect','budget_units','reserved_units','valid_until','executor_principal']);
  if(constraints.action!=='nexloop.service.request:1'||constraints.allow_effect!==true||integer(constraints.reserved_units,0)>integer(constraints.budget_units,0))return fail();
  text(constraints.executor_principal,512);
  const until=text(constraints.valid_until,64),date=/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(\.\d{1,6})?(Z|\+00:00)$/.exec(until);
  if(!date||!Number.isFinite(Date.parse(until)))return fail();
  const parsedDate=new Date(until),calendar=[parsedDate.getUTCFullYear(),parsedDate.getUTCMonth()+1,parsedDate.getUTCDate(),parsedDate.getUTCHours(),parsedDate.getUTCMinutes(),parsedDate.getUTCSeconds()];
  if(calendar.some((value,index)=>value!==Number(date[index+1])))return fail();
  if(protocol===CONTEXT_PROTOCOL_V2){
    const supply=exact(pack.supply,['offering_id','offering_revision','binding_id','binding_revision','provenance','properties']);
    const offering=text(supply.offering_id,64,hex64);integer(supply.offering_revision);
    text(supply.binding_id,64,hex64);integer(supply.binding_revision);
    if(supply.provenance!=='eios:object:'+offering)return fail();
    const properties=exact(supply.properties,['service_code','title','delivery_action','content_kind','price_amount','currency','eligibility','allowed_guarantees','allowed_discounts','evidence_kind','active','valid_until']);
    text(properties.title,256);
    if(properties.service_code!=='local.json-export'||properties.delivery_action!==constraints.action||properties.content_kind!=='json-message-export'||properties.price_amount!=='0'||properties.currency!=='CNY'||properties.eligibility!=='current_consumer_plan'||properties.evidence_kind!=='fsynced_json_export'||properties.active!==true||!Array.isArray(properties.allowed_guarantees)||properties.allowed_guarantees.length!==0||!Array.isArray(properties.allowed_discounts)||properties.allowed_discounts.length!==0)return fail();
    const offeringUntil=text(properties.valid_until,64),offerDate=/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(\.\d{1,6})?(Z|\+00:00)$/.exec(offeringUntil);
    if(!offerDate||!Number.isFinite(Date.parse(offeringUntil)))return fail();
    const calendarDate=new Date(offeringUntil),parts=[calendarDate.getUTCFullYear(),calendarDate.getUTCMonth()+1,calendarDate.getUTCDate(),calendarDate.getUTCHours(),calendarDate.getUTCMinutes(),calendarDate.getUTCSeconds()];
    if(parts.some((value,index)=>value!==Number(offerDate[index+1])))return fail();
  }
  // Canonical exact input rejects duplicate keys and alternative encodings.
  // Pack has no self-hash; whole-input digest is supplied only by actual guard.
  if(canonicalContextJSON(pack)!==input)return fail();
  const attestation=exact(untrustedAttestation,['artifact_ref','sha256','command_binding_digest']);
  text(attestation.sha256,64,hex64);text(attestation.command_binding_digest,64,hex64);
  if(attestation.artifact_ref!==artifact||attestation.command_binding_digest!==digest||attestation.sha256!==createHash('sha256').update(input,'utf8').digest('hex'))return fail();
  return {body,run_id:run};
}

/** Provider input stays the complete original pack, including supply authority
 * description. This validation grants no permission; fresh PG guard is required. */
export function validateContextProviderInput(input:unknown,command:unknown,attestation:unknown,expectedProtocol?:ContextProtocol):string{
  validateContextInput(input,command,attestation,expectedProtocol);
  return input as string;
}
