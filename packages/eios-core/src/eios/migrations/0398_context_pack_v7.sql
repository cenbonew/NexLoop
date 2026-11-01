-- K4c (owner 2026-11-01, Q1-Q7 as recommended, Q2: plan Runs first; design notes l2-k4c-context-v7 v2; temporary number 0398):
-- context-pack v7 = v6 plus one evidence subsection, human_answers: the latest revision of each answered question of the Run's
-- Consumer's plans (K4b, 0397), evidence_kind user_statement - a staff member's statement, never a formal fact. Plan Runs only (the
-- message and fallback binds stay v6 and refuse any other schema_version).
--   1. runtime.nexloop_context_human_answer_items(tenant, world, consumer): the expected items, derived here (question answered,
--      latest answer not a withdrawal, question text not erased, the plan's Consumer). access_decision_ref is the digest of the
--      derivation key (question, revision, Consumer): there is no read proof - SQL re-derives every item. authz.nexloop_context_human_answers
--      is the Source's read of them (service session, as the outreach section).
--   2. authz.nexloop_context_human_answer_in: an item is exactly one expected item; authz.nexloop_context_human_answers_complete:
--      every expected item is in the pack or omitted (section_quota only), at most 8 in the pack.
--   3. V1 (L6): one body for both versions. authz.nexloop_context_sections_core(p_protocol, ...) is 0396's
--      authz.nexloop_context_v6_sections body with two parameterised points only (the protocol written to the context pack; the
--      per-item check of a v7 human_answers item) and the v7 completeness call; authz.nexloop_role_context_command_core(p_protocol, ...)
--      is 0239's authz.nexloop_role_context_v6_command body with the schema_version check and the sections call parameterised.
--      Both v6 entry points are dropped and re-created as one-line calls of the core with 'nexloop.context-pack.v6' (same
--      signatures, owners and grants); authz.nexloop_role_context_v7_command calls it with v7. test_context_v7_pg checks that
--      each core, with those points reverted, is its source body exactly - a v6 rule changes in the core once, for both.
--   4. Q1 (deployment switch + strategy version): a strategy may declare protocol nexloop.context-pack.v7 (the shape check, 0088);
--      a v7 pack is bound only under such a strategy (checked with the completeness); a v6 pack may name it (the switch off).
--   5. A v7 plan Run's model requests are recorded like a v6 Run's: the guard's snapshot check (0100 body, renamed by 0171) and
--      runtime.nexloop_context_v6_binding (0110) accept v7 (copy guard).

-- 1. ---------------------------------------------------------------------------------------------------------------------------
create function runtime.nexloop_context_human_answer_items(p_tenant text,p_world text,p_consumer text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v jsonb;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 select coalesce(jsonb_agg(s.item order by s.item->>'at' desc,s.item->>'ref'),'[]'::jsonb) into v from (
  select jsonb_build_object('subsection','human_answers','ref','nexloop:human_answer:'||q.question_id||'@'||a.revision,'revision',a.revision::text,
    'content',cc.c,'content_hash',runtime.nexloop_context_hash(cc.c),'evidence_kind','user_statement',
    'access_decision_ref','decision:'||runtime.nexloop_context_hash(jsonb_build_object('human_answer',q.question_id,'revision',a.revision,'consumer',p_consumer)),
    'relevance_permille',1000,'at',cc.c->>'answered_at','tags','[]'::jsonb) item
  from runtime.nexloop_human_questions q
  cross join lateral (select x.* from runtime.nexloop_human_answers x where x.tenant_id=q.tenant_id and x.world=q.world and x.question_id=q.question_id
   order by x.revision desc limit 1) a
  cross join lateral (select jsonb_build_object('question_ref','nexloop:human_question:'||q.question_id,'question_kind',q.question_kind,'question_text',q.text,
    'answer',case a.answer_kind when 'option' then jsonb_build_object('kind','option','option_index',a.option_index,'option_text',q.options->>a.option_index)
      else jsonb_build_object('kind','text','text',a.text) end,
    'answerer_role',a.role,'answered_at',to_char(a.recorded_at at time zone 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
    'plan_ref','nexloop:plan:'||q.plan_id||'@'||q.plan_version) c) cc
  where q.tenant_id=p_tenant and q.world=p_world and q.state='answered' and q.text<>'' and a.answer_kind<>'withdraw'
   and (a.answer_kind<>'text' or coalesce(a.text,'')<>'')
   and exists(select 1 from runtime.nexloop_plans pl where pl.tenant_id=q.tenant_id and pl.world=q.world and pl.plan_id=q.plan_id and pl.consumer_id=p_consumer)
 ) s;
 return v;
end $$;
alter function runtime.nexloop_context_human_answer_items(text,text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_context_human_answer_items(text,text,text) from public;

create function authz.nexloop_context_human_answers(p_digest text,p_world text,p_consumer text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare ident jsonb;v_tenant text;
begin
 if session_user<>'nexloop_api' or p_world is distinct from 'real' or coalesce(p_consumer,'')!~'^[a-f0-9]{64}$' then
  raise exception 'human answers context unavailable' using errcode='42501';end if;
 ident:=authz.nexloop_service_identity_snapshot(p_digest,p_world);
 if ident->'binding'->>'subject_kind' is distinct from 'service' then raise exception 'human answers context unavailable' using errcode='42501';end if;
 v_tenant:=ident->'binding'->>'tenant_id';perform set_config('eios.tenant_id',v_tenant,true);
 return runtime.nexloop_context_human_answer_items(v_tenant,p_world,p_consumer);
end $$;
alter function authz.nexloop_context_human_answers(text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_context_human_answers(text,text,text) from public;
grant execute on function authz.nexloop_context_human_answers(text,text,text) to nexloop_api;

-- 2. ---------------------------------------------------------------------------------------------------------------------------
create function authz.nexloop_context_human_answer_in(p_tenant text,p_world text,p_consumer text,p_item jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
begin
 if p_item->>'evidence_kind' is distinct from 'user_statement' or coalesce(p_item->>'ref','') !~ '^nexloop:human_answer:[0-9a-f-]{36}@[1-9][0-9]{0,8}$'
  or not exists(select 1 from jsonb_array_elements(runtime.nexloop_context_human_answer_items(p_tenant,p_world,p_consumer)) e where e.value=p_item) then
  raise exception 'context v7 human answer mismatch' using errcode='42501';end if;
 return '[]'::jsonb;
end $$;
alter function authz.nexloop_context_human_answer_in(text,text,text,jsonb) owner to nexloop_owner;
revoke all on function authz.nexloop_context_human_answer_in(text,text,text,jsonb) from public;

create function authz.nexloop_context_human_answers_complete(p_tenant text,p_world text,p_consumer text,pack jsonb,p jsonb) returns void
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare e jsonb;
begin
 if (select count(*) from jsonb_array_elements(pack->'evidence') x where x->>'subsection'='human_answers')>8
  or exists(select 1 from jsonb_array_elements(p->'omitted_items') o where o->'item'->>'subsection'='human_answers' and o->>'reason' is distinct from 'section_quota')
  or exists(select 1 from jsonb_array_elements(pack->'evidence') x where x->>'ref' like 'nexloop:human_answer:%' and x->>'subsection' is distinct from 'human_answers')
  or exists(select 1 from (select jsonb_array_elements(pack->s) x from unnest(array['constraints','consumer_state','open_work','semantics','experience']) s) y
   where y.x->>'ref' like 'nexloop:human_answer:%' or y.x->>'subsection'='human_answers') then
  raise exception 'context v7 human answers invalid' using errcode='42501';end if;
 for e in select value from jsonb_array_elements(runtime.nexloop_context_human_answer_items(p_tenant,p_world,p_consumer)) loop
  if not exists(select 1 from jsonb_array_elements(pack->'evidence') x where x->>'ref'=e->>'ref')
   and not exists(select 1 from jsonb_array_elements(p->'omitted_items') o where o->>'section'='evidence' and o->'item'->>'ref'=e->>'ref') then
   raise exception 'context v7 human answer missing' using errcode='42501';end if;
 end loop;
 -- Q1: v7 only under a strategy that declares it.
 if not exists(select 1 from control.nexloop_context_strategies s where s.tenant_id=p_tenant and s.world=p_world
   and s.strategy_id=split_part(substr(pack->>'strategy_ref',18),'@',1) and s.version=split_part(pack->>'strategy_ref','@',2)::integer
   and s.definition->>'protocol'='nexloop.context-pack.v7') then
  raise exception 'context v7 strategy not v7' using errcode='42501';end if;
end $$;
alter function authz.nexloop_context_human_answers_complete(text,text,text,jsonb,jsonb) owner to nexloop_owner;
revoke all on function authz.nexloop_context_human_answers_complete(text,text,text,jsonb,jsonb) from public;

-- 3. ---------------------------------------------------------------------------------------------------------------------------
drop function authz.nexloop_context_v6_sections(text,text,text,text,jsonb,jsonb,jsonb,text[],text,uuid,uuid,jsonb);
create function authz.nexloop_context_sections_core(p_protocol text,p_digest text,p_world text,p_tenant text,p_principal text,a jsonb,p jsonb,pack jsonb,
 p_conversations text[],p_consumer text,p_run uuid,p_context uuid,p_core_sources jsonb) returns void
language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare strat control.nexloop_context_strategies;goal_fact jsonb;control jsonb;proofs jsonb:='{}'::jsonb;proof jsonb;item jsonb;section text;
 ordinal integer:=0;tags jsonb;omitted jsonb;n integer;required record;latest text;
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
  values(p_tenant,p_world,p_context,p_run,p_protocol,pack->>'strategy_ref',p->>'pack_digest',pack->'bindings'->>'artifact_id',pack->'budget_report',pack->'insufficient');
 -- Core sources, then strategy and control.
 for item in select value from jsonb_array_elements(p_core_sources||jsonb_build_array(
   jsonb_build_object('section','bindings','ref',pack->>'strategy_ref','revision',strat.version::text,'hash',strat.definition_digest,'kind','policy','decision',runtime.nexloop_context_hash(a)),
   jsonb_build_object('section','goal','ref','nexloop:control:'||coalesce(control->>'control_revision','unavailable'),'revision',coalesce(control->>'control_revision','0'),
    'hash',runtime.nexloop_context_hash(coalesce(control,'null'::jsonb)),'kind','policy','decision',runtime.nexloop_context_hash(a)))) loop
  insert into runtime.nexloop_context_sources values(p_tenant,p_world,p_context,ordinal,item->>'section',item->>'ref',item->>'revision',item->>'hash',item->>'kind','decision:'||(item->>'decision'),'included',null);
  ordinal:=ordinal+1;
 end loop;
 -- 0202: the latest consumer turn before the current statement (the real latest inbound, not erased, of the statement's own
 -- conversation). The pack's conversation item for it must carry latest_consumer_turn (no other item may), and it is pinned.
 if jsonb_typeof(pack->'user_statement')='object' and pack->'user_statement'->>'conversation_id'=any(p_conversations)
  and pack->'user_statement'->>'sequence' ~ '^[0-9]{1,18}$' then
  select m.message_id into latest from runtime.nexloop_conversation_messages m
   where m.tenant_id=p_tenant and m.world=p_world and m.conversation_id=pack->'user_statement'->>'conversation_id'
    and coalesce(m.record->>'erased','false')<>'true' and m.record->>'direction' is distinct from 'outbound'
    and m.sequence<(pack->'user_statement'->>'sequence')::bigint
   order by m.sequence desc limit 1;
 end if;
 -- Item sections: every item re-verified against its source; tags are what the source requires.
 foreach section in array sections loop
  if jsonb_typeof(pack->section) is distinct from 'array' or jsonb_array_length(pack->section)>256 then raise exception 'context v6 pack invalid' using errcode='42501';end if;
  if (pack->'budget_report'->'sections'->section->>'included')::integer is distinct from jsonb_array_length(pack->section) then raise exception 'context v6 budget report mismatch' using errcode='42501';end if;
  for item in select value from jsonb_array_elements(pack->section) loop
   tags:=case when p_protocol='nexloop.context-pack.v7' and section='evidence' and item->>'subsection'='human_answers'
    then authz.nexloop_context_human_answer_in(p_tenant,p_world,p_consumer,item) else authz.nexloop_context_v6_source_in(p_tenant,p_world,p_conversations,p_consumer,section,item,proofs) end;
   if section='evidence' and latest is not null and item->>'ref'='eios:object:Message/'||latest then
    tags:=to_jsonb(array(select t from jsonb_array_elements_text(tags||'["latest_consumer_turn"]'::jsonb) t order by t));end if;
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
  tags:=case when p_protocol='nexloop.context-pack.v7' and omitted->>'section'='evidence' and omitted->'item'->>'subsection'='human_answers'
   then authz.nexloop_context_human_answer_in(p_tenant,p_world,p_consumer,omitted->'item') else authz.nexloop_context_v6_source_in(p_tenant,p_world,p_conversations,p_consumer,omitted->>'section',omitted->'item',proofs) end;
  if tags<>'[]'::jsonb or omitted->'item'->'tags' is distinct from tags
   or latest is not null and omitted->>'section'='evidence' and omitted->'item'->>'ref'='eios:object:Message/'||latest then
   raise exception 'context v6 pinned source omitted' using errcode='42501';end if;
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
  union all select 'open_work','nexloop:plan:'||pl.plan_id||'@'||pl.version from runtime.nexloop_active_plans(p_tenant,p_world) pl where pl.consumer_id=p_consumer
  union all select 'open_work','eios:object:Commitment/'||o.object_id from ontology.objects o where o.tenant_id=p_tenant and o.world=p_world and o.type_name='Commitment'
   and o.properties->>'made_to'=p_consumer and o.properties->>'status' in ('conditional','open','in_progress','breached')
 loop
  if not exists(select 1 from jsonb_array_elements(pack->required.section) r where r->>'ref'=required.ref)
   and not exists(select 1 from jsonb_array_elements(pack->'insufficient') x where x->>'code'='required_source_unreadable' and x->>'section'=required.section)
   -- 0206: a fallback reply Run's degraded Context may name, by reference, open work it cannot carry within the bound size
   -- (mandatory_exceeds_budget); contact limits and refusals (evidence) never. S88: any Run (message, plan, fallback) over its
   -- token budget, by the same rule.
   and not (required.section='open_work'
    and exists(select 1 from jsonb_array_elements(pack->'insufficient') x where x->>'code'='mandatory_exceeds_budget' and x->>'section'='open_work' and x->'refs' ? required.ref)) then
   raise exception 'context v6 pinned source missing' using errcode='42501';end if;
 end loop;
 if p_protocol='nexloop.context-pack.v7' then perform authz.nexloop_context_human_answers_complete(p_tenant,p_world,p_consumer,pack,p);end if;
end $$;
alter function authz.nexloop_context_sections_core(text,text,text,text,text,jsonb,jsonb,jsonb,text[],text,uuid,uuid,jsonb) owner to nexloop_owner;
revoke all on function authz.nexloop_context_sections_core(text,text,text,text,text,jsonb,jsonb,jsonb,text[],text,uuid,uuid,jsonb) from public;
create function authz.nexloop_context_v6_sections(p_digest text,p_world text,p_tenant text,p_principal text,a jsonb,p jsonb,pack jsonb,
 p_conversations text[],p_consumer text,p_run uuid,p_context uuid,p_core_sources jsonb) returns void
language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
begin
 perform authz.nexloop_context_sections_core('nexloop.context-pack.v6',p_digest,p_world,p_tenant,p_principal,a,p,pack,p_conversations,p_consumer,p_run,p_context,p_core_sources);
end $$;
alter function authz.nexloop_context_v6_sections(text,text,text,text,jsonb,jsonb,jsonb,text[],text,uuid,uuid,jsonb) owner to nexloop_owner;
revoke all on function authz.nexloop_context_v6_sections(text,text,text,text,jsonb,jsonb,jsonb,text[],text,uuid,uuid,jsonb) from public;

drop function authz.nexloop_role_context_v6_command(text,text,text,text,text);
create function authz.nexloop_role_context_command_core(p_protocol text,p_digest text,p_world text,p_text text,p_signature text,p_payload text)
returns jsonb language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare a jsonb:=p_text::jsonb;p jsonb;k bytea;ident jsonb;tenant text;principal text;ca jsonb;cp jsonb;snapshot jsonb;pack jsonb;
 b runtime.nexloop_role_context_artifacts;artifact runtime.nexloop_local_artifacts;proof jsonb;key text;consumer text;fresh boolean;namespace text;
 context_uuid uuid;run uuid;binding jsonb;core jsonb;opersona jsonb;outreach jsonb;
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
  or p_protocol not in ('nexloop.context-pack.v6','nexloop.context-pack.v7') or pack->>'schema_version' is distinct from p_protocol or (select count(*) from jsonb_object_keys(pack))<>18+(case when pack ? 'operator_persona' then 1 else 0 end)+(case when pack ? 'outreach' then 1 else 0 end)
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
  -- Outreach slice 5: the operator persona section (as the message Run, 0227) and the outreach section are exactly the re-derived
  -- ones, present only when they exist; both are cited as role sources.
  opersona:=runtime.nexloop_operator_persona_section(tenant,p_world,consumer);
  if (pack ? 'operator_persona') is distinct from (opersona is not null) or (opersona is not null and pack->'operator_persona' is distinct from opersona) then
   raise exception 'context v6 operator persona mismatch' using errcode='42501';end if;
  if opersona is not null then
   core:=core||jsonb_build_array(jsonb_build_object('section','role','ref',opersona->>'persona_ref','revision',opersona->>'version',
    'hash',runtime.nexloop_context_hash(opersona),'kind','policy','decision',runtime.nexloop_context_hash(ca)));
   perform runtime.nexloop_operator_persona_settle(tenant,p_world,consumer,principal,'context:'||context_uuid::text,null);
  end if;
  outreach:=runtime.nexloop_outreach_run_section(tenant,p_world,consumer,run);
  if (pack ? 'outreach') is distinct from (outreach is not null) or (outreach is not null and pack->'outreach' is distinct from outreach) then
   raise exception 'context v6 outreach mismatch' using errcode='42501';end if;
  if outreach is not null then
   core:=core||jsonb_build_array(jsonb_build_object('section','role','ref','outreach-purposes@'||(outreach->>'list_version'),'revision',outreach->>'list_version',
    'hash',runtime.nexloop_context_hash(outreach),'kind','policy','decision',runtime.nexloop_context_hash(ca)));
  end if;
  perform authz.nexloop_context_sections_core(p_protocol,p_digest,p_world,tenant,principal,a,p,pack,
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
alter function authz.nexloop_role_context_command_core(text,text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_role_context_command_core(text,text,text,text,text,text) from public;
create function authz.nexloop_role_context_v6_command(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
returns jsonb language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
begin
 return authz.nexloop_role_context_command_core('nexloop.context-pack.v6',p_digest,p_world,p_text,p_signature,p_payload);
end $$;
create function authz.nexloop_role_context_v7_command(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
returns jsonb language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
begin
 return authz.nexloop_role_context_command_core('nexloop.context-pack.v7',p_digest,p_world,p_text,p_signature,p_payload);
end $$;
alter function authz.nexloop_role_context_v6_command(text,text,text,text,text) owner to nexloop_owner;
alter function authz.nexloop_role_context_v7_command(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_role_context_v6_command(text,text,text,text,text) from public;
revoke all on function authz.nexloop_role_context_v7_command(text,text,text,text,text) from public;
grant execute on function authz.nexloop_role_context_v6_command(text,text,text,text,text) to nexloop_api;
grant execute on function authz.nexloop_role_context_v7_command(text,text,text,text,text) to nexloop_api;

-- 4. ---------------------------------------------------------------------------------------------------------------------------
-- As 0100's (renamed by 0171), a v7 Run's model requests recorded like a v6 Run's.
create or replace function authz.nexloop_runtime_activation_command_before_inspect_scope_v0360(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
language plpgsql security definer set search_path=pg_catalog, pg_temp set row_security=on as $$
declare a jsonb:=p_text::jsonb;p jsonb:=p_payload::jsonb;result jsonb;s jsonb;req jsonb;command jsonb;b record;
 pack runtime.nexloop_context_packs;strat jsonb;existing runtime.nexloop_model_requests;prompt text;outcome jsonb;
begin
 if p ? 'request_snapshot' and (p->>'verb' is distinct from 'authorize' or p->>'operation' is distinct from 'model') then
  raise exception 'model request snapshot unexpected' using errcode='42501';end if;
 if p ? 'model_result' and (p->>'verb' is distinct from 'authorize' or p->>'operation' is distinct from 'model' or p ? 'request_snapshot') then
  raise exception 'model result unexpected' using errcode='42501';end if;
 result:=authz.nexloop_runtime_activation_command_before_model_requests_v0090(p_digest,p_world,p_text,p_signature,p_payload);
 if not p ? 'request_snapshot' and not p ? 'model_result' then return result;end if;
 command:=(p->>'command_text')::jsonb;perform set_config('eios.tenant_id',a->>'tenant_id',true);
 select * into b from runtime.nexloop_context_v6_binding(a->>'tenant_id',p_world,(command->>'run_id')::uuid);
 if not found or b.pack_text::jsonb->>'schema_version' not in ('nexloop.context-pack.v6','nexloop.context-pack.v7') then raise exception 'model request snapshot requires a v6 Context' using errcode='42501';end if;
 if p ? 'model_result' then
  -- The outcome of an already recorded request, written once (0089 trigger); a retry with the
  -- same outcome is idempotent, a different outcome for the same call is refused.
  outcome:=p->'model_result';
  if jsonb_typeof(outcome) is distinct from 'object' or (select array_agg(x order by x) from jsonb_object_keys(outcome) x) is distinct from array['call_sequence','cost','response_digest','result_status','usage']
   or jsonb_typeof(outcome->'call_sequence') is distinct from 'number' or outcome->>'call_sequence'!~'^[1-9][0-9]{0,4}$'
   or outcome->>'result_status' is null or outcome->>'result_status' not in ('succeeded','failed','unknown')
   or (outcome->'usage' is distinct from 'null'::jsonb and (jsonb_typeof(outcome->'usage') is distinct from 'object'
     or (select array_agg(x order by x) from jsonb_object_keys(outcome->'usage') x) is distinct from array['cache_read','cache_write','input','output','total']
     or exists(select 1 from jsonb_each(outcome->'usage') u where jsonb_typeof(u.value) is distinct from 'number' or u.value#>>'{}'!~'^[0-9]{1,12}$')))
   or (outcome->'cost' is distinct from 'null'::jsonb and (jsonb_typeof(outcome->'cost') is distinct from 'string' or outcome->>'cost'!~'^[0-9]{1,10}(\.[0-9]{1,8})?$'))
   or (outcome->'response_digest' is distinct from 'null'::jsonb and coalesce(outcome->>'response_digest','')!~'^[0-9a-f]{64}$')
   or (outcome->>'result_status'='succeeded' and (outcome->'usage'='null'::jsonb or outcome->'cost'='null'::jsonb or outcome->'response_digest'='null'::jsonb)) then
   raise exception 'model result invalid' using errcode='42501';end if;
  select * into existing from runtime.nexloop_model_requests where tenant_id=b.tenant_id and world=b.world and run_id=b.run_id and call_sequence=(outcome->>'call_sequence')::integer for update;
  if not found then raise exception 'model result without request' using errcode='42501';end if;
  if existing.result_status is not null then
   if (existing.result_status,existing.usage,existing.cost,existing.response_digest)
    is distinct from (outcome->>'result_status',nullif(outcome->'usage','null'::jsonb),(outcome->>'cost')::numeric,outcome->>'response_digest') then
    raise exception 'model result already recorded' using errcode='42501';end if;
   return result;
  end if;
  update runtime.nexloop_model_requests set result_status=outcome->>'result_status',usage=nullif(outcome->'usage','null'::jsonb),cost=(outcome->>'cost')::numeric,
   response_digest=outcome->>'response_digest',completed_at=clock_timestamp()
   where tenant_id=b.tenant_id and world=b.world and run_id=b.run_id and call_sequence=(outcome->>'call_sequence')::integer;
  return result;
 end if;
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

-- As 0110's, with the v7 Role packs.
create or replace function runtime.nexloop_context_v6_binding(p_tenant text,p_world text,p_run uuid) returns table(tenant_id text,world text,run_id uuid,context_id uuid,pack_text text)
language sql stable set search_path=pg_catalog,pg_temp as $$
 select b.tenant_id,b.world,b.run_id,b.context_id,b.pack_text from runtime.nexloop_context_artifact_bindings b where b.tenant_id=p_tenant and b.world=p_world and b.run_id=p_run
 union all
 select b.tenant_id,b.world,b.run_id,b.context_id,b.pack_text from runtime.nexloop_fallback_context_bindings b where b.tenant_id=p_tenant and b.world=p_world and b.run_id=p_run
 union all
 select r.tenant_id,r.world,r.run_id,runtime.nexloop_role_pack_context_id(r.run_id),r.pack_text from runtime.nexloop_role_context_artifacts r
  where r.tenant_id=p_tenant and r.world=p_world and r.run_id=p_run and r.pack_text::jsonb->>'schema_version' in ('nexloop.context-pack.v6','nexloop.context-pack.v7') $$;

-- As 0088's, a strategy may declare v7 (Q1).
create or replace function control.nexloop_context_strategy_shape(d jsonb) returns boolean language sql immutable set search_path=pg_catalog,pg_temp as $$
 select jsonb_typeof(d)='object'
  and (select array_agg(k order by k) from jsonb_object_keys(d) k)=array['framing_reserve','include_hypotheses','include_rejected_definitions',
    'input_token_budget','output_reserve','protocol','sections','semantic','strategy_id','trim_order','version']
  and d->>'protocol' in ('nexloop.context-pack.v6','nexloop.context-pack.v7') and jsonb_typeof(d->'input_token_budget')='number' and jsonb_typeof(d->'output_reserve')='number'
  and jsonb_typeof(d->'framing_reserve')='number' and jsonb_typeof(d->'sections')='object' and jsonb_typeof(d->'trim_order')='array'
  and jsonb_typeof(d->'semantic')='object' and jsonb_typeof(d->'include_hypotheses')='boolean' and jsonb_typeof(d->'include_rejected_definitions')='boolean'
  and (d->>'input_token_budget')::numeric between 1024 and 1000000 and (d->>'output_reserve')::numeric between 128 and 200000
  and (d->>'framing_reserve')::numeric between 0 and (d->>'input_token_budget')::numeric
$$;
