/** Typed context interpretation. Authenticity additionally requires fresh PG guard. */
import {createHash} from 'node:crypto';
import {RuntimeError,validateRunCommand,type RunCommand} from './runtime-adapter.js';
export const CONTEXT_PROTOCOL='nexloop.context-pack.v1';
export const CONTEXT_PROTOCOL_V2='nexloop.context-pack.v2';
export const CONTEXT_PROTOCOL_V3='nexloop.context-pack.v3';
export const CONTEXT_PROTOCOL_V4='nexloop.context-pack.v4';
export type ContextProtocol=typeof CONTEXT_PROTOCOL|typeof CONTEXT_PROTOCOL_V2|typeof CONTEXT_PROTOCOL_V3|typeof CONTEXT_PROTOCOL_V4;
export type RelationshipItem={assessment_ref:string;revision:number;relation_type_ref:string|null;source_ref:string;target_ref:string;epistemic_kind:'hypothesis'|'user_statement';resolution_state:'resolved'|'awaiting_definition'|'unresolved';conclusion:string;valid_from:string;valid_to:string|null;source_message_ref:string|null;source_content_hash:string|null};
export type RelationshipZone={current_statements:RelationshipItem[];evidence:RelationshipItem[]};
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
/** Exact UTC instant with calendar round-trip; Date.parse alone accepts rolled-over dates. */
function strictUtc(value:unknown):number{
  const raw=text(value,64),match=/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(\.\d{1,6})?(Z|\+00:00)$/.exec(raw);
  if(!match||!Number.isFinite(Date.parse(raw)))return fail();
  const date=new Date(raw),parts=[date.getUTCFullYear(),date.getUTCMonth()+1,date.getUTCDate(),date.getUTCHours(),date.getUTCMinutes(),date.getUTCSeconds()];
  if(parts.some((part,index)=>part!==Number(match[index+1])))return fail();
  return date.getTime();
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
export function validateContextInput(input:unknown,untrustedCommand:unknown,untrustedAttestation:unknown,expectedProtocol?:ContextProtocol):{body:string;run_id:string;relationship_context?:RelationshipZone}{
  const command=validateRunCommand(untrustedCommand);
  if(typeof input!=='string'||Buffer.byteLength(input,'utf8')>65536)return fail();
  let parsed:unknown;try{parsed=JSON.parse(input);}catch{return fail();}
  if(!parsed||typeof parsed!=='object'||Array.isArray(parsed))return fail();
  const protocol=(parsed as Record<string,unknown>).schema_version;
  if(protocol===CONTEXT_PROTOCOL_V4){
    if(expectedProtocol!==undefined&&expectedProtocol!==CONTEXT_PROTOCOL_V4)return fail();
    const pack=exact(parsed,['schema_version','bindings','user_statement','formal_facts','current_constraints','supply','relationship_context']);
    const attestation=validateContextAttestation(command,untrustedAttestation);
    if(attestation.sha256!==createHash('sha256').update(input,'utf8').digest('hex')||canonicalContextJSON(pack)!==input)return fail();
    const base={...pack,schema_version:CONTEXT_PROTOCOL_V2};delete (base as Record<string,unknown>).relationship_context;
    const baseText=canonicalContextJSON(base);
    const validated=validateContextInput(baseText,command,{...attestation,sha256:createHash('sha256').update(baseText).digest('hex')},CONTEXT_PROTOCOL_V2);
    const zone=exact(pack.relationship_context,['current_statements','evidence']);
    if(!Array.isArray(zone.current_statements)||!Array.isArray(zone.evidence)||zone.current_statements.length+zone.evidence.length<1||zone.current_statements.length+zone.evidence.length>4)return fail();
    const seen=new Set<string>();
    for(const [kind,rows] of Object.entries(zone))for(const value of rows as unknown[]){
      const row=exact(value,['assessment_ref','revision','relation_type_ref','source_ref','target_ref','epistemic_kind','resolution_state','conclusion','valid_from','valid_to','source_message_ref','source_content_hash']);
      const id=text(row.assessment_ref,128,/^eios:object:RelationshipAssessment\/[a-f0-9]{64}$/);if(seen.has(id))return fail();seen.add(id);integer(row.revision);
      for(const key of ['source_ref','target_ref'])text(row[key],160,/^eios:object:[A-Za-z][A-Za-z0-9_]*\/[a-f0-9]{64}$/);
      const conclusion=text(row.conclusion,8192);
      if(!['hypothesis','user_statement'].includes(String(row.epistemic_kind))||!['resolved','awaiting_definition','unresolved'].includes(String(row.resolution_state)))return fail();
      if(row.relation_type_ref!==null)text(row.relation_type_ref,160,/^eios:link_type:[A-Za-z][A-Za-z0-9_]*:[1-9][0-9]*$/);
      for(const key of ['valid_from','valid_to'])if(row[key]!==null)strictUtc(row[key]);
      if(row.epistemic_kind==='user_statement'){
        text(row.source_message_ref,128,/^eios:object:Message\/[a-f0-9]{64}$/);text(row.source_content_hash,64,hex64);
        if(createHash('sha256').update(conclusion,'utf8').digest('hex')!==row.source_content_hash)return fail();
      }else if(row.source_message_ref!==null||row.source_content_hash!==null)return fail();
      if(kind==='current_statements'&&(row.epistemic_kind!=='user_statement'||row.resolution_state!=='resolved'||row.relation_type_ref===null))return fail();
    }
    return {...validated,relationship_context:zone as RelationshipZone};
  }

  if(protocol!==CONTEXT_PROTOCOL&&protocol!==CONTEXT_PROTOCOL_V2&&protocol!==CONTEXT_PROTOCOL_V3||expectedProtocol!==undefined&&protocol!==expectedProtocol)return fail();
  const pack=exact(parsed,protocol===CONTEXT_PROTOCOL_V3?['schema_version','bindings','formal_facts','current_constraints','supply','role_binding','trigger_statement']:protocol===CONTEXT_PROTOCOL_V2?['schema_version','bindings','user_statement','formal_facts','current_constraints','supply']:['schema_version','bindings','user_statement','formal_facts','current_constraints']);
  const binding=exact(pack.bindings,['tenant_id','world_id','run_id','source_principal','context_id','namespace','artifact_id','command_digest']);
  const tenant=text(binding.tenant_id,36,uuid),run=text(binding.run_id,36,uuid),source=text(binding.source_principal,512);
  text(binding.context_id,36,uuid);text(binding.namespace,64,hex64);text(binding.artifact_id,32,hex32);text(binding.command_digest,64,hex64);
  if(tenant!==command.tenant_id||run!==command.run_id||binding.world_id!=='real'||command.world_id!=='real'||command.mode!=='real'||command.credential_ref!=='run:'+run)return fail();
  const digest=contextCommandDigest(command),artifact='artifact:'+binding.artifact_id;
  if(binding.command_digest!==digest||command.context_manifest_ref!==artifact)return fail();
  if(binding.namespace!==createHash('sha256').update(tenant+'\u0000real','utf8').digest('hex'))return fail();
  const artifactId=createHash('sha256').update(canonicalContextJSON([tenant,'real',source,'context:'+run]),'utf8').digest('hex').slice(0,32);
  if(binding.artifact_id!==artifactId)return fail();
  let body:string;
  if(protocol===CONTEXT_PROTOCOL_V3){
    const trigger=exact(pack.trigger_statement,['kind','event_id','source_principal','body','provenance']);
    const event=text(trigger.event_id,36,uuid);text(trigger.source_principal,512);body=text(trigger.body,8192);
    if(trigger.kind!=='service_trigger'||event!==command.trigger_event_id||trigger.source_principal!==source||trigger.provenance!=='eios:role-trigger:'+event)return fail();
  }else{
    const statement=exact(pack.user_statement,['message_id','conversation_id','sequence','body','provenance']);
    const message=text(statement.message_id,64,hex64);text(statement.conversation_id,64,hex64);integer(statement.sequence);
    body=text(statement.body,8192);if(statement.provenance!=='eios:object:'+message)return fail();
  }
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
  if(protocol===CONTEXT_PROTOCOL_V2||protocol===CONTEXT_PROTOCOL_V3){
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
  if(protocol===CONTEXT_PROTOCOL_V3){
    const role=exact(pack.role_binding,['binding','definition','definition_provenance','mapping_provenance','grants_authority']);
    const selected=exact(role.binding,['run_id','tenant_id','world','consumer_id','link_id','role_id','step_id','link_revision','role_revision','step_revision','role_ref','scope','expires_at']);
    const roleId=text(selected.role_id,64,hex64),link=text(selected.link_id,64,hex64);
    text(selected.run_id,36,uuid);text(selected.tenant_id,36,uuid);text(selected.consumer_id,64,hex64);text(selected.step_id,64,hex64);
    integer(selected.link_revision);integer(selected.role_revision);integer(selected.step_revision);text(selected.scope,8192);
    if(selected.run_id!==run||selected.tenant_id!==tenant||selected.world!=='real'||selected.consumer_id!==facts.get('Consumer')!.id||selected.step_id!==facts.get('PlanStep')!.id||selected.step_revision!==facts.get('PlanStep')!.revision||selected.role_ref!==command.role_ref||selected.role_ref!=='role:'+roleId+':mapping:'+link||role.grants_authority!==false||role.definition_provenance!=='eios:object:'+roleId||role.mapping_provenance!=='eios:object:'+link)return fail();
    const definition=exact(role.definition,['name','responsibility','ceiling_ref','active','valid_from','valid_until']);
    text(definition.name,8192);text(definition.responsibility,8192);text(definition.ceiling_ref,8192);
    const utc=(value:unknown):number=>{
      const raw=text(value,64),match=/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(\.\d{1,6})?(Z|\+00:00)$/.exec(raw);
      if(!match||!Number.isFinite(Date.parse(raw)))return fail();
      const date=new Date(raw),parts=[date.getUTCFullYear(),date.getUTCMonth()+1,date.getUTCDate(),date.getUTCHours(),date.getUTCMinutes(),date.getUTCSeconds()];
      if(parts.some((part,index)=>part!==Number(match[index+1])))return fail();return date.getTime();
    };
    const from=utc(definition.valid_from),to=utc(definition.valid_until),expires=utc(selected.expires_at);
    if(definition.active!==true||from>=to||from>Date.now()||to<=Date.now()||expires<=Date.now()||expires>to)return fail();
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
