-- NX-023-A: storage for assembled Context packs, their per-source provenance and every
-- actual model request (AT-027). Writers arrive with NX-023-B (v6 bind + guard request
-- snapshot); this migration fixes the shape and its invariants: tenant RLS, no
-- application-role privileges, append-only rows, dense per-Run call sequence, and
-- request results recorded at most once.
create table runtime.nexloop_context_packs (
 tenant_id text not null,world text not null,context_id uuid not null,run_id uuid not null,
 protocol text not null check(protocol~'^nexloop\.context-pack\.v[1-9][0-9]*$'),
 strategy_ref text not null check(strategy_ref~'^context-strategy:[a-z][a-z0-9_]{0,63}@[1-9][0-9]{0,6}$'),
 pack_digest text not null check(pack_digest~'^[0-9a-f]{64}$'),artifact_id text not null check(artifact_id~'^[0-9a-f]{32}$'),
 budget_report jsonb not null check(jsonb_typeof(budget_report)='object'),insufficient jsonb not null check(jsonb_typeof(insufficient)='array'),
 created_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,world,context_id),unique(tenant_id,world,run_id,context_id)
);
create table runtime.nexloop_context_sources (
 tenant_id text not null,world text not null,context_id uuid not null,ordinal integer not null check(ordinal>=0),
 section text not null check(section in ('bindings','role','goal','current_event','constraints','consumer_state','open_work','evidence','semantics','experience')),
 ref text not null check(length(ref) between 1 and 512),revision text not null check(length(revision) between 1 and 128),
 content_hash text not null check(content_hash~'^[0-9a-f]{64}$'),
 evidence_kind text not null check(evidence_kind in ('formal_object','policy','schema','current_message','conversation','user_statement','hypothesis','execution_state','memory')),
 access_decision_ref text not null check(access_decision_ref~'^decision:[0-9a-f]{64}$'),
 status text not null check(status in ('included','omitted')),omitted_reason text,
 primary key(tenant_id,world,context_id,ordinal),
 check((status='omitted')=(omitted_reason is not null)),
 -- ADR-019 decisions 2/5 (AT-064): the formal zone never carries Claims or hypotheses.
 check(section not in ('constraints','consumer_state') or evidence_kind in ('formal_object','policy')),
 check(section<>'open_work' or evidence_kind in ('formal_object','execution_state')),
 check(evidence_kind<>'hypothesis' or section='evidence'),
 foreign key(tenant_id,world,context_id) references runtime.nexloop_context_packs
);
create table runtime.nexloop_model_requests (
 tenant_id text not null,world text not null,run_id uuid not null,call_sequence integer not null check(call_sequence>=1),
 context_id uuid not null,model_provider text not null check(length(model_provider) between 1 and 64),
 model_id text not null check(length(model_id) between 1 and 128),
 tool_manifest_digest text not null check(tool_manifest_digest~'^[0-9a-f]{64}$'),
 request_digest text not null check(request_digest~'^[0-9a-f]{64}$'),
 prompt_artifact_id text not null check(prompt_artifact_id~'^[0-9a-f]{32}$'),
 input_token_budget integer not null check(input_token_budget>=256),output_token_budget integer not null check(output_token_budget>=128),
 settings_digest text not null check(settings_digest~'^[0-9a-f]{64}$'),requested_at timestamptz not null default clock_timestamp(),
 result_status text check(result_status in ('succeeded','failed','unknown')),usage jsonb check(usage is null or jsonb_typeof(usage)='object'),
 cost numeric(18,8) check(cost is null or cost>=0),response_digest text check(response_digest is null or response_digest~'^[0-9a-f]{64}$'),
 completed_at timestamptz,
 primary key(tenant_id,world,run_id,call_sequence),
 check((result_status is null)=(completed_at is null)),
 foreign key(tenant_id,world,run_id,context_id) references runtime.nexloop_context_packs(tenant_id,world,run_id,context_id)
);
do $$ declare t text;begin
 foreach t in array array['nexloop_context_packs','nexloop_context_sources','nexloop_model_requests'] loop
  execute format('alter table runtime.%I owner to nexloop_owner',t);
  execute format('alter table runtime.%I enable row level security',t);
  execute format('alter table runtime.%I force row level security',t);
  execute format('create policy tenant_boundary on runtime.%I to nexloop_owner using(tenant_id=current_setting(''eios.tenant_id'',true)) with check(tenant_id=current_setting(''eios.tenant_id'',true))',t);
  execute format('revoke all on runtime.%I from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_identity',t);
 end loop;
end $$;
create trigger nexloop_context_pack_append_only before update or delete on runtime.nexloop_context_packs
 for each row execute function control.nexloop_context_append_only();
create trigger nexloop_context_source_append_only before update or delete on runtime.nexloop_context_sources
 for each row execute function control.nexloop_context_append_only();

-- Dense per-Run sequence (1,2,3…) and a single, final result for each request.
create function runtime.nexloop_model_request_guard() returns trigger language plpgsql set search_path=pg_catalog as $$
declare v_previous integer;
begin
 if tg_op='DELETE' then raise exception 'model requests are append-only' using errcode='22023';end if;
 if tg_op='INSERT' then
  if new.result_status is not null then raise exception 'model request result must be recorded after the request' using errcode='22023';end if;
  perform pg_advisory_xact_lock(hashtextextended(new.tenant_id||':'||new.world||':model-request:'||new.run_id::text,23));
  select max(call_sequence) into v_previous from runtime.nexloop_model_requests where tenant_id=new.tenant_id and world=new.world and run_id=new.run_id;
  if new.call_sequence is distinct from coalesce(v_previous,0)+1 then raise exception 'model request call_sequence must be dense' using errcode='22023';end if;
  return new;
 end if;
 if old.result_status is not null or new.result_status is null
  or (new.tenant_id,new.world,new.run_id,new.call_sequence,new.context_id,new.model_provider,new.model_id,new.tool_manifest_digest,new.request_digest,
      new.prompt_artifact_id,new.input_token_budget,new.output_token_budget,new.settings_digest,new.requested_at)
   is distinct from (old.tenant_id,old.world,old.run_id,old.call_sequence,old.context_id,old.model_provider,old.model_id,old.tool_manifest_digest,old.request_digest,
      old.prompt_artifact_id,old.input_token_budget,old.output_token_budget,old.settings_digest,old.requested_at) then
  raise exception 'model request is immutable; its result is recorded once' using errcode='22023';end if;
 return new;
end $$;
alter function runtime.nexloop_model_request_guard() owner to nexloop_owner;
revoke all on function runtime.nexloop_model_request_guard() from public;
create trigger nexloop_model_request_guard before insert or update or delete on runtime.nexloop_model_requests
 for each row execute function runtime.nexloop_model_request_guard();
