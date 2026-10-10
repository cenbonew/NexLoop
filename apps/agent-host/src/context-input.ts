/** Typed context interpretation. Authenticity additionally requires fresh PG guard. */
import {createHash} from 'node:crypto';
import {RuntimeError,validateRunCommand,type RunCommand} from './runtime-adapter.js';
export const CONTEXT_PROTOCOL='nexloop.context-pack.v1';
export const CONTEXT_PROTOCOL_V2='nexloop.context-pack.v2';
export const CONTEXT_PROTOCOL_V3='nexloop.context-pack.v3';
export const CONTEXT_PROTOCOL_V4='nexloop.context-pack.v4';
export const CONTEXT_PROTOCOL_V5='nexloop.context-pack.v5';
export const CONTEXT_PROTOCOL_V6='nexloop.context-pack.v6';
export type ContextProtocol=typeof CONTEXT_PROTOCOL|typeof CONTEXT_PROTOCOL_V2|typeof CONTEXT_PROTOCOL_V3|typeof CONTEXT_PROTOCOL_V4|typeof CONTEXT_PROTOCOL_V5|typeof CONTEXT_PROTOCOL_V6;
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
export function validateContextInput(input:unknown,untrustedCommand:unknown,untrustedAttestation:unknown,expectedProtocol?:ContextProtocol):{body:string;run_id:string;relationship_context?:RelationshipZone;plans?:Array<{ref:string;plan_ref:string}>}{
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
  if(protocol===CONTEXT_PROTOCOL_V6){
    // v6 = frozen v2 core (validated below exactly as v2) + labelled, server-verified sections.
    // Everything here is data for the model; none of it is authority.
    if(expectedProtocol!==undefined&&expectedProtocol!==CONTEXT_PROTOCOL_V6)return fail();
    // Optional v4 relationship section (message Runs only): validated by the v4 rules below.
    const withRelationships=Object.hasOwn(parsed as Record<string,unknown>,'relationship_context');
    const pack=exact(parsed,['schema_version','strategy_ref','bindings','role','current_event','user_statement','goal','formal_facts','current_constraints','supply',
      'constraints','consumer_state','open_work','evidence','semantics','experience','budget_report','insufficient',...(withRelationships?['relationship_context']:[])]);
    const attestation=validateContextAttestation(command,untrustedAttestation);
    if(attestation.sha256!==createHash('sha256').update(input,'utf8').digest('hex')||canonicalContextJSON(pack)!==input)return fail();
    text(pack.strategy_ref,96,/^context-strategy:[a-z][a-z0-9_]{0,63}@[1-9][0-9]{0,6}$/);
    // Message Run: v2 core + pointer event. Role Run: v3/v5 core (role section, service trigger).
    const roleRun=pack.role!==null;
    if(roleRun&&withRelationships)return fail();
    if(roleRun){
      const role=exact(pack.role,['role_binding','role_policy']);
      if(pack.user_statement!==null||exact(pack.current_event,['kind','event_id','source_principal','body','provenance']).kind!=='service_trigger'||role.role_binding===null)return fail();
    }else{
      const statement=exact(pack.user_statement,['message_id','conversation_id','sequence','body','provenance']);
      const event=exact(pack.current_event,['kind','message_id','provenance']);
      if(event.kind!=='consumer_message'||event.message_id!==statement.message_id||event.provenance!==statement.provenance)return fail();
    }
    const goal=exact(pack.goal,['goal_version_refs','control_snapshot']);
    if(!Array.isArray(goal.goal_version_refs)||goal.goal_version_refs.length!==1)return fail();
    const goalRef=text(goal.goal_version_refs[0],160,/^goal:([a-f0-9]{64})@([1-9][0-9]*)$/);
    const insufficient=pack.insufficient;
    if(!Array.isArray(insufficient)||insufficient.length>16)return fail();
    const codes=['mandatory_exceeds_budget','core_trimmed','required_source_unreadable','required_source_stale','goal_not_current','control_paused','semantic_ambiguous_required'];
    for(const value of insufficient){const row=exact(value,['code','section','refs']);if(!codes.includes(String(row.code))||!Array.isArray(row.refs))return fail();}
    if(goal.control_snapshot===null){if(!insufficient.some(row=>['control_paused','goal_not_current'].includes(String((row as Record<string,unknown>).code))))return fail();}
    else exact(goal.control_snapshot,['control_revision','scopes','goals','objects','budgets']);
    // Formal zone never carries Claims or hypotheses; hypotheses only as labelled evidence.
    const kinds:Record<string,string[]>={constraints:['formal_object','policy'],consumer_state:['formal_object','policy'],open_work:['formal_object','execution_state','policy'],
      evidence:['user_statement','conversation','hypothesis','memory'],semantics:['schema'],experience:['memory']};
    for(const [section,allowed] of Object.entries(kinds)){
      const rows=pack[section];if(!Array.isArray(rows)||rows.length>256)return fail();
      for(const value of rows){
        const item=exact(value,['subsection','ref','revision','content','content_hash','evidence_kind','access_decision_ref','relevance_permille','at','tags']);
        text(item.ref,512);text(item.revision,128);text(item.content_hash,64,hex64);text(item.access_decision_ref,73,/^decision:[a-f0-9]{64}$/);
        if(!allowed.includes(String(item.evidence_kind))||integer(item.relevance_permille,0)>1000||typeof item.at!=='string'||!Array.isArray(item.tags))return fail();
        if(createHash('sha256').update(canonicalContextJSON(item.content),'utf8').digest('hex')!==item.content_hash)return fail();
        if(section!=='evidence'&&String(item.ref).startsWith('claim:'))return fail();
        if((item.evidence_kind==='hypothesis')!==(section==='evidence'&&item.subsection==='hypotheses'))return fail();
        if(item.tags.some(tag=>!['negation','contact_limit','unconfirmed'].includes(String(tag))))return fail();
      }
    }
    exact(pack.budget_report,['estimator','input_token_budget','output_reserve','framing_reserve','available','used','sections','omitted']);
    const core={bindings:pack.bindings,formal_facts:pack.formal_facts,current_constraints:pack.current_constraints,supply:pack.supply};
    const role=pack.role as Record<string,unknown>|null;
    const baseProtocol=!roleRun?(withRelationships?CONTEXT_PROTOCOL_V4:CONTEXT_PROTOCOL_V2):role!.role_policy===null?CONTEXT_PROTOCOL_V3:CONTEXT_PROTOCOL_V5;
    const base=!roleRun?{schema_version:baseProtocol,...core,user_statement:pack.user_statement,...(withRelationships?{relationship_context:pack.relationship_context}:{})}
      :{schema_version:baseProtocol,...core,role_binding:role!.role_binding,trigger_statement:pack.current_event,...(role!.role_policy===null?{}:{role_policy:role!.role_policy})};
    const baseText=canonicalContextJSON(base);
    const validated=validateContextInput(baseText,command,{...attestation,sha256:createHash('sha256').update(baseText).digest('hex')},baseProtocol);
    const goalFact=(pack.formal_facts as Array<Record<string,unknown>>).find(row=>row.type==='Goal')!;
    if(goalRef!=='goal:'+goalFact.id+'@'+goalFact.revision)return fail();
    // NX-025: the Consumer's active plans (read-only open-work policy items, re-derived by SQL at bind).
    const plans=(pack.open_work as Array<Record<string,unknown>>).filter(item=>item.subsection==='plan').map(item=>{
      const ref=text(item.ref,64,/^nexloop:plan:[0-9a-f-]{36}@[1-9][0-9]{0,5}$/);const content=(item.content&&typeof item.content==='object'&&!Array.isArray(item.content)?item.content:fail()) as Record<string,unknown>;
      if(item.evidence_kind!=='policy'||content.plan_ref!=='plan:'+ref.slice('nexloop:plan:'.length))return fail();
      return {ref,plan_ref:String(content.plan_ref)};
    });
    return {...validated,plans};
  }
  if(protocol===CONTEXT_PROTOCOL_V5){
    // v5 = v3 Role Context + governed Role policy provenance (never an authority grant).
    if(expectedProtocol!==undefined&&expectedProtocol!==CONTEXT_PROTOCOL_V5)return fail();
    const pack=exact(parsed,['schema_version','bindings','formal_facts','current_constraints','supply','role_binding','trigger_statement','role_policy']);
    const attestation=validateContextAttestation(command,untrustedAttestation);
    if(attestation.sha256!==createHash('sha256').update(input,'utf8').digest('hex')||canonicalContextJSON(pack)!==input)return fail();
    const policy=exact(pack.role_policy,['binding','ceiling','scope','ceiling_provenance','scope_provenance','grants_authority']);
    const binding=exact(policy.binding,['run_id','tenant_id','world','ceiling_id','ceiling_revision','scope_id','scope_revision','budget','effect_units','expires_at']);
    const ceilingId=text(binding.ceiling_id,64,hex64),scopeId=text(binding.scope_id,64,hex64);integer(binding.ceiling_revision);integer(binding.scope_revision);integer(binding.effect_units);
    if(binding.run_id!==command.run_id||binding.tenant_id!==command.tenant_id||binding.world!=='real'||policy.grants_authority!==false)return fail();
    if(policy.ceiling_provenance!=='eios:object:'+ceilingId||policy.scope_provenance!=='eios:object:'+scopeId)return fail();
    if(canonicalContextJSON(binding.budget)!==canonicalContextJSON(command.budget)||strictUtc(binding.expires_at)<=Date.now())return fail();
    const ceiling=exact(policy.ceiling,['active','action_resources','consumer_ids','goal_ids','step_ids','budget','effect_units','valid_from','valid_until']);
    const scope=exact(policy.scope,['active','role_id','consumer_id','goal_ids','step_ids','valid_from','valid_until']);
    for(const window of [ceiling,scope]){const from=strictUtc(window.valid_from),to=strictUtc(window.valid_until);if(window.active!==true||from>=to||from>Date.now()||to<=Date.now())return fail();}
    if(ceiling.effect_units!==binding.effect_units)return fail();
    const role=exact(pack.role_binding,['binding','definition','definition_provenance','mapping_provenance','grants_authority']);
    const selected=role.binding as Record<string,unknown>,definition=role.definition as Record<string,unknown>;
    if(!selected||!definition||definition.ceiling_ref!==ceilingId||selected.scope!==scopeId||scope.role_id!==selected.role_id||scope.consumer_id!==selected.consumer_id)return fail();
    const includes=(list:unknown,value:unknown)=>Array.isArray(list)&&list.includes(value);
    const facts=new Map((pack.formal_facts as Array<Record<string,unknown>>).map(f=>[f.type,f.id]));
    if(!includes(ceiling.consumer_ids,selected.consumer_id)||![ceiling,scope].every(p=>includes(p.goal_ids,facts.get('Goal'))&&includes(p.step_ids,facts.get('PlanStep'))))return fail();
    const base={...pack,schema_version:CONTEXT_PROTOCOL_V3};delete (base as Record<string,unknown>).role_policy;
    const baseText=canonicalContextJSON(base);
    return validateContextInput(baseText,command,{...attestation,sha256:createHash('sha256').update(baseText).digest('hex')},CONTEXT_PROTOCOL_V3);
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
