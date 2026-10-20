-- NX-041 S1 "one world check" (docs/implementation/NX-041-design.md D1, D9, §2.1, §11 S1; owner D1-D13 decided). The migration number
-- lives only in the file name and the catalog; nothing below repeats it.
--
-- A world is exactly one of: real, test, sim:<uuid>, shadow:<uuid> (D1: one world per experiment; no reserved name but real and test).
-- runtime.nexloop_world_valid is the one check; runtime.nexloop_world_kind names the kind (real | test | simulation | shadow); the
-- literal 'simulation' is no longer a world - it stays only the cost and Claim data mode (runtime.nexloop_world_data_mode: real ->
-- real, sim:* and shadow:* -> simulation, test -> test; D9). The existing checks, each written its own way, now call it:
--   0234 runtime.nexloop_semantic_accesses: check(world in ('real','test','simulation'))      -> check(runtime.nexloop_world_valid(world))
--   0067 ontology.nexloop_recall_tenant: p_world ~ '^[A-Za-z][A-Za-z0-9_.-]{0,63}$'          -> runtime.nexloop_world_valid(p_world)
--   0165 authz.nexloop_service_credentials: worlds unchecked beyond "real alone"              -> + every world valid (constraint)
--   0120 runtime.nexloop_cost_data_mode: p_world in ('simulation','shadow') -> 'simulation'   -> runtime.nexloop_world_data_mode
--   0155 authz.nexloop_claim_contract: the same world -> mode mapping                          -> runtime.nexloop_world_data_mode
--   0131 authz.nexloop_metrics_export: the world list (real, test, simulation)                 -> (real, test); registered sim worlds are S2's
-- real and test behave exactly as before. Published migrations are not edited.

create function runtime.nexloop_world_valid(p_world text) returns boolean language sql immutable set search_path=pg_catalog,pg_temp as $$
 select p_world is not null and (p_world in ('real','test')
  or p_world ~ '^(sim|shadow):[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
$$;
create function runtime.nexloop_world_kind(p_world text) returns text language sql immutable set search_path=pg_catalog,pg_temp as $$
 select case when not runtime.nexloop_world_valid(p_world) then null when p_world in ('real','test') then p_world
  when p_world like 'sim:%' then 'simulation' else 'shadow' end
$$;
-- The cost and Claim data mode of a world: real, simulation (sim:* and shadow:*), else test (as 0120 / 0155 for real and test).
create function runtime.nexloop_world_data_mode(p_world text) returns text language sql immutable set search_path=pg_catalog,pg_temp as $$
 select case runtime.nexloop_world_kind(p_world) when 'real' then 'real' when 'simulation' then 'simulation' when 'shadow' then 'simulation' else 'test' end
$$;
create function runtime.nexloop_assert_world(p_world text) returns text language plpgsql immutable set search_path=pg_catalog,pg_temp as $$
begin
 if not runtime.nexloop_world_valid(p_world) then raise exception 'world invalid' using errcode='22023';end if;
 return p_world;
end $$;
create function runtime.nexloop_worlds_valid(p_worlds text[]) returns boolean language sql immutable set search_path=pg_catalog,pg_temp as $$
 select p_worlds is not null and cardinality(p_worlds)>=1 and not exists(select 1 from unnest(p_worlds) w where not runtime.nexloop_world_valid(w))
$$;
do $$declare f text;begin
 foreach f in array array['runtime.nexloop_world_valid(text)','runtime.nexloop_world_kind(text)','runtime.nexloop_world_data_mode(text)',
   'runtime.nexloop_assert_world(text)','runtime.nexloop_worlds_valid(text[])'] loop
  execute 'alter function '||f||' owner to nexloop_owner';
  -- Pure functions of their argument: every role may evaluate them (they appear in check constraints and invoker-rights views).
  execute 'grant execute on function '||f||' to public';
 end loop;
end $$;

-- 0234: the semantic access record.
alter table runtime.nexloop_semantic_accesses drop constraint nexloop_semantic_accesses_world_check;
alter table runtime.nexloop_semantic_accesses add constraint nexloop_semantic_accesses_world_check check(runtime.nexloop_world_valid(world)) not valid;
alter table runtime.nexloop_semantic_accesses validate constraint nexloop_semantic_accesses_world_check;

-- 0165: every world of a service credential is a valid world (real alone stays as it is).
alter table authz.nexloop_service_credentials add constraint nexloop_service_credentials_worlds_valid check(runtime.nexloop_worlds_valid(worlds)) not valid;
alter table authz.nexloop_service_credentials validate constraint nexloop_service_credentials_worlds_valid;

-- 0120: the cost data mode.
create or replace function runtime.nexloop_cost_data_mode(p_world text) returns text language sql immutable set search_path=pg_catalog,pg_temp as $$
 select runtime.nexloop_world_data_mode(p_world)
$$;

-- 0067: the recall entry.
create or replace function ontology.nexloop_recall_tenant(p_digest text,p_world text) returns text
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v jsonb;t text;
begin
 if not runtime.nexloop_world_valid(p_world) then raise exception 'recall world invalid' using errcode='22023';end if;
 v:=authz.nexloop_service_identity_snapshot(p_digest,p_world);
 t:=v->'binding'->>'tenant_id';
 if t is null or t='' then raise exception 'recall authentication denied' using errcode='42501';end if;
 perform 1 from control.nexloop_tenants where tenant_id=t and status='active';
 if not found then raise exception 'recall tenant inactive' using errcode='42501';end if;
 perform set_config('eios.tenant_id',t,true);
 return t;
end $$;

-- 0155: the Claim contract's mode (0155 body; the mode expression only).
create or replace function authz.nexloop_claim_contract(x ontology.nexloop_claims) returns jsonb
 language sql stable set search_path=pg_catalog,pg_temp as $$
 select jsonb_build_object('claim_id',x.claim_id,'tenant_id',x.tenant_id,'world_id',x.world,
  'mode',runtime.nexloop_world_data_mode(x.world),
  'conversation_id',x.conversation_id,
  'consumer_id',x.consumer_id,'topic_key',x.topic_key,
  'subject',jsonb_build_object('kind',x.subject_kind,'ref',x.subject_ref,'text',x.subject_text),
  'predicate',x.predicate,'value',x.value,'speaker',x.speaker,'polarity',x.polarity,'modality',x.modality,'condition',x.condition_text,
  'time_expression',x.time_expression,'valid_time',x.valid_time,
  'source',case when x.source_message_id is null then 'null'::jsonb else jsonb_build_object('message_id',x.source_message_id,'sequence',x.source_sequence,
    'span_start',x.span_start,'span_end',x.span_end,'content_hash',x.source_content_hash,'quote',x.quote) end,
  'derived_from',x.derived_from,'corrects_claim_id',x.corrects_claim_id,'extractor_version',x.extractor_version,
  'confidence',x.confidence,'epistemic_kind',x.epistemic_kind,'resolution_state',x.resolution_state,'correlation_key',x.correlation_key,
  'guard_flags',x.guard_flags,'recorded_at',to_char(x.recorded_at at time zone 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"'))
$$;

-- 0131: the metrics export's world list (0131 body; real and test).
create or replace function authz.nexloop_metrics_export() returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
begin
 if session_user is distinct from 'nexloop_metrics' then raise exception 'metrics export unavailable' using errcode='42501';end if;
 return coalesce((select jsonb_agg(runtime.nexloop_metrics_snapshot(t.tenant_id,w.world) order by t.tenant_id,w.world)
  from control.nexloop_tenants t cross join (values ('real'),('test')) w(world) where t.status='active'),'[]'::jsonb);
end $$;

