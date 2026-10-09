-- NX-023-C: Context v6 for Role Runs (frozen v3/v5 core + Engine sections).
--
-- * Role v6 = the Role chain's own SQL snapshot (bindings, formal facts, constraints,
--   supply, role binding, optional v5 policy, service trigger), re-derived here by the
--   unchanged Role command under its own signed envelope, plus the same strategy, goal,
--   control and labelled item sections as message v6 (0092).
-- * Item sources are re-verified with the 0092 rules; Claim evidence of a Role Run may come
--   from any of the Consumer's conversations (a message Run keeps its one conversation).
-- * The 0092 guard request snapshot and the Manifest projection now find Role Run bindings.
-- * v3/v5 stay frozen; only the Role chain's version-independent checks see v6 packs.

-- 0092 item verifier generalized to a set of conversations; the 0092 entry delegates.
create function authz.nexloop_context_v6_source_in(p_tenant text,p_world text,p_conversations text[],p_consumer text,p_section text,p_item jsonb,p_proofs jsonb)
returns jsonb language plpgsql stable security definer set search_path=pg_catalog set row_security=on as $$
declare ref text:=p_item->>'ref';proof jsonb;x ontology.nexloop_claims;i runtime.nexloop_effect_intents;o runtime.nexloop_outbound_messages;
 obj ontology.objects;expected jsonb;kind text;sub text;revision text;tags text[]:='{}';prop text;
begin
 if jsonb_typeof(p_item) is distinct from 'object' or (select count(*) from jsonb_object_keys(p_item))<>10
  or not p_item ?& array['subsection','ref','revision','content','content_hash','evidence_kind','access_decision_ref','relevance_permille','at','tags']
  or jsonb_typeof(p_item->'relevance_permille') is distinct from 'number' or (p_item->>'relevance_permille')!~'^[0-9]{1,4}$' or (p_item->>'relevance_permille')::integer>1000
  or jsonb_typeof(p_item->'at') is distinct from 'string' or length(p_item->>'at')>64 or jsonb_typeof(p_item->'tags') is distinct from 'array'
  or jsonb_typeof(p_item->'revision') is distinct from 'string' then raise exception 'context source shape invalid' using errcode='42501';end if;
 if p_item->>'content_hash' is distinct from runtime.nexloop_context_hash(p_item->'content') then raise exception 'context source hash mismatch' using errcode='42501';end if;
 proof:=p_proofs->(p_item->>'access_decision_ref');
 if proof is null then raise exception 'context source decision unknown' using errcode='42501';end if;
 if ref ~ '^claim:[0-9a-f]{64}$' then
  -- AT-064: a Claim is evidence only; never formal state, never a planning fact.
  if p_section<>'evidence' then raise exception 'context Claim outside evidence' using errcode='42501';end if;
  select * into x from ontology.nexloop_claims where tenant_id=p_tenant and world=p_world and claim_id=substr(ref,7) and conversation_id=any(p_conversations);
  if not found then raise exception 'context Claim unavailable' using errcode='42501';end if;
  if proof->>'target_resource' is distinct from 'eios:object:Conversation/'||x.conversation_id then raise exception 'context source decision mismatch' using errcode='42501';end if;
  if x.epistemic_kind='hypothesis' then
   -- Real-world strategies cannot include hypotheses (0088); nothing else may carry one.
   raise exception 'context hypothesis excluded' using errcode='42501';
  end if;
  if x.source_message_id is null or x.resolution_state not in ('unresolved','needs_resolution','awaiting_definition','resolved') then
   raise exception 'context Claim not admissible evidence' using errcode='42501';end if;
  sub:='claim_evidence';revision:=x.extractor_version;kind:=case when x.speaker='consumer' then 'user_statement' else 'conversation' end;
  -- Original text only: no structured value can act as a formal property.
  expected:=jsonb_build_object('quote',x.quote,'message_ref','eios:object:Message/'||x.source_message_id,'span',jsonb_build_array(x.span_start,x.span_end),
   'speaker',x.speaker,'epistemic_kind',x.epistemic_kind,'resolution_state',x.resolution_state,'modality',x.modality,'condition',x.condition_text);
  if x.polarity='negated' then tags:=tags||'negation'::text;end if;
  if x.epistemic_kind='constraint' then tags:=tags||'contact_limit'::text;end if;
 elsif ref ~ '^nexloop:intent:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' then
  if p_section<>'open_work' then raise exception 'context execution state outside open work' using errcode='42501';end if;
  select * into i from runtime.nexloop_effect_intents where intent_id=substr(ref,16)::uuid and tenant_id=p_tenant and world=p_world and consumer_id=p_consumer;
  if not found or i.state not in ('accepted','dispatching','unknown') then raise exception 'context intent unavailable' using errcode='42501';end if;
  if proof->>'target_resource' is distinct from 'eios:object:Consumer/'||p_consumer then raise exception 'context source decision mismatch' using errcode='42501';end if;
  sub:='intents';revision:=i.state;kind:='execution_state';tags:=array['unconfirmed'];
  expected:=jsonb_build_object('action',i.action_name||':'||i.action_version,'state',i.state,'since',i.created_at);
 elsif ref ~ '^nexloop:outbound:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' then
  if p_section<>'open_work' then raise exception 'context execution state outside open work' using errcode='42501';end if;
  select o2.* into o from runtime.nexloop_outbound_messages o2 join runtime.nexloop_conversations cv on cv.tenant_id=o2.tenant_id and cv.world=o2.world and cv.conversation_id=o2.conversation_id
   where o2.tenant_id=p_tenant and o2.world=p_world and o2.intent_id=substr(ref,18)::uuid and cv.consumer_id=p_consumer;
  -- Channel-accepted replies are conversation, never unconfirmed execution (ADR-020 §2).
  if not found or o.delivery_state in ('provider_accepted','delivered') then raise exception 'context outbound unavailable' using errcode='42501';end if;
  if proof->>'target_resource' is distinct from 'eios:object:Consumer/'||p_consumer then raise exception 'context source decision mismatch' using errcode='42501';end if;
  sub:='outbound';revision:=o.delivery_state;kind:='execution_state';
  if o.delivery_state in ('persisted','dispatching','unknown') then tags:=array['unconfirmed'];end if;
  expected:=jsonb_build_object('delivery_state',o.delivery_state,'since',o.delivery_changed_at,'note','not confirmed as delivered to the consumer');
 elsif ref ~ '^eios:object:Consumer/[0-9a-f]{64}$' then
  if p_section not in ('constraints','consumer_state') or substr(ref,22) is distinct from p_consumer then raise exception 'context formal object outside formal state' using errcode='42501';end if;
  select * into obj from ontology.objects where tenant_id=p_tenant and world=p_world and type_name='Consumer' and object_id=p_consumer;
  if not found or proof->>'target_resource' is distinct from 'eios:object:Consumer/'||p_consumer then raise exception 'context source decision mismatch' using errcode='42501';end if;
  if jsonb_typeof(p_item->'content'->'properties') is distinct from 'object' then raise exception 'context formal projection invalid' using errcode='42501';end if;
  -- Projection only: every shown property is current and was itself READ-authorized.
  for prop in select jsonb_object_keys(p_item->'content'->'properties') loop
   if not obj.properties ? prop or not exists(select 1 from jsonb_each(p_proofs) q where q.value->>'target_resource'='eios:property:Consumer/'||p_consumer||'/'||prop) then
    raise exception 'context formal property unreadable' using errcode='42501';end if;
  end loop;
  sub:='Consumer';revision:=obj.nexloop_revision::text;kind:='formal_object';
  expected:=jsonb_build_object('type','Consumer','properties',(select coalesce(jsonb_object_agg(k,obj.properties->k),'{}'::jsonb) from jsonb_object_keys(p_item->'content'->'properties') k));
 else
  raise exception 'context source kind unsupported' using errcode='42501';
 end if;
 if p_item->>'subsection' is distinct from sub or p_item->>'evidence_kind' is distinct from kind or p_item->>'revision' is distinct from revision
  or p_item->'content' is distinct from expected then raise exception 'context source content mismatch' using errcode='42501';end if;
 return to_jsonb(array(select t from unnest(tags) t order by t));
end $$;
alter function authz.nexloop_context_v6_source_in(text,text,text[],text,text,jsonb,jsonb) owner to nexloop_owner;
revoke all on function authz.nexloop_context_v6_source_in(text,text,text[],text,text,jsonb,jsonb) from public;
create or replace function authz.nexloop_context_v6_source(p_tenant text,p_world text,p_conversation text,p_consumer text,p_section text,p_item jsonb,p_proofs jsonb)
returns jsonb language sql stable security definer set search_path=pg_catalog set row_security=on as $$
 select authz.nexloop_context_v6_source_in(p_tenant,p_world,array[p_conversation],p_consumer,p_section,p_item,p_proofs) $$;

-- Conversations of one Consumer (ids only), under the caller's current Consumer READ. Role
-- Runs gather Claim evidence across them; each Claim read still needs Conversation READ.
create function authz.nexloop_context_consumer_conversations(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare a jsonb:=p_text::jsonb;c jsonb:=p_payload::jsonb;k bytea;ident jsonb;v_tenant text;proof jsonb;v_consumer text:=c->>'consumer_id';v_result jsonb;
begin
 if session_user not in ('nexloop_api','nexloop_domain_worker') or p_world is distinct from 'real' or octet_length(p_text)>1048576 or octet_length(p_payload)>4096
  or a->>'protocol' is distinct from 'nexloop-context-read-v1' or a->>'parameters_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex')
  or c->>'verb' is distinct from 'conversations' or v_consumer is null or v_consumer!~'^[0-9a-f]{64}$'
  or jsonb_typeof(a->'read_proofs') is distinct from 'array' or jsonb_array_length(a->'read_proofs')<>1 then raise exception 'context read unavailable' using errcode='42501';end if;
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-context-read-v1:'||p_text,'UTF8'),k,'sha256'),'hex') then raise exception 'context read unavailable' using errcode='42501';end if;
 ident:=authz.nexloop_service_identity_snapshot(p_digest,p_world);v_tenant:=ident->'binding'->>'tenant_id';
 proof:=a->'read_proofs'->0;
 if proof->>'tenant_id' is distinct from v_tenant or proof->>'principal_id' is distinct from ident->'binding'->>'subject_principal_id' or proof->>'operation' is distinct from 'read'
  or proof->>'target_resource' is distinct from 'eios:object:Consumer/'||v_consumer or proof->>'resource_id' is distinct from proof->>'target_resource'
  or proof->>'expires_at' is null or (proof->>'expires_at')::timestamptz<=clock_timestamp() then raise exception 'context open work requires Consumer READ' using errcode='42501';end if;
 perform authz.nexloop_assert_read_authority(p_digest,p_world,proof);
 perform set_config('eios.tenant_id',v_tenant,true);
 select jsonb_build_object('conversations',coalesce(jsonb_agg(conversation_id order by conversation_id),'[]'::jsonb)) into v_result
  from runtime.nexloop_conversations where tenant_id=v_tenant and world=p_world and consumer_id=v_consumer;
 perform authz.nexloop_assert_read_authority(p_digest,p_world,proof);
 return v_result;
end $$;
alter function authz.nexloop_context_consumer_conversations(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_context_consumer_conversations(text,text,text,text,text) from public,nexloop_action_worker,nexloop_scheduler,nexloop_identity;
grant execute on function authz.nexloop_context_consumer_conversations(text,text,text,text,text) to nexloop_api,nexloop_domain_worker;

-- First bind of a v6 pack: everything beyond its frozen core (the 0092 message rules, shared).
-- p_core_sources: [{section,ref,revision,hash,kind,decision}] the caller derived from its core.
create function authz.nexloop_context_v6_sections(p_digest text,p_world text,p_tenant text,p_principal text,a jsonb,p jsonb,pack jsonb,
 p_conversations text[],p_consumer text,p_run uuid,p_context uuid,p_core_sources jsonb) returns void
language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare strat control.nexloop_context_strategies;goal_fact jsonb;control jsonb;proofs jsonb:='{}'::jsonb;proof jsonb;item jsonb;section text;
 ordinal integer:=0;tags jsonb;omitted jsonb;n integer;required record;
 sections constant text[]:=array['constraints','consumer_state','open_work','evidence','semantics','experience'];
begin
 -- Strategy: the current published version only.
 if pack->>'strategy_ref' !~ '^context-strategy:[a-z][a-z0-9_]{0,63}@[1-9][0-9]{0,6}$' then raise exception 'context v6 strategy invalid' using errcode='42501';end if;
 select * into strat from control.nexloop_context_strategies where tenant_id=p_tenant and world=p_world and strategy_id=split_part(substr(pack->>'strategy_ref',18),'@',1)
  and version=split_part(pack->>'strategy_ref','@',2)::integer;
 if not found or exists(select 1 from control.nexloop_context_strategies s where s.tenant_id=p_tenant and s.world=p_world and s.strategy_id=strat.strategy_id and s.version>strat.version)
  or strat.definition->'include_hypotheses' is distinct from 'false'::jsonb then raise exception 'context v6 strategy not current' using errcode='42501';end if;
 if jsonb_typeof(pack->'budget_report') is distinct from 'object'
  or (pack->'budget_report'->'input_token_budget',pack->'budget_report'->'output_reserve',pack->'budget_report'->'framing_reserve')
   is distinct from (strat.definition->'input_token_budget',strat.definition->'output_reserve',strat.definition->'framing_reserve') then
  raise exception 'context v6 budget mismatch' using errcode='42501';end if;
 -- Goal: the bound formal Goal revision; control snapshot recomputed now (NX-022), or declared insufficient.
 select value into goal_fact from jsonb_array_elements(pack->'formal_facts') where value->>'type'='Goal';
 if jsonb_typeof(pack->'goal') is distinct from 'object' or (select count(*) from jsonb_object_keys(pack->'goal'))<>2
  or pack->'goal'->'goal_version_refs' is distinct from jsonb_build_array('goal:'||(goal_fact->>'id')||'@'||(goal_fact->>'revision')) then
  raise exception 'context v6 goal mismatch' using errcode='42501';end if;
 control:=pack->'goal'->'control_snapshot';
 if control is null or control='null'::jsonb then
  if not exists(select 1 from jsonb_array_elements(pack->'insufficient') x where x->>'code' in ('control_paused','goal_not_current')) then
   raise exception 'context v6 control snapshot required' using errcode='42501';end if;
 elsif control is distinct from authz.nexloop_control_snapshot(p_digest,p_world,jsonb_build_object('scopes',jsonb_build_array(jsonb_build_object('kind','consumer','ref','consumer:'||p_consumer)),
   'goals','[]'::jsonb,'objects',jsonb_build_array(jsonb_build_object('type_name','Consumer','object_id',p_consumer)),'budgets',jsonb_build_array('model'))) then
  raise exception 'context v6 control snapshot stale' using errcode='42501';
 end if;
 -- Read proofs: caller's own, current, each addressed by the hash the items cite.
 if jsonb_typeof(a->'read_proofs') is distinct from 'array' or jsonb_array_length(a->'read_proofs')>256 then raise exception 'context v6 unavailable' using errcode='42501';end if;
 for proof in select value from jsonb_array_elements(a->'read_proofs') loop
  if proof->>'tenant_id' is distinct from p_tenant or proof->>'principal_id' is distinct from p_principal or proof->>'operation' is distinct from 'read'
   or proof->>'expires_at' is null or (proof->>'expires_at')::timestamptz<=clock_timestamp() then raise exception 'context read proof invalid' using errcode='42501';end if;
  perform authz.nexloop_assert_read_authority(p_digest,p_world,proof);
  proofs:=proofs||jsonb_build_object('decision:'||runtime.nexloop_context_hash(proof),proof);
 end loop;
 insert into runtime.nexloop_context_packs(tenant_id,world,context_id,run_id,protocol,strategy_ref,pack_digest,artifact_id,budget_report,insufficient)
  values(p_tenant,p_world,p_context,p_run,'nexloop.context-pack.v6',pack->>'strategy_ref',p->>'pack_digest',pack->'bindings'->>'artifact_id',pack->'budget_report',pack->'insufficient');
 -- Core sources, then strategy and control.
 for item in select value from jsonb_array_elements(p_core_sources||jsonb_build_array(
   jsonb_build_object('section','bindings','ref',pack->>'strategy_ref','revision',strat.version::text,'hash',strat.definition_digest,'kind','policy','decision',runtime.nexloop_context_hash(a)),
   jsonb_build_object('section','goal','ref','nexloop:control:'||coalesce(control->>'control_revision','unavailable'),'revision',coalesce(control->>'control_revision','0'),
    'hash',runtime.nexloop_context_hash(coalesce(control,'null'::jsonb)),'kind','policy','decision',runtime.nexloop_context_hash(a)))) loop
  insert into runtime.nexloop_context_sources values(p_tenant,p_world,p_context,ordinal,item->>'section',item->>'ref',item->>'revision',item->>'hash',item->>'kind','decision:'||(item->>'decision'),'included',null);
  ordinal:=ordinal+1;
 end loop;
 -- Item sections: every item re-verified against its source; tags are what the source requires.
 foreach section in array sections loop
  if jsonb_typeof(pack->section) is distinct from 'array' or jsonb_array_length(pack->section)>256 then raise exception 'context v6 pack invalid' using errcode='42501';end if;
  if (pack->'budget_report'->'sections'->section->>'included')::integer is distinct from jsonb_array_length(pack->section) then raise exception 'context v6 budget report mismatch' using errcode='42501';end if;
  for item in select value from jsonb_array_elements(pack->section) loop
   tags:=authz.nexloop_context_v6_source_in(p_tenant,p_world,p_conversations,p_consumer,section,item,proofs);
   if item->'tags' is distinct from tags then raise exception 'context v6 tags mismatch' using errcode='42501';end if;
   insert into runtime.nexloop_context_sources values(p_tenant,p_world,p_context,ordinal,section,item->>'ref',item->>'revision',item->>'content_hash',item->>'evidence_kind',item->>'access_decision_ref','included',null);
   ordinal:=ordinal+1;
  end loop;
 end loop;
 -- Omitted items are real sources too, and none of them may be pinned (AT-028).
 select count(*) into n from jsonb_array_elements(pack->'budget_report'->'omitted');
 if jsonb_typeof(p->'omitted_items') is distinct from 'array' or n<>jsonb_array_length(p->'omitted_items') then raise exception 'context v6 omission report mismatch' using errcode='42501';end if;
 for omitted in select value from jsonb_array_elements(p->'omitted_items') loop
  if not exists(select 1 from jsonb_array_elements(pack->'budget_report'->'omitted') r where r=jsonb_build_object('section',omitted->>'section','subsection',omitted->'item'->>'subsection','ref',omitted->'item'->>'ref','reason',omitted->>'reason'))
   or not (omitted->>'section')=any(sections) or exists(select 1 from jsonb_array_elements(pack->(omitted->>'section')) r where r->>'ref'=omitted->'item'->>'ref') then
   raise exception 'context v6 omission report mismatch' using errcode='42501';end if;
  tags:=authz.nexloop_context_v6_source_in(p_tenant,p_world,p_conversations,p_consumer,omitted->>'section',omitted->'item',proofs);
  if tags<>'[]'::jsonb or omitted->'item'->'tags' is distinct from tags then raise exception 'context v6 pinned source omitted' using errcode='42501';end if;
  insert into runtime.nexloop_context_sources values(p_tenant,p_world,p_context,ordinal,omitted->>'section',omitted->'item'->>'ref',omitted->'item'->>'revision',
   omitted->'item'->>'content_hash',omitted->'item'->>'evidence_kind',omitted->'item'->>'access_decision_ref','omitted',omitted->>'reason');
  ordinal:=ordinal+1;
 end loop;
 -- Every pinned source the database knows about is present, or its section is declared insufficient.
 for required in
  select 'evidence' section,'claim:'||x.claim_id ref from ontology.nexloop_claims x where x.tenant_id=p_tenant and x.world=p_world and x.conversation_id=any(p_conversations)
   and x.epistemic_kind<>'hypothesis' and x.source_message_id is not null and x.resolution_state in ('unresolved','needs_resolution','awaiting_definition','resolved')
   and (x.polarity='negated' or x.epistemic_kind='constraint')
  union all select 'open_work','nexloop:intent:'||i.intent_id from runtime.nexloop_effect_intents i where i.tenant_id=p_tenant and i.world=p_world and i.consumer_id=p_consumer and i.state in ('accepted','dispatching','unknown')
  union all select 'open_work','nexloop:outbound:'||o.intent_id from runtime.nexloop_outbound_messages o join runtime.nexloop_conversations cv
   on cv.tenant_id=o.tenant_id and cv.world=o.world and cv.conversation_id=o.conversation_id where o.tenant_id=p_tenant and o.world=p_world and cv.consumer_id=p_consumer and o.delivery_state in ('persisted','dispatching','unknown')
 loop
  if not exists(select 1 from jsonb_array_elements(pack->required.section) r where r->>'ref'=required.ref)
   and not exists(select 1 from jsonb_array_elements(pack->'insufficient') x where x->>'code'='required_source_unreadable' and x->>'section'=required.section) then
   raise exception 'context v6 pinned source missing' using errcode='42501';end if;
 end loop;
end $$;
alter function authz.nexloop_context_v6_sections(text,text,text,text,jsonb,jsonb,jsonb,text[],text,uuid,uuid,jsonb) owner to nexloop_owner;
revoke all on function authz.nexloop_context_v6_sections(text,text,text,text,jsonb,jsonb,jsonb,text[],text,uuid,uuid,jsonb) from public;

-- A Role Run's effect context is shared by every Run of its PlanStep, so its v6 Context
-- (Manifest context_id) is a per-Run id derived from the Run; message Runs keep theirs.
create function runtime.nexloop_role_pack_context_id(p_run uuid) returns uuid language sql immutable set search_path=pg_catalog as $$
 select substr(encode(sha256(convert_to('nexloop-role-context:'||p_run::text,'UTF8')),'hex'),1,32)::uuid $$;
alter function runtime.nexloop_role_pack_context_id(uuid) owner to nexloop_owner;
revoke all on function runtime.nexloop_role_pack_context_id(uuid) from public;

-- Role Run v6 bind: the frozen Role command (verb 'snapshot') re-derives the core; the v6
-- pack must carry it unchanged (role binding/policy under `role`, trigger as current_event).
create function authz.nexloop_role_context_v6_command(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
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
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
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
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 return jsonb_build_object('context_id',context_uuid,'run_id',run,'pack_digest',p->>'pack_digest','strategy_ref',pack->>'strategy_ref','bound',true);
end $$;
alter function authz.nexloop_role_context_v6_command(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_role_context_v6_command(text,text,text,text,text) from public,nexloop_identity,nexloop_domain_worker,nexloop_scheduler,nexloop_action_worker;
grant execute on function authz.nexloop_role_context_v6_command(text,text,text,text,text) to nexloop_api;

-- Bound v6 Context of a Run, message or Role (at most one exists per Run).
create function runtime.nexloop_context_v6_binding(p_tenant text,p_world text,p_run uuid) returns table(tenant_id text,world text,run_id uuid,context_id uuid,pack_text text)
language sql stable set search_path=pg_catalog as $$
 select b.tenant_id,b.world,b.run_id,b.context_id,b.pack_text from runtime.nexloop_context_artifact_bindings b where b.tenant_id=p_tenant and b.world=p_world and b.run_id=p_run
 union all
 select r.tenant_id,r.world,r.run_id,runtime.nexloop_role_pack_context_id(r.run_id),r.pack_text from runtime.nexloop_role_context_artifacts r
  where r.tenant_id=p_tenant and r.world=p_world and r.run_id=p_run and r.pack_text::jsonb->>'schema_version'='nexloop.context-pack.v6' $$;
alter function runtime.nexloop_context_v6_binding(text,text,uuid) owner to nexloop_owner;
revoke all on function runtime.nexloop_context_v6_binding(text,text,uuid) from public;

-- The 0092 guard request snapshot and Manifest projection, now for message and Role v6 Runs.
create or replace function authz.nexloop_runtime_activation_command(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare a jsonb:=p_text::jsonb;p jsonb:=p_payload::jsonb;result jsonb;s jsonb;req jsonb;command jsonb;b record;
 pack runtime.nexloop_context_packs;strat jsonb;existing runtime.nexloop_model_requests;prompt text;
begin
 if p ? 'request_snapshot' and (p->>'verb' is distinct from 'authorize' or p->>'operation' is distinct from 'model') then
  raise exception 'model request snapshot unexpected' using errcode='42501';end if;
 result:=authz.nexloop_runtime_activation_command_before_model_requests_v0090(p_digest,p_world,p_text,p_signature,p_payload);
 if not p ? 'request_snapshot' then return result;end if;
 command:=(p->>'command_text')::jsonb;perform set_config('eios.tenant_id',a->>'tenant_id',true);
 select * into b from runtime.nexloop_context_v6_binding(a->>'tenant_id',p_world,(command->>'run_id')::uuid);
 if not found or b.pack_text::jsonb->>'schema_version' is distinct from 'nexloop.context-pack.v6' then raise exception 'model request snapshot requires a v6 Context' using errcode='42501';end if;
 select * into pack from runtime.nexloop_context_packs where tenant_id=b.tenant_id and world=b.world and run_id=b.run_id and context_id=b.context_id;
 select definition into strat from control.nexloop_context_strategies where tenant_id=b.tenant_id and world=b.world
  and strategy_id=split_part(substr(pack.strategy_ref,18),'@',1) and version=split_part(pack.strategy_ref,'@',2)::integer;
 s:=p->'request_snapshot';
 if not found or jsonb_typeof(s) is distinct from 'object' or (select count(*) from jsonb_object_keys(s))<>7
  or not s ?& array['call_sequence','model_provider','model_id','request_text','request_digest','tool_manifest_digest','settings_digest']
  or jsonb_typeof(s->'call_sequence') is distinct from 'number' or s->>'call_sequence'!~'^[1-9][0-9]{0,4}$'
  or jsonb_typeof(s->'request_text') is distinct from 'string' or octet_length(s->>'request_text')>262144 then raise exception 'model request snapshot invalid' using errcode='42501';end if;
 prompt:=s->>'request_text';
 -- The digest is over the exact request bytes; tool/settings digests are recomputed from them.
 if s->>'request_digest' is distinct from encode(sha256(convert_to(prompt,'UTF8')),'hex') then raise exception 'model request digest mismatch' using errcode='42501';end if;
 req:=prompt::jsonb;
 if (select count(*) from jsonb_object_keys(req))<>3 or not req ?& array['model','context','settings']
  or req->'model'->>'provider' is distinct from s->>'model_provider' or req->'model'->>'id' is distinct from s->>'model_id'
  or s->>'tool_manifest_digest' is distinct from runtime.nexloop_context_hash(runtime.nexloop_tool_manifest(req->'context'))
  or s->>'settings_digest' is distinct from runtime.nexloop_context_hash(req->'settings') then raise exception 'model request snapshot mismatch' using errcode='42501';end if;
 -- Profile allowlist: the Run command's runtime profile fixes the provider.
 if (case command->>'runtime_profile' when 'deterministic-test' then 'faux' when 'deepseek-flash' then 'deepseek' end) is distinct from s->>'model_provider' then
  raise exception 'model provider not allowed for profile' using errcode='42501';end if;
 -- The request carries the bound pack as the Run input (the Context it claims to use).
 if not exists(select 1 from jsonb_array_elements(req->'context'->'messages') m where m->>'role'='user'
   and (m->>'content'=b.pack_text or exists(select 1 from jsonb_array_elements(case when jsonb_typeof(m->'content')='array' then m->'content' else '[]'::jsonb end) c where c->>'type'='text' and c->>'text'=b.pack_text))) then
  raise exception 'model request does not carry the bound Context' using errcode='42501';end if;
 select * into existing from runtime.nexloop_model_requests where tenant_id=b.tenant_id and world=b.world and run_id=b.run_id and call_sequence=(s->>'call_sequence')::integer;
 if found then
  -- Guard retry of the same call is idempotent; a different request under the same number is not.
  if (existing.request_digest,existing.model_provider,existing.model_id,existing.tool_manifest_digest,existing.settings_digest)
   is distinct from (s->>'request_digest',s->>'model_provider',s->>'model_id',s->>'tool_manifest_digest',s->>'settings_digest') then
   raise exception 'model request call_sequence conflict' using errcode='42501';end if;
  return result;
 end if;
 insert into runtime.nexloop_model_requests(tenant_id,world,run_id,call_sequence,context_id,model_provider,model_id,tool_manifest_digest,request_digest,
  prompt_artifact_id,input_token_budget,output_token_budget,settings_digest)
  values(b.tenant_id,b.world,b.run_id,(s->>'call_sequence')::integer,b.context_id,s->>'model_provider',s->>'model_id',s->>'tool_manifest_digest',s->>'request_digest',
  substr(encode(sha256(convert_to('prompt:'||b.run_id::text||':'||(s->>'call_sequence'),'UTF8')),'hex'),1,32),(strat->>'input_token_budget')::integer,(strat->>'output_reserve')::integer,s->>'settings_digest');
 insert into runtime.nexloop_model_request_prompts values(b.tenant_id,b.world,b.run_id,(s->>'call_sequence')::integer,
  substr(encode(sha256(convert_to('prompt:'||b.run_id::text||':'||(s->>'call_sequence'),'UTF8')),'hex'),1,32),prompt);
 return result;
end $$;

create or replace function runtime.nexloop_context_manifest(p_run uuid,p_call integer) returns jsonb language sql stable set search_path=pg_catalog as $$
 select jsonb_build_object('schema_version','1.0','context_id',r.context_id,'tenant_id',r.tenant_id,'world_id',r.world,'mode','real','run_id',r.run_id,
  'call_sequence',r.call_sequence,'goal_version_ref',b.pack_text::jsonb->'goal'->'goal_version_refs'->>0,
  'policy_revision','sha256:'||runtime.nexloop_context_hash(b.pack_text::jsonb->'current_constraints'),
  'ontology_schema_revision','sha256:'||runtime.nexloop_context_hash(coalesce((select jsonb_object_agg(v.type_name,v.version) from (select type_name,max(version) version
    from ontology.object_type_versions where tenant_id=r.tenant_id group by type_name) v),'{}'::jsonb)),
  'semantic_snapshot_ref','semantic:none','context_strategy_version',c.strategy_ref,'model_provider',r.model_provider,'model_id',r.model_id,'embedding_profile_ref',null,
  'sources',(select jsonb_agg(jsonb_build_object('ref',s.ref,'revision',s.revision,'content_hash',s.content_hash,'evidence_kind',s.evidence_kind,'access_decision_ref',s.access_decision_ref) order by s.ordinal)
    from runtime.nexloop_context_sources s where s.tenant_id=r.tenant_id and s.world=r.world and s.context_id=r.context_id and s.status='included'),
  'prompt_artifact_ref','prompt:'||r.prompt_artifact_id,'request_digest',r.request_digest,'input_token_budget',r.input_token_budget,'output_token_budget',r.output_token_budget,
  'redaction_policy_ref','redaction:none-v1','created_at',to_char(r.requested_at at time zone 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"'))
 from runtime.nexloop_model_requests r join runtime.nexloop_context_packs c on c.tenant_id=r.tenant_id and c.world=r.world and c.context_id=r.context_id
  cross join lateral runtime.nexloop_context_v6_binding(r.tenant_id,r.world,r.run_id) b
 where r.run_id=p_run and r.call_sequence=p_call $$;
