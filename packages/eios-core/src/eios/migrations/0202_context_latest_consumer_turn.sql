-- Latest consumer turn in the message Context (temporary number 0202; 10/17 stage smoke: the second reply cited older history
-- from Claims but not the customer's own last request). The Context now lists conversation turns before Claims and marks the
-- latest consumer turn before the current statement with the tag latest_consumer_turn; that turn is pinned (never trimmed).
-- authz.nexloop_context_v6_sections (latest body: 0113, copied verbatim) additionally requires that tag on exactly that turn:
-- the latest not-erased inbound Message of the statement's own conversation with a smaller sequence. It may not be omitted.
-- Its Claim rules are unchanged (evidence only, pinned negations/contact limits). Nothing is written beyond 0113's own rows.
create or replace function authz.nexloop_context_v6_sections(p_digest text,p_world text,p_tenant text,p_principal text,a jsonb,p jsonb,pack jsonb,
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
  values(p_tenant,p_world,p_context,p_run,'nexloop.context-pack.v6',pack->>'strategy_ref',p->>'pack_digest',pack->'bindings'->>'artifact_id',pack->'budget_report',pack->'insufficient');
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
   tags:=authz.nexloop_context_v6_source_in(p_tenant,p_world,p_conversations,p_consumer,section,item,proofs);
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
  tags:=authz.nexloop_context_v6_source_in(p_tenant,p_world,p_conversations,p_consumer,omitted->>'section',omitted->'item',proofs);
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
   and not exists(select 1 from jsonb_array_elements(pack->'insufficient') x where x->>'code'='required_source_unreadable' and x->>'section'=required.section) then
   raise exception 'context v6 pinned source missing' using errcode='42501';end if;
 end loop;
end $$;
