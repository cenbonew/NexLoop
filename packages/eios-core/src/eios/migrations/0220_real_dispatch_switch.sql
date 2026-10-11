-- Real dispatch switch (temporary number 0220; NX-037 prerequisite, dispatcher rulings 2026-10-11).
--
-- An external effect (one executed by a system outside NexLoop: a third-party channel, an enterprise's external endpoint, a
-- real incentive channel) is dispatched only when three layers are open at dispatch: the deployment master switch
-- REAL_DISPATCH_ENABLED (process configuration of the dispatching worker, passed to SQL as the transaction-local setting
-- nexloop.real_dispatch_enabled), the tenant switch and the switch of that Action (action_type = resource_id) in the
-- tenant. Fail closed: no row is closed, world <> 'real' is closed, a tenant switch recorded with data_mode 'test' is closed.
-- A closed layer refuses before any provider request with NXC07 real_dispatch_closed (recorded in the dispatch refusal table
-- on the effect path; the incentive path refuses the intent with the same code).
--
-- 1. Which effects are external: control.nexloop_action_effect_externality, declared by the same trusted configuration as the
--    0112 effect categories (deploy/configuration/action-effect-categories.v*.json, optional external_effect per Action),
--    frozen by the digest of the published definition. Undeclared, a changed definition or anything else is external; only
--    an explicit false is not (today the native WebChat reply / outbound channel and the loopback incentive ledger).
-- 2. Switches: control.nexloop_real_dispatch_switches, changed only by the human owner through the governed entry registry
--    (capability real_dispatch.set, operation set_real_dispatch, 0124). Each change bumps the control revision with a
--    control event (event kind real_dispatch): a snapshot taken before it is superseded at dispatch (NXC02), like every other
--    owner control.
-- 3. Dispatch: authz.nexloop_assert_intent_dispatch_controls (0109 body kept as …_before_real_dispatch_v0220) and the incentive
--    admission (authz.nexloop_incentive_command, kept as …_before_real_dispatch_v0220) check the switches in the admitting
--    transaction.
-- 4. Status for readiness, doctor and the workbench: counts and states only.

-- 0. Refusal code ----------------------------------------------------------------------------------------------------------------
insert into control.nexloop_dispatch_refusal_codes values ('NXC07','real_dispatch_closed');

-- 1. Externality declarations ----------------------------------------------------------------------------------------------------
create table control.nexloop_action_effect_externality (
 tenant_id text not null,world text not null,resource_id text not null check(resource_id~'^eios:action:[A-Za-z][A-Za-z0-9_.]{0,159}:[1-9][0-9]{0,8}$'),
 definition_digest text not null check(definition_digest~'^[0-9a-f]{64}$'),external_effect boolean not null,
 manifest_version integer not null check(manifest_version>=1),configured_by text not null default session_user,
 configured_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,world,resource_id,definition_digest)
);
alter table control.nexloop_action_effect_externality owner to nexloop_owner;
alter table control.nexloop_action_effect_externality enable row level security;
alter table control.nexloop_action_effect_externality force row level security;
create policy tenant_boundary on control.nexloop_action_effect_externality to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
revoke all on control.nexloop_action_effect_externality from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity,nexloop_configurator;
create trigger rds_append_only before update or delete on control.nexloop_action_effect_externality for each row execute function control.nexloop_nx022_append_only();

-- The 0112 configuration function is kept; the wrapper strips the optional external_effect key, applies the categories as
-- before and records each declared externality (immutable per definition digest).
alter function control.nexloop_configure_effect_categories(text,text,jsonb) rename to nexloop_configure_effect_categories_before_externality_v0220;
revoke all on function control.nexloop_configure_effect_categories_before_externality_v0220(text,text,jsonb) from public,nexloop_configurator;
create function control.nexloop_configure_effect_categories(p_tenant text,p_world text,p_manifest jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);e jsonb;stripped jsonb:='[]'::jsonb;v jsonb;d jsonb;v_resource text;v_digest text;
 cur control.nexloop_action_effect_externality;n integer:=0;
begin
 if session_user<>'nexloop_configurator' then raise exception 'effect categories are trusted configuration only' using errcode='42501';end if;
 if jsonb_typeof(p_manifest->'actions') is distinct from 'array' then raise exception 'effect categories manifest invalid' using errcode='22023';end if;
 for e in select value from jsonb_array_elements(p_manifest->'actions') loop
  if jsonb_typeof(e)='object' and e ? 'external_effect' and jsonb_typeof(e->'external_effect') is distinct from 'boolean' then
   raise exception 'effect category entry invalid' using errcode='22023';end if;
  stripped:=stripped||jsonb_build_array(case when jsonb_typeof(e)='object' then e-'external_effect' else e end);
 end loop;
 v:=control.nexloop_configure_effect_categories_before_externality_v0220(p_tenant,p_world,jsonb_set(p_manifest,'{actions}',stripped));
 perform set_config('eios.tenant_id',p_tenant,true);
 for e in select value from jsonb_array_elements(p_manifest->'actions') where value ? 'external_effect' loop
  v_resource:='eios:action:'||(e->>'stable_name')||':'||(e->>'version');
  select definition into d from control.nexloop_action_definitions where tenant_id=p_tenant and world=p_world and resource_id=v_resource and active;
  v_digest:=control.nexloop_definition_digest(d);
  select * into cur from control.nexloop_action_effect_externality x where x.tenant_id=p_tenant and x.world=p_world and x.resource_id=v_resource and x.definition_digest=v_digest;
  if found then
   if cur.external_effect<>(e->>'external_effect')::boolean then raise exception 'external_effect of % is immutable',v_resource using errcode='22023';end if;
  else
   insert into control.nexloop_action_effect_externality(tenant_id,world,resource_id,definition_digest,external_effect,manifest_version)
    values(p_tenant,p_world,v_resource,v_digest,(e->>'external_effect')::boolean,(p_manifest->>'manifest_version')::integer);
   n:=n+1;
  end if;
 end loop;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return v||jsonb_build_object('externality_written',n);
end $$;
alter function control.nexloop_configure_effect_categories(text,text,jsonb) owner to nexloop_owner;
revoke all on function control.nexloop_configure_effect_categories(text,text,jsonb) from public;
grant execute on function control.nexloop_configure_effect_categories(text,text,jsonb) to nexloop_configurator;

-- External unless explicitly declared false for exactly this published definition.
create function control.nexloop_effect_is_external(p_tenant text,p_world text,p_resource text,p_definition jsonb) returns boolean
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);v boolean;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 select x.external_effect into v from control.nexloop_action_effect_externality x where x.tenant_id=p_tenant and x.world=p_world and x.resource_id=p_resource
  and p_definition is not null and x.definition_digest=control.nexloop_definition_digest(p_definition);
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return v is distinct from false;
end $$;
alter function control.nexloop_effect_is_external(text,text,text,jsonb) owner to nexloop_owner;
revoke all on function control.nexloop_effect_is_external(text,text,text,jsonb) from public;

-- 2. Switches ----------------------------------------------------------------------------------------------------------------------
create table control.nexloop_real_dispatch_switches (
 tenant_id text not null,world text not null,scope_kind text not null check(scope_kind in ('tenant','action_type')),
 scope_ref text not null check(scope_ref~'^(\*|eios:action:[A-Za-z][A-Za-z0-9_.]{0,159}:[1-9][0-9]{0,8})$'),
 enabled boolean not null,data_mode text check(data_mode in ('real','test')),revision bigint not null check(revision>=1),
 reason text not null check(length(reason) between 1 and 2000),changed_by text not null,intent_id text not null,
 updated_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,world,scope_kind,scope_ref),
 check((scope_kind='tenant')=(scope_ref='*')),check((scope_kind='tenant')=(data_mode is not null))
);
alter table control.nexloop_real_dispatch_switches owner to nexloop_owner;
alter table control.nexloop_real_dispatch_switches enable row level security;
alter table control.nexloop_real_dispatch_switches force row level security;
create policy tenant_boundary on control.nexloop_real_dispatch_switches to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
revoke all on control.nexloop_real_dispatch_switches from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity,nexloop_configurator;

alter table control.nexloop_control_events drop constraint nexloop_control_events_event_kind_check;
alter table control.nexloop_control_events add constraint nexloop_control_events_event_kind_check
 check(event_kind in ('pause','resume','budget','goal_version','agent_goal','metric','contact_restricted','contact_released','takeover','handback','real_dispatch'));

-- Governed human owner Action (0124 registry). Every change is a control event with a new control revision (the version).
create function control.nexloop_governed_set_real_dispatch(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language plpgsql set search_path=pg_catalog,pg_temp as $$
declare v_rev bigint;v_kind text:=body->>'scope_kind';v_ref text;
begin
 if body->>'operation' is distinct from 'set_real_dispatch' or v_kind not in ('tenant','action_type') or jsonb_typeof(body->'enabled') is distinct from 'boolean'
  or coalesce(length(body->>'reason'),0) not between 1 and 2000 then raise exception 'real dispatch switch invalid' using errcode='22023';end if;
 v_ref:=case when v_kind='tenant' then '*' else body->>'action_type' end;
 if v_ref is null or v_ref!~'^(\*|eios:action:[A-Za-z][A-Za-z0-9_.]{0,159}:[1-9][0-9]{0,8})$'
  or (v_kind='tenant' and coalesce(body->>'data_mode','') not in ('real','test')) or (v_kind='action_type' and body ? 'data_mode') then
  raise exception 'real dispatch switch invalid' using errcode='22023';end if;
 if control.nexloop_workbench_role(p_tenant,p_principal) is distinct from 'owner' then raise exception 'real dispatch switch is owner only' using errcode='42501';end if;
 if p_world<>'real' and (body->>'enabled')::boolean then raise exception 'real dispatch exists only in world real' using errcode='22023';end if;
 v_rev:=control.nexloop_nx022_bump(p_tenant,p_world,'real_dispatch',v_kind,v_ref,
  jsonb_build_object('enabled',(body->>'enabled')::boolean,'data_mode',body->>'data_mode'),p_principal,p_intent);
 perform set_config('eios.tenant_id',p_tenant,true);
 insert into control.nexloop_real_dispatch_switches(tenant_id,world,scope_kind,scope_ref,enabled,data_mode,revision,reason,changed_by,intent_id,updated_at)
  values(p_tenant,p_world,v_kind,v_ref,(body->>'enabled')::boolean,body->>'data_mode',v_rev,body->>'reason',p_principal,p_intent,clock_timestamp())
  on conflict(tenant_id,world,scope_kind,scope_ref) do update set enabled=excluded.enabled,data_mode=excluded.data_mode,revision=excluded.revision,
   reason=excluded.reason,changed_by=excluded.changed_by,intent_id=excluded.intent_id,updated_at=excluded.updated_at;
 return jsonb_build_object('scope_kind',v_kind,'action_type',case when v_kind='action_type' then v_ref end,'enabled',(body->>'enabled')::boolean,'control_revision',v_rev);
end $$;
alter function control.nexloop_governed_set_real_dispatch(text,text,text,text,text,jsonb) owner to nexloop_owner;
revoke all on function control.nexloop_governed_set_real_dispatch(text,text,text,text,text,jsonb) from public;
insert into control.nexloop_governed_capabilities(capability,operation,subject_rule,handler,source_task) values
 ('real_dispatch.set','set_real_dispatch','human','control.nexloop_governed_set_real_dispatch(text,text,text,text,text,jsonb)','NX-037');

-- 3. Decision at dispatch ------------------------------------------------------------------------------------------------------------
-- The layer that is closed first (deployment, world, tenant, action), or null when all are open. Read under a share lock on the
-- control head, so a concurrent switch change is either seen or ordered after the dispatching transaction.
create function control.nexloop_real_dispatch_closed_layer(p_tenant text,p_world text,p_resource text) returns text
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);t control.nexloop_real_dispatch_switches;a control.nexloop_real_dispatch_switches;v text;
begin
 if coalesce(current_setting('nexloop.real_dispatch_enabled',true),'') is distinct from 'true' then return 'deployment';end if;
 if p_world is distinct from 'real' then return 'world';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 perform 1 from control.nexloop_control_heads where tenant_id=p_tenant and world=p_world for share;
 select * into t from control.nexloop_real_dispatch_switches where tenant_id=p_tenant and world=p_world and scope_kind='tenant' and scope_ref='*';
 select * into a from control.nexloop_real_dispatch_switches where tenant_id=p_tenant and world=p_world and scope_kind='action_type' and scope_ref=p_resource;
 v:=case when t.enabled is not true or t.data_mode is distinct from 'real' then 'tenant' when a.enabled is not true then 'action' end;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return v;
end $$;
alter function control.nexloop_real_dispatch_closed_layer(text,text,text) owner to nexloop_owner;
revoke all on function control.nexloop_real_dispatch_closed_layer(text,text,text) from public;

-- A switch change after the snapshot supersedes it (NXC02), like every other owner control. A tenant-level change is a
-- 'tenant' scope event (0068 already supersedes every snapshot of the tenant); an Action-level change supersedes the
-- snapshots of work on that Action.
create function control.nexloop_real_dispatch_superseded(p_tenant text,p_world text,p_resource text,p_snapshot jsonb) returns void
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);v_seen bigint:=coalesce((p_snapshot->>'control_revision')::bigint,-1);hit bigint;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 select e.revision into hit from control.nexloop_control_events e where e.tenant_id=p_tenant and e.world=p_world and e.event_kind='real_dispatch'
  and e.revision>v_seen and e.scope_kind='action_type' and e.scope_ref=p_resource order by e.revision limit 1;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 if hit is not null then raise exception 'control revision % (real_dispatch) supersedes snapshot %',hit,v_seen using errcode='NXC02';end if;
end $$;
alter function control.nexloop_real_dispatch_superseded(text,text,text,jsonb) owner to nexloop_owner;
revoke all on function control.nexloop_real_dispatch_superseded(text,text,text,jsonb) from public;

-- Effect path: the 0109 intent check is kept and then the switch is checked for an external effect.
alter function authz.nexloop_assert_intent_dispatch_controls(text,text,uuid) rename to nexloop_assert_intent_dispatch_controls_before_real_dispatch_v0220;
revoke all on function authz.nexloop_assert_intent_dispatch_controls_before_real_dispatch_v0220(text,text,uuid) from public,nexloop_action_worker,nexloop_domain_worker,nexloop_api;
create function authz.nexloop_assert_intent_dispatch_controls(p_digest text,p_world text,p_intent uuid) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_result jsonb;ident jsonb;v_tenant text;i runtime.nexloop_effect_intents;v_resource text;v_layer text;v_snapshot jsonb;
begin
 v_result:=authz.nexloop_assert_intent_dispatch_controls_before_real_dispatch_v0220(p_digest,p_world,p_intent);
 ident:=control.nexloop_nx022_identity(p_digest,p_world,true);v_tenant:=ident->'binding'->>'tenant_id';
 perform set_config('eios.tenant_id',v_tenant,true);
 select * into i from runtime.nexloop_effect_intents where intent_id=p_intent and tenant_id=v_tenant and world=p_world;
 if not found then raise exception 'effect intent unavailable' using errcode='42501';end if;
 v_resource:='eios:action:'||i.action_name||':'||i.action_version;
 select c.snapshot into v_snapshot from runtime.nexloop_effect_dispatch_controls c
  where c.intent_id=p_intent and c.tenant_id=v_tenant and c.world=p_world order by c.captured_at desc,c.run_id limit 1;
 perform control.nexloop_real_dispatch_superseded(v_tenant,p_world,v_resource,v_snapshot);
 if control.nexloop_effect_is_external(v_tenant,p_world,v_resource,i.action_definition) then
  v_layer:=control.nexloop_real_dispatch_closed_layer(v_tenant,p_world,v_resource);
  if v_layer is not null then raise exception 'real dispatch closed (%)',v_layer using errcode='NXC07';end if;
 end if;
 perform set_config('eios.tenant_id',v_tenant,true);
 return v_result;
end $$;
alter function authz.nexloop_assert_intent_dispatch_controls(text,text,uuid) owner to nexloop_owner;
revoke all on function authz.nexloop_assert_intent_dispatch_controls(text,text,uuid) from public;
grant execute on function authz.nexloop_assert_intent_dispatch_controls(text,text,uuid) to nexloop_action_worker,nexloop_domain_worker,nexloop_api;

-- Incentive path: an accepted intent of an external incentive Action is refused (terminal, never sent) while a layer is closed.
alter function authz.nexloop_incentive_command(text,text,text,text,text) rename to nexloop_incentive_command_before_real_dispatch_v0220;
revoke all on function authz.nexloop_incentive_command_before_real_dispatch_v0220(text,text,text,text,text) from public,nexloop_domain_worker;
create function authz.nexloop_incentive_command(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare c jsonb:=p_payload::jsonb;t text;i runtime.nexloop_incentive_issues;d jsonb;v_resource text:='eios:action:nexloop.incentive.issue:1';
begin
 if c->>'verb'='admit' then
  t:=authz.nexloop_plan_port_tenant(p_digest,p_world,p_text,p_signature,p_payload,'nexloop-incentive-v1','eios:action:nexloop.incentive.execute:1',array['nexloop_domain_worker']);
  perform set_config('eios.tenant_id',t,true);
  select * into i from runtime.nexloop_incentive_issues where tenant_id=t and world=p_world and intent_id=c->>'intent_id' for update;
  if found and i.state='accepted' then
   select definition into d from control.nexloop_action_definitions where tenant_id=t and world=p_world and resource_id=v_resource and active;
   if control.nexloop_effect_is_external(t,p_world,v_resource,d) and (control.nexloop_real_dispatch_closed_layer(t,p_world,v_resource) is not null
     or exists(select 1 from control.nexloop_control_events e where e.tenant_id=t and e.world=p_world and e.event_kind='real_dispatch'
       and e.scope_kind='action_type' and e.scope_ref=v_resource and e.revision>coalesce((i.control_snapshot->>'control_revision')::bigint,-1))) then
    perform set_config('eios.tenant_id',t,true);
    update runtime.nexloop_incentive_issues set state='refused',refusal_code='real_dispatch_closed',finished_at=clock_timestamp(),updated_at=clock_timestamp()
     where tenant_id=t and world=p_world and intent_id=i.intent_id;
    insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,payload,created_at)
     values('incentive-refused:'||i.intent_id,'nexloop.incentive.issue.refused',t,'service:'||((p_text::jsonb)->>'principal_id'),'incentive',i.intent_id,
      jsonb_build_object('code','real_dispatch_closed'),clock_timestamp()) on conflict do nothing;
    return jsonb_build_object('intent_id',i.intent_id,'admitted',false,'state','refused','code','real_dispatch_closed');
   end if;
  end if;
 end if;
 return authz.nexloop_incentive_command_before_real_dispatch_v0220(p_digest,p_world,p_text,p_signature,p_payload);
end $$;
alter function authz.nexloop_incentive_command(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_incentive_command(text,text,text,text,text) from public;
grant execute on function authz.nexloop_incentive_command(text,text,text,text,text) to nexloop_domain_worker;

-- 4. Status (states and counts only) -------------------------------------------------------------------------------------------------
-- Deployment-wide counts read the switches of every tenant: a read-only owner policy that only this function enables.
create policy summary_read on control.nexloop_real_dispatch_switches for select to nexloop_owner
 using(current_setting('nexloop.real_dispatch_summary',true)='on');
create function runtime.nexloop_real_dispatch_summary() returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v jsonb;
begin
 perform set_config('nexloop.real_dispatch_summary','on',true);
 select jsonb_build_object(
  'tenants_open',(select count(*) from control.nexloop_real_dispatch_switches where scope_kind='tenant' and world='real' and enabled and data_mode='real'),
  'actions_open',(select count(*) from control.nexloop_real_dispatch_switches a where a.scope_kind='action_type' and a.world='real' and a.enabled
    and exists(select 1 from control.nexloop_real_dispatch_switches t where t.tenant_id=a.tenant_id and t.world=a.world and t.scope_kind='tenant' and t.enabled and t.data_mode='real'))) into v;
 perform set_config('nexloop.real_dispatch_summary','off',true);
 return v;
end $$;
alter function runtime.nexloop_real_dispatch_summary() owner to nexloop_owner;
revoke all on function runtime.nexloop_real_dispatch_summary() from public;
create function authz.nexloop_real_dispatch_summary() returns jsonb
 language sql stable security definer set search_path=pg_catalog,pg_temp as $$ select runtime.nexloop_real_dispatch_summary() $$;
alter function authz.nexloop_real_dispatch_summary() owner to nexloop_owner;
revoke all on function authz.nexloop_real_dispatch_summary() from public;
grant execute on function authz.nexloop_real_dispatch_summary() to nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_metrics;

-- Workbench (owner / member read of the own tenant): the switches and their versions, no reasons or principals.
create function runtime.nexloop_real_dispatch_switches_read(p_tenant text,p_world text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);v jsonb;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 select jsonb_build_object('world',p_world,
  'tenant',(select jsonb_build_object('enabled',s.enabled,'data_mode',s.data_mode,'revision',s.revision) from control.nexloop_real_dispatch_switches s
    where s.tenant_id=p_tenant and s.world=p_world and s.scope_kind='tenant'),
  'actions',(select coalesce(jsonb_agg(jsonb_build_object('action_type',s.scope_ref,'enabled',s.enabled,'revision',s.revision) order by s.scope_ref),'[]'::jsonb)
    from control.nexloop_real_dispatch_switches s where s.tenant_id=p_tenant and s.world=p_world and s.scope_kind='action_type')) into v;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return v;
end $$;
alter function runtime.nexloop_real_dispatch_switches_read(text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_real_dispatch_switches_read(text,text) from public;

-- The M12 snapshot carries the tenant's switches (the workbench metrics view and the read-only metrics export show them).
alter function runtime.nexloop_metrics_snapshot(text,text) rename to nexloop_metrics_snapshot_before_real_dispatch_v0220;
revoke all on function runtime.nexloop_metrics_snapshot_before_real_dispatch_v0220(text,text) from public;
create function runtime.nexloop_metrics_snapshot(p_tenant text,p_world text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
begin
 return runtime.nexloop_metrics_snapshot_before_real_dispatch_v0220(p_tenant,p_world)||jsonb_build_object('real_dispatch',runtime.nexloop_real_dispatch_switches_read(p_tenant,p_world));
end $$;
alter function runtime.nexloop_metrics_snapshot(text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_metrics_snapshot(text,text) from public;
