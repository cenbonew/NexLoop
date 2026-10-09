-- NX-023-D: one implementation of the v6 message bind, v4 relationship assessments inside v6,
-- and the v6 Context copy read.
--
-- * The 0092 message bind is redefined on the shared 0093 section verifier (same rules and
--   messages; 0092/0093 files unchanged). A pack carrying `relationship_context` takes its
--   core from the frozen v4 chain, so the v4 rule stays: current statements are resolved user
--   statements only, hypotheses appear as evidence only.
-- * The activation chain applies the v4 relationship and formal-currency checks to such v6
--   packs exactly as to v4.

create function authz.nexloop_context_has_relationships(p_pack jsonb) returns boolean language sql immutable set search_path=pg_catalog as $$
 select p_pack->>'schema_version'='nexloop.context-pack.v4'
  or (p_pack->>'schema_version'='nexloop.context-pack.v6' and jsonb_typeof(p_pack->'relationship_context')='object') $$;
alter function authz.nexloop_context_has_relationships(jsonb) owner to nexloop_owner;
revoke all on function authz.nexloop_context_has_relationships(jsonb) from public;

create or replace function authz.nexloop_context_v6_command(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare a jsonb:=p_text::jsonb;p jsonb;k bytea;ident jsonb;tenant text;principal text;ca jsonb;cp jsonb;snapshot jsonb;pack jsonb;
 b runtime.nexloop_context_artifact_bindings;artifact runtime.nexloop_local_artifacts;cl runtime.nexloop_action_claims;
 conv runtime.nexloop_conversations;consumer text;proof jsonb;key text;fresh boolean;namespace text;context_uuid uuid;run uuid;rel boolean;core jsonb;
begin
 if session_user<>'nexloop_api' or p_world is distinct from 'real' or octet_length(p_text)>1048576 or octet_length(p_payload)>1048576
  or a->>'protocol' is distinct from 'nexloop-context-v6-v1' or a->>'resource_id' is distinct from 'eios:action:nexloop.context.assemble:1'
  or a->>'parameters_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex')
  or a->>'expires_at' is null or (a->>'expires_at')::timestamptz<=clock_timestamp() or (a->>'expires_at')::timestamptz>clock_timestamp()+interval '25 seconds' then
  raise exception 'context v6 unavailable' using errcode='42501';end if;
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-context-v6-v1:'||p_text,'UTF8'),k,'sha256'),'hex') then
  raise exception 'context v6 unavailable' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 ident:=authz.nexloop_service_identity_snapshot(p_digest,p_world);
 if ident->'run_context' is distinct from 'null'::jsonb or ident->'binding'->>'subject_kind' is distinct from 'service' then raise exception 'context v6 unavailable' using errcode='42501';end if;
 tenant:=ident->'binding'->>'tenant_id';principal:=ident->'binding'->>'subject_principal_id';perform set_config('eios.tenant_id',tenant,true);
 p:=p_payload::jsonb;
 if p->>'verb' is distinct from 'bind' or jsonb_typeof(p->'core') is distinct from 'object' or jsonb_typeof(p->'omitted_items') is distinct from 'array'
  or jsonb_array_length(p->'omitted_items')>1024 or p->>'pack_text' is null or octet_length(p->>'pack_text')>65536 then raise exception 'context v6 unavailable' using errcode='42501';end if;
 ca:=(p->'core'->>'text')::jsonb;cp:=(p->'core'->>'payload')::jsonb;
 if cp->>'verb' is distinct from 'snapshot' then raise exception 'context v6 unavailable' using errcode='42501';end if;
 pack:=(p->>'pack_text')::jsonb;rel:=pack ? 'relationship_context';
 -- Core: the frozen v2 chain (or the v4 chain when the pack carries relationship assessments)
 -- re-derives and authorizes its sections under its own signed envelope.
 if rel then snapshot:=authz.nexloop_relationship_context_artifact_command(p_digest,p_world,p->'core'->>'text',p->'core'->>'signature',p->'core'->>'payload');
 else snapshot:=authz.nexloop_context_artifact_command(p_digest,p_world,p->'core'->>'text',p->'core'->>'signature',p->'core'->>'payload');end if;
 if p->>'pack_digest' is distinct from encode(sha256(convert_to(p->>'pack_text','UTF8')),'hex') or runtime.nexloop_canonical_json(pack) is distinct from p->>'pack_text'
  or pack->>'schema_version' is distinct from 'nexloop.context-pack.v6' or (select count(*) from jsonb_object_keys(pack))<>(case when rel then 19 else 18 end)
  or not pack ?& array['strategy_ref','bindings','role','current_event','user_statement','goal','formal_facts','current_constraints','supply','constraints','consumer_state',
   'open_work','evidence','semantics','experience','budget_report','insufficient'] then raise exception 'context v6 pack invalid' using errcode='42501';end if;
 foreach key in array array['bindings','user_statement','formal_facts','current_constraints','supply']||case when rel then array['relationship_context'] else array[]::text[] end loop
  if pack->key is distinct from snapshot->key then raise exception 'context v6 core mismatch' using errcode='42501';end if;
 end loop;
 if pack->'role' is distinct from 'null'::jsonb then raise exception 'context v6 Role section unsupported' using errcode='42501';end if;
 if pack->'current_event' is distinct from jsonb_build_object('kind','consumer_message','message_id',pack->'user_statement'->'message_id','provenance',pack->'user_statement'->'provenance') then
  raise exception 'context v6 current event mismatch' using errcode='42501';end if;
 context_uuid:=(pack->'bindings'->>'context_id')::uuid;run:=(pack->'bindings'->>'run_id')::uuid;
 select * into conv from runtime.nexloop_conversations where tenant_id=tenant and world=p_world and conversation_id=pack->'user_statement'->>'conversation_id' for share;
 select value->>'id' into consumer from jsonb_array_elements(pack->'formal_facts') where value->>'type'='Consumer';
 if not found or conv.consumer_id is distinct from consumer then raise exception 'context v6 consumer mismatch' using errcode='42501';end if;
 -- A bound pack replays unchanged: it was fully verified when bound (its read proofs have
 -- since expired); only the Artifact, the bind claim and the core above are rechecked.
 select * into b from runtime.nexloop_context_artifact_bindings where tenant_id=tenant and world=p_world and message_id=pack->'user_statement'->>'message_id' for update;
 fresh:=not found;
 if not fresh and (b.pack_text is distinct from p->>'pack_text' or b.run_id is distinct from run) then raise exception 'context artifact conflict' using errcode='23505';end if;
 if fresh then
  -- Core sources: what the frozen chain re-derived, cited under the envelope it verified;
  -- v4 relationship rows keep their v4 meaning (hypotheses are evidence only).
  core:=jsonb_build_array(
    jsonb_build_object('section','current_event','ref','eios:object:Message/'||(pack->'user_statement'->>'message_id'),'revision',pack->'user_statement'->>'sequence',
     'hash',runtime.nexloop_context_hash(pack->'user_statement'),'kind','current_message','decision',runtime.nexloop_context_hash(coalesce(ca->'message_read_envelope',ca->'trigger_message_envelope'))),
    jsonb_build_object('section','constraints','ref','eios:action:nexloop.service.request:1','revision',runtime.nexloop_context_hash(pack->'current_constraints'),
     'hash',runtime.nexloop_context_hash(pack->'current_constraints'),'kind','policy','decision',runtime.nexloop_context_hash(ca)),
    jsonb_build_object('section','constraints','ref','eios:object:'||(pack->'supply'->>'offering_id'),'revision',pack->'supply'->>'offering_revision',
     'hash',runtime.nexloop_context_hash(pack->'supply'),'kind','formal_object','decision',runtime.nexloop_context_hash(ca->'catalog_envelope')))
   ||coalesce((select jsonb_agg(jsonb_build_object('section','constraints','ref','eios:object:'||(f->>'type')||'/'||(f->>'id'),'revision',f->>'revision',
     'hash',runtime.nexloop_context_hash(f),'kind','formal_object','decision',runtime.nexloop_context_hash(ca))) from jsonb_array_elements(pack->'formal_facts') f),'[]'::jsonb)
   ||case when rel then coalesce((select jsonb_agg(jsonb_build_object('section','evidence','ref',x->>'assessment_ref','revision',x->>'revision','hash',runtime.nexloop_context_hash(x),
     'kind',x->>'epistemic_kind','decision',runtime.nexloop_context_hash(ca->'relationship_envelopes')))
     from jsonb_array_elements((pack->'relationship_context'->'current_statements')||(pack->'relationship_context'->'evidence')) x),'[]'::jsonb) else '[]'::jsonb end;
  perform authz.nexloop_context_v6_sections(p_digest,p_world,tenant,principal,a,p,pack,array[conv.conversation_id],consumer,run,context_uuid,core);
 end if;
 -- Artifact and governed bind claim: same rules as the frozen bind, against the v6 bytes.
 namespace:=encode(sha256(convert_to(tenant,'UTF8')||decode('00','hex')||convert_to(p_world,'UTF8')),'hex');
 select * into artifact from runtime.nexloop_local_artifacts where tenant_id=tenant and world=p_world and artifact_id=p->>'artifact_id' for share;
 if not found or artifact.artifact_id is distinct from pack->'bindings'->>'artifact_id' or artifact.status is distinct from 'available' or artifact.principal_id is distinct from principal
  or artifact.sha256 is distinct from p->>'pack_digest' or artifact.size_bytes is distinct from octet_length(p->>'pack_text')
  or artifact.media_type is distinct from 'application/vnd.nexloop.context+json' or artifact.retention_until<=clock_timestamp()
  or artifact.object_key is distinct from namespace||'/'||artifact.artifact_id then raise exception 'context artifact unavailable' using errcode='42501';end if;
 select * into cl from runtime.nexloop_action_claims where tenant_id=tenant and world=p_world and action_name='nexloop.context.bind' and intent_id=p->>'claim_id' for share;
 if not found or cl.principal_id is distinct from principal or cl.claim->'binding' is distinct from ca->'claim_binding' or cl.claim->>'state' not in ('active','terminal')
  or (cl.claim->>'state'='active' and (cl.claim->>'lease_expires_at')::timestamptz<=clock_timestamp())
  or (cl.claim->>'state'='terminal' and (fresh or cl.claim->'terminal_outcome'->>'status' is distinct from 'succeeded' or cl.claim->'terminal_outcome'->>'outcome_digest' is distinct from artifact.sha256)) then
  raise exception 'context artifact unavailable' using errcode='42501';end if;
 if fresh then
  insert into runtime.nexloop_context_artifact_bindings values(tenant,p_world,pack->'user_statement'->>'message_id',run,principal,p_digest,context_uuid,artifact.artifact_id,namespace,
   p->>'pack_text',p->>'pack_digest',authz.nexloop_context_command_binding(cp->'command'),clock_timestamp());
 elsif not exists(select 1 from runtime.nexloop_context_packs c where c.tenant_id=tenant and c.world=p_world and c.context_id=context_uuid and c.run_id=run and c.pack_digest=p->>'pack_digest') then
  raise exception 'context artifact conflict' using errcode='23505';
 end if;
 -- Tail: every authority used above is still current at return.
 for proof in select value from jsonb_array_elements(a->'read_proofs') loop
  if (proof->>'expires_at')::timestamptz<=clock_timestamp() then raise exception 'context read proof expired' using errcode='42501';end if;
  perform authz.nexloop_assert_read_authority(p_digest,p_world,proof);
 end loop;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 return jsonb_build_object('context_id',context_uuid,'run_id',run,'pack_digest',p->>'pack_digest','strategy_ref',pack->>'strategy_ref','bound',true);
end $$;

-- Activation chain (0092 body): v4 relationship/formal checks also for v6 packs with relationships.
create or replace function authz.nexloop_runtime_activation_command_before_roles_v0062(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare a jsonb:=p_text::jsonb;p jsonb:=p_payload::jsonb;command jsonb;result jsonb;b runtime.nexloop_context_artifact_bindings;env jsonb;checked jsonb;supply jsonb;tail_env jsonb;tail_proof jsonb;formal_meta jsonb;
begin
 result:=authz.nexloop_runtime_activation_command_v0052(p_digest,p_world,p_text,p_signature,p_payload);
 command:=(p->>'command_text')::jsonb;
 if not exists(select 1 from authz.nexloop_message_run_issuances i where i.run_id=(command->>'run_id')::uuid) then return result;end if;
 -- Original 52 already owns lease/fence and validates immutable pack binding.
 select * into b from runtime.nexloop_context_artifact_bindings where tenant_id=a->>'tenant_id' and world=p_world and run_id=(command->>'run_id')::uuid;
 if not found or (b.pack_text::jsonb->>'schema_version' is null or b.pack_text::jsonb->>'schema_version' not in ('nexloop.context-pack.v2','nexloop.context-pack.v4','nexloop.context-pack.v6')) then raise exception 'catalog context mandatory' using errcode='42501';end if;
 supply:=b.pack_text::jsonb->'supply';
 if jsonb_typeof(supply) is distinct from 'object' then raise exception 'catalog context mandatory' using errcode='42501';end if;
 if p->>'verb'='resolve' then
  if authz.nexloop_context_has_relationships(b.pack_text::jsonb) then result:=result||jsonb_build_object('_context_relationship',b.pack_text::jsonb->'relationship_context','_context_formal_facts',b.pack_text::jsonb->'formal_facts');end if;
  return result||jsonb_build_object('_context_catalog',supply,'_context_consumer_id',substr(command->>'consumer_ref',10));
 elsif p->>'verb'='authorize' then
  env:=a->'context_catalog_envelope';
  if jsonb_typeof(env) is distinct from 'object' then raise exception 'catalog context mandatory' using errcode='42501';end if;
  checked:=authz.nexloop_service_catalog_scope(b.source_digest,p_world,env->>'text',env->>'signature',env->>'payload');
  if checked->'allowed' is distinct from 'true'::jsonb or checked->'supply' is distinct from supply
   or (env->>'payload')::jsonb->>'consumer_id' is distinct from substr(command->>'consumer_ref',10) then raise exception 'catalog context stale' using errcode='42501';end if;
  if p->>'verb'='authorize' and authz.nexloop_context_has_relationships(b.pack_text::jsonb) then if authz.nexloop_relationship_context_snapshot(b.source_digest,p_world,substr(command->>'consumer_ref',10),a->'context_relationship_envelopes') is distinct from b.pack_text::jsonb->'relationship_context' then raise exception 'relationship context stale' using errcode='42501';end if;end if;
  result:=authz.nexloop_runtime_activation_command_v0052(p_digest,p_world,p_text,p_signature,p_payload);
  checked:=authz.nexloop_service_catalog_scope(b.source_digest,p_world,env->>'text',env->>'signature',env->>'payload');
  if checked->'allowed' is distinct from 'true'::jsonb or checked->'supply' is distinct from supply then raise exception 'catalog context stale' using errcode='42501';end if;
 end if;
  if p->>'verb'='authorize' and authz.nexloop_context_has_relationships(b.pack_text::jsonb) then if authz.nexloop_relationship_context_snapshot(b.source_digest,p_world,substr(command->>'consumer_ref',10),a->'context_relationship_envelopes') is distinct from b.pack_text::jsonb->'relationship_context' then raise exception 'relationship context stale' using errcode='42501';end if;end if;
 if p->>'verb'='authorize' and authz.nexloop_context_has_relationships(b.pack_text::jsonb) then
  formal_meta:=authz.nexloop_context_formal_current(b.source_digest,p_world,b.pack_text::jsonb->'formal_facts',a->'context_formal_reads');
  perform authz.nexloop_assert_context_proof_deadlines(a);
  perform authz.nexloop_assert_context_proof_deadlines(a->'context_artifact_proof');
  for tail_proof in select value from jsonb_array_elements(p->'run_proofs') loop perform authz.nexloop_assert_context_proof_deadlines(tail_proof);end loop;
  for tail_env in select value from jsonb_each((a->'context_catalog_envelope'->>'payload')::jsonb->'reads') loop perform authz.nexloop_assert_context_proof_deadlines((tail_env->>'text')::jsonb);end loop;
  for tail_env in select value from jsonb_each(a->'context_formal_reads') loop perform authz.nexloop_assert_context_proof_deadlines((tail_env->>'text')::jsonb);end loop;
  perform authz.nexloop_assert_relationship_deadlines(a->'context_relationship_envelopes',b.pack_text::jsonb->'relationship_context');
  if formal_meta->>'control_valid_until' is null or (formal_meta->>'control_valid_until')::timestamptz<=clock_timestamp() then raise exception 'context activation formal expired' using errcode='42501';end if;
  if command->>'not_after' is null or (command->>'not_after')::timestamptz<=clock_timestamp() or b.pack_text::jsonb->'current_constraints'->>'valid_until' is null or (b.pack_text::jsonb->'current_constraints'->>'valid_until')::timestamptz<=clock_timestamp() then raise exception 'context activation expired at return' using errcode='42501';end if;
 end if;
 return result;
end $$;
-- v6 Context copy read. A copy of a v6 pack is handed out only to a reader that holds
-- current READ, right now, on every object the pack was assembled from: the trigger
-- Message (or, for a Role pack, the Role binding objects and only the producing Source),
-- the formal facts, the supply, v4 relationship assessments, each Conversation whose
-- Claims are quoted and the Consumer whose open work / state is shown. The required
-- reads are derived here from the bound pack, never chosen by the caller.
create function authz.nexloop_context_v6_copy_dependency(p_tenant text,p_world text,p_artifact text,p_principal text) returns jsonb
language plpgsql stable security definer set search_path=pg_catalog set row_security=on as $$
declare pack jsonb;run uuid;message text;source text;reads jsonb:='{}'::jsonb;x jsonb;n integer:=0;fields jsonb;consumer text;
begin
 select b.pack_text::jsonb,b.run_id,b.message_id,b.source_principal into pack,run,message,source from runtime.nexloop_context_artifact_bindings b
  where b.tenant_id=p_tenant and b.world=p_world and b.artifact_id=p_artifact;
 if not found then
  select r.pack_text::jsonb,r.run_id,null,r.source_principal into pack,run,message,source from runtime.nexloop_role_context_artifacts r
   where r.tenant_id=p_tenant and r.world=p_world and r.artifact_id=p_artifact;
  if not found then return null;end if;
 end if;
 if pack->>'schema_version' is distinct from 'nexloop.context-pack.v6' then return null;end if;
 consumer:=(select f->>'id' from jsonb_array_elements(pack->'formal_facts') f where f->>'type'='Consumer');
 if message is not null then
  reads:=reads||jsonb_build_object('message',jsonb_build_object('type_name','Message','object_id',message,'fields','["actor","body"]'::jsonb));
 else
  -- Role trigger text is service-internal: only the producing Source copies a Role pack (as v3/v5).
  if source is distinct from p_principal then raise exception 'role Context source unavailable' using errcode='42501';end if;
  reads:=reads||jsonb_build_object(
   'role',jsonb_build_object('type_name','RoleDefinition','object_id',pack->'role'->'role_binding'->'binding'->>'role_id','fields','["active","ceiling_ref","name","responsibility","valid_from","valid_until"]'::jsonb),
   'link',jsonb_build_object('type_name','ConsumerRoleLink','object_id',pack->'role'->'role_binding'->'binding'->>'link_id','fields','["active","consumer_id","role_id","scope","valid_from","valid_until"]'::jsonb),
   'step',jsonb_build_object('type_name','PlanStep','object_id',pack->'role'->'role_binding'->'binding'->>'step_id','fields','["consumer_id","state","submitter_principals"]'::jsonb));
 end if;
 reads:=reads||jsonb_build_object(
  'offering',jsonb_build_object('type_name','ServiceOffering','object_id',pack->'supply'->>'offering_id','fields','["active","allowed_discounts","allowed_guarantees","content_kind","currency","delivery_action","eligibility","evidence_kind","price_amount","service_code","title","valid_until"]'::jsonb),
  'offering_binding',jsonb_build_object('type_name','ConsumerServiceOffering','object_id',pack->'supply'->>'binding_id','fields','["active","consumer_id","offering_id","offering_revision","source_principal"]'::jsonb));
 reads:=reads||(select jsonb_object_agg('fact_'||(f->>'type'),jsonb_build_object('type_name',f->>'type','object_id',f->>'id','fields',
   case when f->>'type'='EffectControl' then '["allow_effect","budget_units","executor_principal","valid_until"]'::jsonb else '[]'::jsonb end)) from jsonb_array_elements(pack->'formal_facts') f);
 if jsonb_typeof(pack->'relationship_context')='object' then
  for x in select value from jsonb_array_elements((pack->'relationship_context'->'current_statements')||(pack->'relationship_context'->'evidence')) order by value->>'assessment_ref' loop
   n:=n+1;
   reads:=reads||jsonb_build_object('relationship_'||n,jsonb_build_object('type_name','RelationshipAssessment','object_id',substr(x->>'assessment_ref',36),
    'fields','["conclusion","corrects_revision","epistemic_kind","evidence_content_hash","evidence_message_id","relation_name","relation_version","resolution_state","source_id","source_type","target_id","target_type","valid_from","valid_to"]'::jsonb));
  end loop;
 end if;
 -- Quoted Claims: the Conversation they were read under (same basis as at assembly).
 reads:=reads||coalesce((select jsonb_object_agg('conversation_'||c.conversation_id,jsonb_build_object('type_name','Conversation','object_id',c.conversation_id,'fields','[]'::jsonb))
  from (select distinct x2.conversation_id from jsonb_array_elements(pack->'evidence') i join ontology.nexloop_claims x2
   on x2.tenant_id=p_tenant and x2.world=p_world and x2.claim_id=substr(i->>'ref',7) where i->>'ref' like 'claim:%') c),'{}'::jsonb);
 -- Consumer open work and formal state: Consumer READ plus every shown property.
 if jsonb_array_length(pack->'open_work')>0 or jsonb_array_length(pack->'consumer_state')>0 or jsonb_array_length(pack->'constraints')>0 then
  select coalesce(jsonb_agg(distinct k order by k),'[]'::jsonb) into fields from (select jsonb_object_keys(i->'content'->'properties') k
   from jsonb_array_elements((pack->'consumer_state')||(pack->'constraints')) i where jsonb_typeof(i->'content'->'properties')='object') q;
  reads:=reads||jsonb_build_object('consumer_items',jsonb_build_object('type_name','Consumer','object_id',consumer,'fields',fields));
 end if;
 return jsonb_build_object('kind','v6','run_id',run,'reads',reads);
end $$;
alter function authz.nexloop_context_v6_copy_dependency(text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_context_v6_copy_dependency(text,text,text,text) from public;

alter function authz.nexloop_context_artifact_read_dependency_v2(text,text,text,text) rename to nexloop_context_artifact_read_dependency_v2_before_v6;
revoke all on function authz.nexloop_context_artifact_read_dependency_v2_before_v6(text,text,text,text) from public,nexloop_api,nexloop_domain_worker,nexloop_scheduler,nexloop_action_worker,nexloop_identity;
create function authz.nexloop_context_artifact_read_dependency_v2(p_digest text,p_world text,p_permit text,p_payload text)
returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare i jsonb;dep jsonb;r jsonb;
begin
 i:=authz.nexloop_service_identity_snapshot(p_digest,p_world);perform set_config('eios.tenant_id',i->'binding'->>'tenant_id',true);
 dep:=authz.nexloop_context_v6_copy_dependency(i->'binding'->>'tenant_id',p_world,p_payload::jsonb->>'artifact_id',i->'binding'->>'subject_principal_id');
 if dep is null then return authz.nexloop_context_artifact_read_dependency_v2_before_v6(p_digest,p_world,p_permit,p_payload);end if;
 -- The reader's own Artifact READ first; the dependency names objects, not content.
 r:=authz.nexloop_read_local_artifact_v0060(p_digest,p_world,p_permit,p_payload);
 if r->>'media_type' is distinct from 'application/vnd.nexloop.context+json' then raise exception 'context artifact unavailable' using errcode='42501';end if;
 return dep;
end $$;
alter function authz.nexloop_context_artifact_read_dependency_v2(text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_context_artifact_read_dependency_v2(text,text,text,text) from public,nexloop_scheduler,nexloop_action_worker,nexloop_identity;
grant execute on function authz.nexloop_context_artifact_read_dependency_v2(text,text,text,text) to nexloop_api,nexloop_domain_worker;

alter function authz.nexloop_read_local_artifact(text,text,text,text) rename to nexloop_read_local_artifact_before_v6;
revoke all on function authz.nexloop_read_local_artifact_before_v6(text,text,text,text) from public,nexloop_api,nexloop_domain_worker,nexloop_scheduler,nexloop_action_worker,nexloop_identity;
create function authz.nexloop_read_local_artifact(p_digest text,p_world text,p_permit text,p_payload text)
returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare i jsonb;dep jsonb;d jsonb:=p_payload::jsonb->'context_dependency';r jsonb;name text;reference jsonb;env jsonb;a jsonb;permit jsonb;
begin
 i:=authz.nexloop_service_identity_snapshot(p_digest,p_world);perform set_config('eios.tenant_id',i->'binding'->>'tenant_id',true);
 dep:=authz.nexloop_context_v6_copy_dependency(i->'binding'->>'tenant_id',p_world,p_payload::jsonb->>'artifact_id',i->'binding'->>'subject_principal_id');
 if dep is null then return authz.nexloop_read_local_artifact_before_v6(p_digest,p_world,p_permit,p_payload);end if;
 if jsonb_typeof(d) is distinct from 'object' or d->>'kind' is distinct from 'v6' or d->>'run_id' is distinct from dep->>'run_id'
  or jsonb_typeof(d->'reads') is distinct from 'object'
  or (select array_agg(k order by k) from jsonb_object_keys(d->'reads') k) is distinct from (select array_agg(k order by k) from jsonb_object_keys(dep->'reads') k) then
  raise exception 'context v6 copy READ required' using errcode='42501';end if;
 -- Every source object, with exactly the fields the pack shows, under the reader's own current READ.
 for name,reference in select key,value from jsonb_each(dep->'reads') order by key loop
  env:=d->'reads'->name;a:=(env->>'text')::jsonb;
  if env->>'text' is null or env->>'signature' is null or a->>'type_name' is distinct from reference->>'type_name'
   or a->>'object_id' is distinct from reference->>'object_id' or a->'fields' is distinct from reference->'fields'
   or a->>'operation' is distinct from 'read' or a->>'principal_id' is distinct from i->'binding'->>'subject_principal_id' then
   raise exception 'context v6 copy READ required' using errcode='42501';end if;
  perform authz.nexloop_role_strict_read(p_digest,p_world,env->>'text',env->>'signature');
 end loop;
 r:=authz.nexloop_read_local_artifact_v0060(p_digest,p_world,p_permit,p_payload);
 select claims into permit from authz.nexloop_artifact_permits where tenant_id=r->>'tenant_id' and permit_id=p_permit;
 if permit is null or permit->>'expires_at' is null or (permit->>'expires_at')::timestamptz<=clock_timestamp() then raise exception 'context read expired' using errcode='42501';end if;
 perform authz.nexloop_assert_artifact_authority(p_digest,p_world,permit);
 if not exists(select 1 from runtime.nexloop_context_artifact_bindings b where b.tenant_id=r->>'tenant_id' and b.world=p_world and b.artifact_id=r->>'artifact_id' and b.pack_digest=r->>'sha256'
   union all select 1 from runtime.nexloop_role_context_artifacts b where b.tenant_id=r->>'tenant_id' and b.world=p_world and b.artifact_id=r->>'artifact_id' and b.pack_digest=r->>'sha256')
  or r->>'retention_until' is null or (r->>'retention_until')::timestamptz<=clock_timestamp() then raise exception 'context artifact unavailable' using errcode='42501';end if;
 return r;
end $$;
alter function authz.nexloop_read_local_artifact(text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_read_local_artifact(text,text,text,text) from public,nexloop_scheduler,nexloop_action_worker,nexloop_identity;
grant execute on function authz.nexloop_read_local_artifact(text,text,text,text) to nexloop_api,nexloop_domain_worker;
