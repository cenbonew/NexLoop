-- NX-028 slice 2 (temporary number 0150; design placeholder 0116): one governed human entry with a capability registry,
-- the dispatch prediction, and two human requests (manual plan reevaluation, query of an effect result).
--
-- 1. D9 (dispatcher ruling 2026-10-10): authz.nexloop_goal_governed_action is renamed ONCE and kept
--    (authz.nexloop_goal_governed_action_before_registry_v0115, no grants) and replaced by a wrapper whose capability →
--    operation, subject rule and handler come from control.nexloop_governed_capabilities (owner-only, append-only).
--    Every check of the 0111 body is unchanged (signature, permit, published contract, live identity, claim fence,
--    commit tail). Handlers have one signature (tenant, world, principal, subject_kind, intent, body) → jsonb, are owned
--    by nexloop_owner and live in control/runtime; the registry trigger refuses anything else. NX-027 and later tasks
--    add registry rows only (and their handler functions); they never rewrite the entry again.
-- 2. control.nexloop_intent_dispatch_prediction(tenant, world, intent): what the intent dispatch check (0097 snapshot,
--    0068 controls, 0109/0110/0112 contact rules) would decide now, without raising and without writing. Workbench
--    reads (AT-006 UI part) show it; later slices replace only its body (NX-028 slice 3 adds taken_over).
-- 3. Human requests registered through the entry: nexloop.plan.request_reevaluation (marks one active plan due now,
--    cause human) and nexloop.service.query_request (D7: brings an unknown / query-pending effect forward for the
--    executor's one QUERY; nobody resends, the idempotency key never changes, the human holds no reconcile Action).

-- 1. Registry ------------------------------------------------------------------------------------------------------
create table control.nexloop_governed_capabilities (
 capability text primary key check(capability~'^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$'),
 operation text not null unique check(operation~'^[a-z][a-z0-9_]{0,63}$'),
 subject_rule text not null check(subject_rule in ('human','agent_or_human')),
 handler regprocedure not null,source_task text not null check(source_task~'^NX-[0-9]{3}$'),
 registered_at timestamptz not null default clock_timestamp()
);
alter table control.nexloop_governed_capabilities owner to nexloop_owner;
create trigger nx028_append_only before update or delete on control.nexloop_governed_capabilities for each row execute function control.nexloop_nx022_append_only();
revoke all on control.nexloop_governed_capabilities from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity,nexloop_configurator;

-- A handler is an owner function in control/runtime with exactly (text,text,text,text,text,jsonb) → jsonb
-- (oidvector is zero-based: re-index before comparing).
create function control.nexloop_governed_capability_guard() returns trigger
 language plpgsql set search_path=pg_catalog,pg_temp as $$
declare f pg_proc;
begin
 select * into f from pg_proc where oid=new.handler::oid;
 if not found or f.pronamespace not in ('control'::regnamespace,'runtime'::regnamespace) or f.proowner<>'nexloop_owner'::regrole
  or f.prorettype<>'jsonb'::regtype or array(select unnest(f.proargtypes::oid[]))<>array['text','text','text','text','text','jsonb']::regtype[]::oid[] or f.proretset then
  raise exception 'governed capability handler invalid' using errcode='22023';end if;
 return new;
end $$;
alter function control.nexloop_governed_capability_guard() owner to nexloop_owner;
revoke all on function control.nexloop_governed_capability_guard() from public;
create trigger nx028_handler_guard before insert on control.nexloop_governed_capabilities for each row execute function control.nexloop_governed_capability_guard();

-- Handlers of the existing capabilities (thin adapters to the unchanged functions).
create function control.nexloop_governed_owner_change(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language sql set search_path=pg_catalog,pg_temp as $$ select control.nexloop_nx022_owner_change(p_tenant,p_world,p_principal,p_intent,body) $$;
create function control.nexloop_governed_publish_goal(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language sql set search_path=pg_catalog,pg_temp as $$ select control.nexloop_nx022_write_goal(p_tenant,p_world,p_principal,p_subject,p_intent,body,false) $$;
create function control.nexloop_governed_propose_goal(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language sql set search_path=pg_catalog,pg_temp as $$ select control.nexloop_nx022_write_goal(p_tenant,p_world,p_principal,p_subject,p_intent,body,true) $$;
create function control.nexloop_governed_contact_release(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language sql set search_path=pg_catalog,pg_temp as $$ select control.nexloop_contact_release(p_tenant,p_world,p_principal,p_intent,body) $$;
create function runtime.nexloop_governed_commitment(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language sql set search_path=pg_catalog,pg_temp as $$ select runtime.nexloop_commitment_human(p_tenant,p_world,p_principal,p_intent,body) $$;

-- 3. Human requests -----------------------------------------------------------------------------------------------
-- Audit of both requests (one row per governed intent).
create table runtime.nexloop_human_requests (
 tenant_id text not null,world text not null,intent_id text not null,kind text not null check(kind in ('plan_reevaluation','effect_query')),
 target text not null,principal_id text not null,reason text not null check(length(reason) between 1 and 500),detail jsonb not null default '{}'::jsonb,
 requested_at timestamptz not null default clock_timestamp(),primary key(tenant_id,world,intent_id)
);
alter table runtime.nexloop_human_requests owner to nexloop_owner;
alter table runtime.nexloop_human_requests enable row level security;
alter table runtime.nexloop_human_requests force row level security;
create policy tenant_boundary on runtime.nexloop_human_requests to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
create trigger nx028_append_only before update or delete on runtime.nexloop_human_requests for each row execute function control.nexloop_nx022_append_only();
revoke all on runtime.nexloop_human_requests from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity,nexloop_configurator;

-- Manual reevaluation: one active plan marked due now (cause human); precheck still decides everything (NX-024).
create function runtime.nexloop_governed_plan_reevaluation(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language plpgsql set search_path=pg_catalog,pg_temp as $$
declare v_plan uuid;cur record;
begin
 if body->>'operation' is distinct from 'request_plan_reevaluation' or coalesce(body->>'plan_id','')!~'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
  or length(coalesce(body->>'reason','')) not between 1 and 500 then raise exception 'plan reevaluation request invalid' using errcode='22023';end if;
 v_plan:=(body->>'plan_id')::uuid;
 select * into cur from runtime.nexloop_plan_current(p_tenant,p_world,v_plan);
 if cur.status is distinct from 'active' then raise exception 'plan not active' using errcode='22023';end if;
 insert into runtime.nexloop_human_requests(tenant_id,world,intent_id,kind,target,principal_id,reason)
  values(p_tenant,p_world,p_intent,'plan_reevaluation','plan:'||v_plan,p_principal,body->>'reason');
 perform authz.nexloop_plan_feed_touch(p_tenant,p_world,v_plan,jsonb_build_object('kind','manual','cause','human','ref','human:'||p_principal,'at',clock_timestamp()),clock_timestamp());
 return jsonb_build_object('plan_id',v_plan,'version',cur.version,'requested',true);
end $$;

-- D7: a human asks for the result of an unknown / query-pending effect; the executor service runs its one QUERY
-- (0042/0065). Nothing is resent, the outbox state and idempotency key are unchanged; only its due time moves now.
create function runtime.nexloop_governed_effect_query(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language plpgsql set search_path=pg_catalog,pg_temp as $$
declare o runtime.nexloop_effect_outbox;v_intent uuid;
begin
 if body->>'operation' is distinct from 'request_effect_query' or coalesce(body->>'intent_id','')!~'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
  or length(coalesce(body->>'reason','')) not between 1 and 500 then raise exception 'effect query request invalid' using errcode='22023';end if;
 v_intent:=(body->>'intent_id')::uuid;
 select * into o from runtime.nexloop_effect_outbox where intent_id=v_intent and tenant_id=p_tenant and world=p_world for update;
 if not found or o.state not in ('unknown','query_pending') then raise exception 'effect result is not awaiting a query' using errcode='22023';end if;
 insert into runtime.nexloop_human_requests(tenant_id,world,intent_id,kind,target,principal_id,reason,detail)
  values(p_tenant,p_world,p_intent,'effect_query','intent:'||v_intent,p_principal,body->>'reason',jsonb_build_object('outbox_state',o.state));
 update runtime.nexloop_effect_outbox set available_at=least(available_at,clock_timestamp()) where intent_id=v_intent;
 return jsonb_build_object('intent_id',v_intent,'outbox_state',o.state,'requested',true);
end $$;

do $owners$
declare f text;
begin
 foreach f in array array['control.nexloop_governed_owner_change(text,text,text,text,text,jsonb)','control.nexloop_governed_publish_goal(text,text,text,text,text,jsonb)',
   'control.nexloop_governed_propose_goal(text,text,text,text,text,jsonb)','control.nexloop_governed_contact_release(text,text,text,text,text,jsonb)',
   'runtime.nexloop_governed_commitment(text,text,text,text,text,jsonb)','runtime.nexloop_governed_plan_reevaluation(text,text,text,text,text,jsonb)',
   'runtime.nexloop_governed_effect_query(text,text,text,text,text,jsonb)'] loop
  execute 'alter function '||f||' owner to nexloop_owner';
  execute 'revoke all on function '||f||' from public';
 end loop;
end $owners$;

insert into control.nexloop_governed_capabilities(capability,operation,subject_rule,handler,source_task) values
 ('goals.metric.approve','approve_metric','human','control.nexloop_governed_owner_change(text,text,text,text,text,jsonb)','NX-022'),
 ('goals.version.publish','publish_goal','human','control.nexloop_governed_publish_goal(text,text,text,text,text,jsonb)','NX-022'),
 ('goals.agent.propose','propose_agent_goal','agent_or_human','control.nexloop_governed_propose_goal(text,text,text,text,text,jsonb)','NX-022'),
 ('goals.control.set','set_control','human','control.nexloop_governed_owner_change(text,text,text,text,text,jsonb)','NX-022'),
 ('goals.budget.set','set_budget','human','control.nexloop_governed_owner_change(text,text,text,text,text,jsonb)','NX-022'),
 ('goals.contact.release','release_contact_restriction','human','control.nexloop_governed_contact_release(text,text,text,text,text,jsonb)','NX-025'),
 ('commitment.cancel','cancel_commitment','human','runtime.nexloop_governed_commitment(text,text,text,text,text,jsonb)','NX-026'),
 ('commitment.extend','extend_commitment','human','runtime.nexloop_governed_commitment(text,text,text,text,text,jsonb)','NX-026'),
 ('commitment.attest','attest_commitment','human','runtime.nexloop_governed_commitment(text,text,text,text,text,jsonb)','NX-026'),
 ('commitment.condition_met','commitment_condition_met','human','runtime.nexloop_governed_commitment(text,text,text,text,text,jsonb)','NX-026'),
 ('commitment.mark_communication','mark_commitment_communication','human','runtime.nexloop_governed_commitment(text,text,text,text,text,jsonb)','NX-026'),
 ('plan.request_reevaluation','request_plan_reevaluation','human','runtime.nexloop_governed_plan_reevaluation(text,text,text,text,text,jsonb)','NX-028'),
 ('service.query_request','request_effect_query','human','runtime.nexloop_governed_effect_query(text,text,text,text,text,jsonb)','NX-028');

-- The entry: renamed once and kept; the wrapper reads the registry.
alter function authz.nexloop_goal_governed_action(text,text,text,text,text) rename to nexloop_goal_governed_action_before_registry_v0115;
revoke all on function authz.nexloop_goal_governed_action_before_registry_v0115(text,text,text,text,text) from public,nexloop_api,nexloop_domain_worker;

create function authz.nexloop_goal_governed_action(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
 returns jsonb language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare a jsonb:=p_text::jsonb;body jsonb:=p_payload::jsonb;k bytea;stored runtime.nexloop_action_claims%rowtype;
 permit jsonb:=a->'permit';v_claim jsonb:=permit->'claim';ref jsonb:=v_claim->'binding'->'action_reference';d jsonb;
 ident jsonb;v_subject text;v_cap text;v_tenant text:=a->>'tenant_id';v_principal text:=a->>'principal_id';v_id text;v_outcome jsonb;v_result jsonb;
 cap control.nexloop_governed_capabilities;
begin
 if octet_length(p_text)>1048576 or octet_length(p_payload)>262144 then raise exception 'goal command too large' using errcode='22023';end if;
 if a->>'expires_at' is null or permit->>'expires_at' is null or v_claim->>'lease_expires_at' is null then
  raise exception 'goal command expiry required' using errcode='42501';end if;
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-goal-governed-v1:'||p_text,'UTF8'),k,'sha256'),'hex')
  or a->>'protocol' is distinct from 'nexloop-goal-governed-v1'
  or a->>'resource_id' is distinct from 'eios:action:'||(ref->>'stable_name')||':'||(ref->>'version')
  or (a->>'expires_at')::timestamptz>clock_timestamp()+interval '30 seconds'
  or v_claim->'binding'->>'request_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex') then
  raise exception 'goal permit rejected' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 select definition into d from control.nexloop_action_definitions where tenant_id=v_tenant and world=p_world and resource_id=a->>'resource_id' and active;
 v_cap:=d->'capability_binding'->>'capability_name';
 select * into cap from control.nexloop_governed_capabilities where capability=v_cap;
 if d is null or cap.capability is null or d is distinct from a->'definition' or d->'governance'->>'approval_mode'<>'none'
  or jsonb_array_length(d->'governance'->'policy_refs')<>0
  or not (d->'governance'->'change_scope'->'target_systems' @> '["postgres"]'::jsonb)
  or body->>'operation' is distinct from cap.operation
 then raise exception 'goal Action contract unavailable' using errcode='42501';end if;
 ident:=authz.nexloop_service_identity_snapshot(p_digest,p_world);v_subject:=ident->'binding'->>'subject_kind';
 if ident->'binding'->>'tenant_id' is distinct from v_tenant or ident->'binding'->>'subject_principal_id' is distinct from v_principal then
  raise exception 'goal identity binding mismatch' using errcode='42501';end if;
 if cap.subject_rule='agent_or_human' then
  if v_subject not in ('agent','human') then raise exception 'goal proposal requires an agent or human author' using errcode='42501';end if;
 elsif v_subject is distinct from 'human' then
  -- AT-005: only a human with a current EXECUTE grant changes owner goals, controls, budgets, restrictions or commitments.
  raise exception 'human goal authority required' using errcode='42501';
 end if;
 select * into stored from runtime.nexloop_action_claims where tenant_id=v_tenant and world=p_world
  and action_name=v_claim->'key'->>'action_stable_name' and intent_id=v_claim->'key'->>'idempotency_key' for update;
 if not found or stored.principal_id is distinct from v_principal or stored.claim is distinct from v_claim
  or v_claim->>'state'<>'active' or (v_claim->>'lease_expires_at')::timestamptz<=clock_timestamp()
  or (permit->>'expires_at')::timestamptz<=clock_timestamp() or body->>'request_id' is distinct from stored.intent_id then
  raise exception 'goal claim fenced' using errcode='40001';end if;
 perform set_config('eios.tenant_id',v_tenant,true);
 execute format('select %s($1,$2,$3,$4,$5,$6)',cap.handler::oid::regproc) into v_result using v_tenant,p_world,v_principal,v_subject,stored.intent_id,body;
 v_id:=encode(sha256(convert_to(jsonb_build_array(v_tenant,p_world,body->>'operation',stored.intent_id)::text,'UTF8')),'hex');
 v_outcome:=jsonb_build_object('outcome_id',v_id,'outcome_revision',1,'status','succeeded','outcome_digest',v_claim->'binding'->>'request_digest','finalized_at',clock_timestamp());
 update runtime.nexloop_action_claims set claim=stored.claim||jsonb_build_object('state','terminal','terminal_outcome',v_outcome)
  where tenant_id=stored.tenant_id and world=p_world and action_name=stored.action_name and intent_id=stored.intent_id;
 -- Commit tail: writes stay provisional until authority, contract and fences are still current.
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 select definition into d from control.nexloop_action_definitions where tenant_id=v_tenant and world=p_world and resource_id=a->>'resource_id' and active for share;
 if not found or d is distinct from a->'definition' then raise exception 'goal Action changed at commit' using errcode='42501';end if;
 if (a->>'expires_at')::timestamptz<=clock_timestamp() or (permit->>'expires_at')::timestamptz<=clock_timestamp()
  or (v_claim->>'lease_expires_at')::timestamptz<=clock_timestamp()
  or not exists(select 1 from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active and key_material=k) then
  raise exception 'goal authority expired at commit' using errcode='42501';end if;
 return v_result||jsonb_build_object('outcome_id',v_id,'operation',body->>'operation');
end $$;
alter function authz.nexloop_goal_governed_action(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_goal_governed_action(text,text,text,text,text) from public;
grant execute on function authz.nexloop_goal_governed_action(text,text,text,text,text) to nexloop_api,nexloop_domain_worker;

-- 2. Dispatch prediction ------------------------------------------------------------------------------------------
-- The 0068 snapshot check without the caller identity (tenant given by the owner caller): same pause, goal chain,
-- object revision and control-event rules as authz.nexloop_assert_dispatch_controls; raises the same SQLSTATEs.
create function control.nexloop_dispatch_controls_check(p_tenant text,p_world text,p_snapshot jsonb) returns void
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_rev bigint;v_seen bigint;item jsonb;chain text[]:='{}';v_object_revision bigint;hit control.nexloop_control_events%rowtype;
begin
 perform control.nexloop_nx022_dispatch_shape(p_snapshot,true);
 v_seen:=(p_snapshot->>'control_revision')::bigint;
 select revision into v_rev from control.nexloop_control_heads where tenant_id=p_tenant and world=p_world;
 v_rev:=coalesce(v_rev,0);
 if v_seen>v_rev or v_seen<0 then raise exception 'control snapshot from another lineage' using errcode='42501';end if;
 perform control.nexloop_nx022_assert_not_paused(p_tenant,p_world,p_snapshot->'scopes');
 for item in select value from jsonb_array_elements(p_snapshot->'goals') loop
  chain:=chain||control.nexloop_nx022_goal_chain(p_tenant,p_world,item->>'goal_id',(item->>'version')::integer);
 end loop;
 for item in select value from jsonb_array_elements(p_snapshot->'objects') loop
  select nexloop_revision into v_object_revision from ontology.objects where tenant_id=p_tenant and world=p_world and type_name=item->>'type_name' and object_id=item->>'object_id';
  if not found or v_object_revision<>(item->>'revision')::bigint then
   raise exception 'object %/% changed since planning',item->>'type_name',item->>'object_id' using errcode='NXC04';end if;
 end loop;
 select e.* into hit from control.nexloop_control_events e where e.tenant_id=p_tenant and e.world=p_world and e.revision>v_seen
  and (e.scope_kind='tenant'
   or (e.scope_kind='goal' and e.scope_ref=any(chain))
   or (e.scope_kind='budget' and exists(select 1 from jsonb_array_elements_text(p_snapshot->'budgets') b where b=e.scope_ref))
   or exists(select 1 from jsonb_array_elements(p_snapshot->'scopes') s where s->>'kind'=e.scope_kind and s->>'ref'=e.scope_ref))
  order by e.revision limit 1;
 if found then raise exception 'control revision % (%) supersedes snapshot %',hit.revision,hit.event_kind,v_seen using errcode='NXC02';end if;
end $$;
alter function control.nexloop_dispatch_controls_check(text,text,jsonb) owner to nexloop_owner;
revoke all on function control.nexloop_dispatch_controls_check(text,text,jsonb) from public;

-- Signature fixed for the workbench (slice 1 reads it; slice 3 replaces only the body to add taken_over):
--   control.nexloop_intent_dispatch_prediction(p_tenant text, p_world text, p_intent uuid) returns jsonb
--   {"dispatchable": bool, "reason": null | "not_queued" | "control_paused" | "control_revision_stale" | "goal_version_stale"
--    | "object_revision_stale" | "contact_restricted" | "attached_notification" | "taken_over" | "unavailable", "detail": {...}}
-- Owner-only, never raises, writes nothing (contact checks run in a subtransaction that is always rolled back).
create function control.nexloop_intent_dispatch_prediction(p_tenant text,p_world text,p_intent uuid) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);i runtime.nexloop_effect_intents;v_snapshot jsonb;v_state text;v_result jsonb;
 v_code text;v_message text;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into i from runtime.nexloop_effect_intents where intent_id=p_intent and tenant_id=p_tenant and world=p_world;
 if not found then
  perform set_config('eios.tenant_id',coalesce(prior,''),true);
  return jsonb_build_object('dispatchable',false,'reason','unavailable','detail',jsonb_build_object('intent','not_found'));
 end if;
 select o.state into v_state from runtime.nexloop_effect_outbox o where o.intent_id=p_intent;
 -- Queued = accepted and still in the outbox (pending, or leased by a worker whose admission may have been refused).
 if i.state<>'accepted' or v_state is null or v_state not in ('pending','leased') then
  perform set_config('eios.tenant_id',coalesce(prior,''),true);
  return jsonb_build_object('dispatchable',false,'reason','not_queued','detail',jsonb_build_object('intent_state',i.state,'outbox_state',v_state));
 end if;
 select c.snapshot into v_snapshot from runtime.nexloop_effect_dispatch_controls c where c.intent_id=p_intent and c.tenant_id=p_tenant and c.world=p_world
  order by c.captured_at desc,c.run_id limit 1;
 begin
  if v_snapshot is null then raise exception 'control snapshot missing' using errcode='NXC02';end if;
  perform control.nexloop_dispatch_controls_check(p_tenant,p_world,v_snapshot);
  perform control.nexloop_contact_assert_intent(p_tenant,p_world,p_intent);
  v_result:=jsonb_build_object('dispatchable',true,'reason',null,'detail',jsonb_build_object('snapshot_revision',v_snapshot->'control_revision'));
  -- Always undo what the checks may have taken or set in this subtransaction.
  raise exception 'prediction done' using errcode='NXP00';
 exception when others then
  get stacked diagnostics v_code=returned_sqlstate,v_message=message_text;
  if v_code<>'NXP00' then
   v_result:=jsonb_build_object('dispatchable',false,'reason',case v_code when 'NXC01' then 'control_paused' when 'NXC02' then 'control_revision_stale'
     when 'NXC03' then 'goal_version_stale' when 'NXC04' then 'object_revision_stale'
     when 'NXC05' then case when v_message like '%attached_notification%' then 'attached_notification' else 'contact_restricted' end
     when 'NXC06' then 'taken_over' else 'unavailable' end,
    'detail',jsonb_build_object('sqlstate',v_code,'snapshot_revision',v_snapshot->'control_revision'));
  end if;
 end;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return v_result;
end $$;
alter function control.nexloop_intent_dispatch_prediction(text,text,uuid) owner to nexloop_owner;
revoke all on function control.nexloop_intent_dispatch_prediction(text,text,uuid) from public;
