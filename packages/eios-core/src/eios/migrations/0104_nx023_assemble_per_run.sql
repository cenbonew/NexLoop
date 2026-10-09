-- NX-023 (temporary number): EXECUTE nexloop.context.assemble:1 issued with the Run.
-- The Source no longer needs a standing assemble grant: a Context strategy read or a v6
-- bind may instead carry run_assemble {run_id, run_digest} naming a Run credential that
-- this very Source credential issued (0034 source_digest), still active and unexpired in
-- this tenant and world, whose source directory is unchanged and whose runtime task (if
-- any) has not ended. A v6 bind additionally requires that Run to be the pack's own Run.
-- Expiry, revocation or the end of the Run, another Source's Run, another Run's binding,
-- another tenant or world: refused at the next use. Without run_assemble the standing
-- EIOS check is unchanged. Only the assemble check inside nexloop_context_read (0090),
-- nexloop_context_v6_command (0099 body) and nexloop_role_context_v6_command (0094 body)
-- is replaced by one helper; everything else in those bodies is identical.

create function authz.nexloop_assert_context_assemble(p_digest text,p_world text,p_claims jsonb,p_payload text) returns jsonb
language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare ident jsonb;r authz.nexloop_run_credentials;ra jsonb:=p_claims->'run_assemble';v_run text;
begin
 if not (p_claims ? 'run_assemble') then return authz.nexloop_assert_action_authority(p_digest,p_world,p_claims);end if;
 ident:=authz.nexloop_service_identity_snapshot(p_digest,p_world);
 if jsonb_typeof(ra) is distinct from 'object' or (select count(*) from jsonb_object_keys(ra))<>2
  or coalesce(ra->>'run_id','') !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' or coalesce(ra->>'run_digest','') !~ '^[a-f0-9]{64}$'
  or ident->'binding'->>'subject_kind' is distinct from 'service' or (ident->'run_context' is not null and ident->'run_context'<>'null'::jsonb)
  or p_claims->>'tenant_id' is distinct from ident->'binding'->>'tenant_id' or p_claims->>'principal_id' is distinct from ident->'binding'->>'subject_principal_id'
  or p_claims->>'credential_id' is distinct from ident->'binding'->>'credential_id' or p_claims->>'directory_hash' is distinct from ident->>'directory_hash'
  or p_claims->>'world' is distinct from p_world or p_claims->>'operation' is distinct from 'execute'
  or p_claims->>'resource_id' is distinct from 'eios:action:nexloop.context.assemble:1' or p_claims->>'action_resource' is distinct from 'eios:action:nexloop.context.assemble:1'
  or p_claims->'facts' is distinct from '[]'::jsonb
  or p_claims->>'expires_at' is null or (p_claims->>'expires_at')::timestamptz<=clock_timestamp() or (p_claims->>'expires_at')::timestamptz>clock_timestamp()+interval '25 seconds' then
  raise exception 'run assemble invalid' using errcode='42501';end if;
 -- A v6 bind may only use its own Run.
 if p_payload is not null then
  v_run:=((p_payload::jsonb->>'pack_text')::jsonb)->'bindings'->>'run_id';
  if v_run is distinct from ra->>'run_id' then raise exception 'run assemble other Run' using errcode='42501';end if;
 end if;
 perform set_config('eios.tenant_id',ident->'binding'->>'tenant_id',true);
 select * into r from authz.nexloop_run_credentials where token_digest=ra->>'run_digest' for share;
 if not found or r.run_id::text is distinct from ra->>'run_id' or r.source_digest is distinct from p_digest or r.world is distinct from p_world
  or r.status is distinct from 'active' or r.expires_at<=clock_timestamp() or (p_claims->>'expires_at')::timestamptz>r.expires_at
  or r.source_directory_hash is distinct from ident->>'directory_hash' then
  raise exception 'run assemble unavailable' using errcode='42501';end if;
 -- The Run has not ended: its runtime task, once enrolled, is not terminal.
 if exists(select 1 from authz.nexloop_runtime_run_bindings b join runtime.jobs j on j.tenant_id=b.tenant_id and j.job_id=b.task_id
   where b.run_id=r.run_id and j.status in ('succeeded','failed','dead_lettered')) then
  raise exception 'run assemble ended' using errcode='42501';end if;
 return ident->'binding';
end $$;
alter function authz.nexloop_assert_context_assemble(text,text,jsonb,text) owner to nexloop_owner;
revoke all on function authz.nexloop_assert_context_assemble(text,text,jsonb,text) from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity,nexloop_configurator;

-- Strategy read (0090 body; assemble check via the helper).
create or replace function authz.nexloop_context_read(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
 returns jsonb language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare a jsonb:=p_text::jsonb;c jsonb:=p_payload::jsonb;k bytea;v_verb text:=c->>'verb';ident jsonb;v_tenant text;
 v_result jsonb;v_row control.nexloop_context_strategies%rowtype;proof jsonb;ref text;gate text;v_type text;v_prop text;v_value text;
 v_def jsonb;v_version integer;v_items jsonb:='[]'::jsonb;v_props jsonb;v_consumer text;
begin
 if session_user not in ('nexloop_api','nexloop_domain_worker') or octet_length(p_text)>4194304 or octet_length(p_payload)>65536
  or a->>'protocol' is distinct from 'nexloop-context-read-v1' or v_verb not in ('strategy','definitions','open_work') or v_verb is null
  or a->>'parameters_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex') then
  raise exception 'context read unavailable' using errcode='42501';end if;
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-context-read-v1:'||p_text,'UTF8'),k,'sha256'),'hex') then
  raise exception 'context read unavailable' using errcode='42501';end if;
 ident:=authz.nexloop_service_identity_snapshot(p_digest,p_world);v_tenant:=ident->'binding'->>'tenant_id';
 if a->>'tenant_id' is distinct from v_tenant or a->>'world' is distinct from p_world then raise exception 'context read unavailable' using errcode='42501';end if;
 if v_verb='strategy' then
  if a->>'action_resource' is distinct from 'eios:action:nexloop.context.assemble:1' or c->>'strategy_id' !~ '^[a-z][a-z0-9_]{0,63}$'
   or (c ? 'version' and jsonb_typeof(c->'version') is distinct from 'number') then raise exception 'context read unavailable' using errcode='42501';end if;
  perform authz.nexloop_assert_context_assemble(p_digest,p_world,a,null);
  select * into v_row from control.nexloop_context_strategies where tenant_id=v_tenant and world=p_world and strategy_id=c->>'strategy_id'
   and (not c ? 'version' or version=(c->>'version')::integer) order by version desc limit 1;
  if not found then v_result:=null;else
   v_result:=jsonb_build_object('strategy_ref','context-strategy:'||v_row.strategy_id||'@'||v_row.version,'definition',v_row.definition,
    'definition_digest',v_row.definition_digest,'published_at',v_row.published_at);end if;
  perform authz.nexloop_assert_context_assemble(p_digest,p_world,a,null);
  return v_result;
 end if;
 if jsonb_typeof(a->'read_proofs') is distinct from 'array' or jsonb_array_length(a->'read_proofs')>256 then raise exception 'context read unavailable' using errcode='42501';end if;
 -- Every proof must be the caller's own, current, and for exactly the resource it names.
 for proof in select value from jsonb_array_elements(a->'read_proofs') loop
  if proof->>'tenant_id' is distinct from v_tenant or proof->>'principal_id' is distinct from ident->'binding'->>'subject_principal_id'
   or proof->>'operation' is distinct from 'read' or proof->>'expires_at' is null or (proof->>'expires_at')::timestamptz<=clock_timestamp() then
   raise exception 'context read proof invalid' using errcode='42501';end if;
  perform authz.nexloop_assert_read_authority(p_digest,p_world,proof);
 end loop;
 perform set_config('eios.tenant_id',v_tenant,true);
 if v_verb='definitions' then
  if jsonb_typeof(c->'refs') is distinct from 'array' or jsonb_array_length(c->'refs') not between 1 and 64 then raise exception 'context read unavailable' using errcode='42501';end if;
  for ref in select value#>>'{}' from jsonb_array_elements(c->'refs') loop
   v_type:=null;v_prop:=null;v_value:=null;v_props:=null;
   if ref ~ '^eios:object_type:[A-Za-z][A-Za-z0-9_]{0,63}$' then v_type:=substring(ref from 18);
   elsif ref ~ '^eios:property:[A-Za-z][A-Za-z0-9_]{0,63}/[A-Za-z][A-Za-z0-9_]{0,63}$' then
    v_type:=split_part(substring(ref from 15),'/',1);v_prop:=split_part(substring(ref from 15),'/',2);
   elsif ref ~ '^nexloop:vocabulary:[A-Za-z][A-Za-z0-9_]{0,63}/[A-Za-z][A-Za-z0-9_]{0,63}/.+$' then
    v_type:=split_part(substring(ref from 20),'/',1);v_prop:=split_part(substring(ref from 20),'/',2);
    v_value:=substring(substring(ref from 20) from length(v_type)+length(v_prop)+3);
   else raise exception 'context definition ref invalid' using errcode='22023';end if;
   -- Gates: the type always; the property for property/vocabulary refs (same as NX-021 recall gates).
   foreach gate in array array_remove(array['eios:object_type:'||v_type,case when v_prop is null then null else 'eios:property:'||v_type||'/'||v_prop end],null) loop
    if not exists(select 1 from jsonb_array_elements(a->'read_proofs') q where q->>'target_resource'=gate and q->>'resource_id'=gate) then
     raise exception 'context definition READ required' using errcode='42501';end if;
   end loop;
   select definition,version into v_def,v_version from ontology.object_type_versions where tenant_id=v_tenant and type_name=v_type order by version desc limit 1;
   if not found then raise exception 'context definition unavailable' using errcode='42501';end if;
   if v_prop is null then
    -- A type view lists only the properties the caller may READ.
    select coalesce(jsonb_agg(p->>'property_name' order by p->>'property_name'),'[]'::jsonb) into v_props from jsonb_array_elements(v_def->'properties') p
     where exists(select 1 from jsonb_array_elements(a->'read_proofs') q where q->>'target_resource'='eios:property:'||v_type||'/'||(p->>'property_name'));
    v_items:=v_items||jsonb_build_array(jsonb_build_object('ref',ref,'kind','object_type','schema_version',v_version,
     'display_name',v_def->>'display_name','description',coalesce(v_def->>'description',''),'readable_properties',v_props,
     'aliases',(select coalesce(jsonb_agg(x.alias_text order by x.alias_text),'[]'::jsonb) from ontology.nexloop_definition_aliases x where x.tenant_id=v_tenant and x.world=p_world and x.canonical_ref=ref)));
   else
    select value into v_props from jsonb_array_elements(v_def->'properties') where value->>'property_name'=v_prop;
    if v_props is null then raise exception 'context definition unavailable' using errcode='42501';end if;
    if v_value is not null and not coalesce(v_props->'type_descriptor'->'enum','[]'::jsonb) ? replace(replace(replace(replace(replace(replace(replace(v_value,'%20',' '),'%09',chr(9)),'%0A',chr(10)),'%0D',chr(13)),'%0C',chr(12)),'%0B',chr(11)),'%25','%') then
     raise exception 'context definition unavailable' using errcode='42501';end if;
    v_items:=v_items||jsonb_build_array(jsonb_build_object('ref',ref,'kind',case when v_value is null then 'property' else 'vocabulary_value' end,
     'schema_version',v_version,'property_ref','eios:property:'||v_type||'/'||v_prop,'display_name',v_props->>'display_name','description',coalesce(v_props->>'description',''),
     'value_type',v_props->>'value_type','closed_vocabulary',coalesce(v_props->'type_descriptor'->'enum','[]'::jsonb),
     'group',(select g->>'group_name' from jsonb_array_elements(coalesce(v_def->'property_groups','[]'::jsonb)) g where g->'property_names' ? v_prop limit 1),
     'aliases',(select coalesce(jsonb_agg(x.alias_text order by x.alias_text),'[]'::jsonb) from ontology.nexloop_definition_aliases x where x.tenant_id=v_tenant and x.world=p_world and x.canonical_ref=ref)));
   end if;
  end loop;
  v_result:=jsonb_build_object('items',v_items);
 else
  v_consumer:=c->>'consumer_id';
  if v_consumer !~ '^[0-9a-f]{64}$' or v_consumer is null or not exists(select 1 from jsonb_array_elements(a->'read_proofs') q
    where q->>'target_resource'='eios:object:Consumer/'||v_consumer and q->>'resource_id'='eios:object:Consumer/'||v_consumer) then
   raise exception 'context open work requires Consumer READ' using errcode='42501';end if;
  if not exists(select 1 from ontology.objects where tenant_id=v_tenant and world=p_world and type_name='Consumer' and object_id=v_consumer) then
   raise exception 'context consumer unavailable' using errcode='42501';end if;
  v_result:=jsonb_build_object(
   'intents',(select coalesce(jsonb_agg(jsonb_build_object('intent_id',i.intent_id::text,'action',i.action_name||':'||i.action_version,'state',i.state,
      'created_at',i.created_at) order by i.created_at,i.intent_id),'[]'::jsonb) from runtime.nexloop_effect_intents i
     where i.tenant_id=v_tenant and i.world=p_world and i.consumer_id=v_consumer and i.state in ('accepted','dispatching','unknown')),
   'outbound',(select coalesce(jsonb_agg(jsonb_build_object('intent_id',o.intent_id::text,'conversation_id',o.conversation_id,'delivery_state',o.delivery_state,
      'persisted_at',o.persisted_at,'delivery_changed_at',o.delivery_changed_at,'message_id',o.message_id,'body_digest',o.body_digest)
      order by o.persisted_at,o.intent_id),'[]'::jsonb) from runtime.nexloop_outbound_messages o join runtime.nexloop_conversations cv
      on cv.tenant_id=o.tenant_id and cv.world=o.world and cv.conversation_id=o.conversation_id
     where o.tenant_id=v_tenant and o.world=p_world and cv.consumer_id=v_consumer));
 end if;
 for proof in select value from jsonb_array_elements(a->'read_proofs') loop
  perform authz.nexloop_assert_read_authority(p_digest,p_world,proof);
 end loop;
 return v_result;
end $$;

-- Message Run v6 bind (0099 body; assemble check via the helper, Run = the pack's Run).
create or replace function authz.nexloop_context_v6_command(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
returns jsonb language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
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
 perform authz.nexloop_assert_context_assemble(p_digest,p_world,a,p_payload);
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
 perform authz.nexloop_assert_context_assemble(p_digest,p_world,a,p_payload);
 return jsonb_build_object('context_id',context_uuid,'run_id',run,'pack_digest',p->>'pack_digest','strategy_ref',pack->>'strategy_ref','bound',true);
end $$;

-- Role Run v6 bind (0094 body; assemble check via the helper, Run = the pack's Run).
create or replace function authz.nexloop_role_context_v6_command(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
returns jsonb language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare a jsonb:=p_text::jsonb;p jsonb;k bytea;ident jsonb;tenant text;principal text;ca jsonb;cp jsonb;snapshot jsonb;pack jsonb;
 b runtime.nexloop_role_context_artifacts;artifact runtime.nexloop_local_artifacts;proof jsonb;key text;consumer text;fresh boolean;namespace text;
 context_uuid uuid;run uuid;binding jsonb;core jsonb;
begin
 if session_user<>'nexloop_api' or p_world is distinct from 'real' or octet_length(p_text)>1048576 or octet_length(p_payload)>1048576
  or a->>'protocol' is distinct from 'nexloop-context-v6-v1' or a->>'resource_id' is distinct from 'eios:action:nexloop.context.assemble:1'
  or a->>'parameters_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex')
  or a->>'expires_at' is null or (a->>'expires_at')::timestamptz<=clock_timestamp() or (a->>'expires_at')::timestamptz>clock_timestamp()+interval '25 seconds' then
  raise exception 'context v6 unavailable' using errcode='42501';end if;
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-context-v6-v1:'||p_text,'UTF8'),k,'sha256'),'hex') then
  raise exception 'context v6 unavailable' using errcode='42501';end if;
 perform authz.nexloop_assert_context_assemble(p_digest,p_world,a,p_payload);
 ident:=authz.nexloop_service_identity_snapshot(p_digest,p_world);
 if ident->'run_context' is distinct from 'null'::jsonb or ident->'binding'->>'subject_kind' is distinct from 'service' then raise exception 'context v6 unavailable' using errcode='42501';end if;
 tenant:=ident->'binding'->>'tenant_id';principal:=ident->'binding'->>'subject_principal_id';perform set_config('eios.tenant_id',tenant,true);
 p:=p_payload::jsonb;
 if p->>'verb' is distinct from 'bind' or jsonb_typeof(p->'core') is distinct from 'object' or p->>'pack_text' is null or octet_length(p->>'pack_text')>65536 then
  raise exception 'context v6 unavailable' using errcode='42501';end if;
 ca:=(p->'core'->>'text')::jsonb;cp:=(p->'core'->>'payload')::jsonb;
 if cp->>'verb' is distinct from 'snapshot' then raise exception 'context v6 unavailable' using errcode='42501';end if;
 snapshot:=authz.nexloop_role_context_command(p_digest,p_world,p->'core'->>'text',p->'core'->>'signature',p->'core'->>'payload');
 pack:=(p->>'pack_text')::jsonb;
 if p->>'pack_digest' is distinct from encode(sha256(convert_to(p->>'pack_text','UTF8')),'hex') or runtime.nexloop_canonical_json(pack) is distinct from p->>'pack_text'
  or pack->>'schema_version' is distinct from 'nexloop.context-pack.v6' or (select count(*) from jsonb_object_keys(pack))<>18
  or not pack ?& array['strategy_ref','bindings','role','current_event','user_statement','goal','formal_facts','current_constraints','supply','constraints','consumer_state',
   'open_work','evidence','semantics','experience','budget_report','insufficient'] then raise exception 'context v6 pack invalid' using errcode='42501';end if;
 foreach key in array array['bindings','formal_facts','current_constraints','supply'] loop
  if pack->key is distinct from snapshot->key then raise exception 'context v6 core mismatch' using errcode='42501';end if;
 end loop;
 if pack->'role' is distinct from jsonb_build_object('role_binding',snapshot->'role_binding','role_policy',coalesce(snapshot->'role_policy','null'::jsonb)) then
  raise exception 'context v6 Role section mismatch' using errcode='42501';end if;
 if pack->'current_event' is distinct from snapshot->'trigger_statement' or pack->'user_statement' is distinct from 'null'::jsonb then
  raise exception 'context v6 current event mismatch' using errcode='42501';end if;
 run:=(pack->'bindings'->>'run_id')::uuid;context_uuid:=runtime.nexloop_role_pack_context_id(run);consumer:=snapshot->'role_binding'->'binding'->>'consumer_id';
 if consumer is null or not exists(select 1 from jsonb_array_elements(pack->'formal_facts') f where f->>'type'='Consumer' and f->>'id'=consumer) then
  raise exception 'context v6 consumer mismatch' using errcode='42501';end if;
 binding:=authz.nexloop_context_command_binding(cp->'command');
 select * into b from runtime.nexloop_role_context_artifacts where run_id=run for update;
 fresh:=not found;
 if not fresh and (b.pack_text is distinct from p->>'pack_text' or b.command_binding is distinct from binding or b.tenant_id is distinct from tenant) then
  raise exception 'role Artifact conflict' using errcode='23505';end if;
 if fresh then
  -- Role trigger is the current event; Role binding and policy are governed policy provenance.
  select jsonb_build_array(
    jsonb_build_object('section','current_event','ref',pack->'current_event'->>'provenance','revision','1','hash',runtime.nexloop_context_hash(pack->'current_event'),
     'kind','current_message','decision',runtime.nexloop_context_hash(ca->'role_envelope')),
    jsonb_build_object('section','role','ref',snapshot->'role_binding'->'binding'->>'role_ref','revision',snapshot->'role_binding'->'binding'->>'role_revision',
     'hash',runtime.nexloop_context_hash(pack->'role'),'kind','policy','decision',runtime.nexloop_context_hash(ca->'role_envelope')),
    jsonb_build_object('section','constraints','ref','eios:action:nexloop.service.request:1','revision',runtime.nexloop_context_hash(pack->'current_constraints'),
     'hash',runtime.nexloop_context_hash(pack->'current_constraints'),'kind','policy','decision',runtime.nexloop_context_hash(ca)),
    jsonb_build_object('section','constraints','ref','eios:object:'||(pack->'supply'->>'offering_id'),'revision',pack->'supply'->>'offering_revision',
     'hash',runtime.nexloop_context_hash(pack->'supply'),'kind','formal_object','decision',runtime.nexloop_context_hash(ca->'catalog_envelope')))
   ||coalesce((select jsonb_agg(jsonb_build_object('section','constraints','ref','eios:object:'||(f->>'type')||'/'||(f->>'id'),'revision',f->>'revision',
     'hash',runtime.nexloop_context_hash(f),'kind','formal_object','decision',runtime.nexloop_context_hash(ca->'formal_reads'->(f->>'type')))) from jsonb_array_elements(pack->'formal_facts') f),'[]'::jsonb)
   into core;
  perform authz.nexloop_context_v6_sections(p_digest,p_world,tenant,principal,a,p,pack,
   array(select cv.conversation_id from runtime.nexloop_conversations cv where cv.tenant_id=tenant and cv.world=p_world and cv.consumer_id=consumer),consumer,run,context_uuid,core);
 elsif not exists(select 1 from runtime.nexloop_context_packs c where c.tenant_id=tenant and c.world=p_world and c.context_id=context_uuid and c.run_id=run and c.pack_digest=p->>'pack_digest') then
  raise exception 'role Artifact conflict' using errcode='23505';
 end if;
 -- Artifact: same rules as the frozen Role bind, against the v6 bytes.
 if jsonb_typeof(ca->'artifact_proofs') is distinct from 'array' or jsonb_array_length(ca->'artifact_proofs')<>2
  or (select jsonb_agg(v->>'operation' order by v->>'operation') from jsonb_array_elements(ca->'artifact_proofs') t(v)) is distinct from '["create","read"]'::jsonb then
  raise exception 'Artifact CREATE/READ required' using errcode='42501';end if;
 for proof in select value from jsonb_array_elements(ca->'artifact_proofs') loop
  if proof->>'expires_at' is null or (proof->>'expires_at')::timestamptz<=clock_timestamp() then raise exception 'Artifact proof expired' using errcode='42501';end if;
  perform authz.nexloop_assert_artifact_authority(p_digest,p_world,proof);
 end loop;
 namespace:=encode(sha256(convert_to(tenant,'UTF8')||decode('00','hex')||convert_to(p_world,'UTF8')),'hex');
 select * into artifact from runtime.nexloop_local_artifacts where tenant_id=tenant and world=p_world and artifact_id=pack->'bindings'->>'artifact_id' for share;
 if not found or artifact.artifact_id is distinct from p->>'artifact_id' or artifact.status is distinct from 'available' or artifact.principal_id is distinct from principal
  or artifact.sha256 is distinct from p->>'pack_digest' or artifact.size_bytes is distinct from octet_length(p->>'pack_text')
  or artifact.media_type is distinct from 'application/vnd.nexloop.context+json' or artifact.retention_until is null or artifact.retention_until<=clock_timestamp()
  or artifact.object_key is distinct from namespace||'/'||artifact.artifact_id then raise exception 'role Artifact unavailable' using errcode='42501';end if;
 if fresh then
  insert into runtime.nexloop_role_context_artifacts values(run,tenant,p_world,principal,p_digest,(pack->'bindings'->>'context_id')::uuid,artifact.artifact_id,namespace,p->>'pack_text',p->>'pack_digest',binding,clock_timestamp());
 end if;
 -- Tail: every authority used above is still current at return.
 for proof in select value from jsonb_array_elements(a->'read_proofs') loop
  if (proof->>'expires_at')::timestamptz<=clock_timestamp() then raise exception 'context read proof expired' using errcode='42501';end if;
  perform authz.nexloop_assert_read_authority(p_digest,p_world,proof);
 end loop;
 for proof in select value from jsonb_array_elements(ca->'artifact_proofs') loop perform authz.nexloop_assert_artifact_authority(p_digest,p_world,proof);end loop;
 perform authz.nexloop_assert_context_assemble(p_digest,p_world,a,p_payload);
 return jsonb_build_object('context_id',context_uuid,'run_id',run,'pack_digest',p->>'pack_digest','strategy_ref',pack->>'strategy_ref','bound',true);
end $$;
