-- NX-027 (temporary number 0133): a recorded CommercialRecord state reaches plans (NX-024) and commitments (NX-026),
-- docs/implementation/NX-027-design.md §7.
--
-- The record keeper's downstream hook (0130 §9, metrics in 0131) now also, once per new object revision:
-- 1. Plans: marks every active plan of the record's linked Consumer for reevaluation (cause external, trigger kind
--    'commercial_event'), e.g. a verified payment lets the plan retire a payment reminder (PRD J01). A pseudonymized or
--    unlinked record wakes nobody. The trigger carries references only, never amounts or the customer reference.
-- 2. Commitments: a commitment bound to a commercial reference (connector, kind, external id; same tenant and world)
--    gets one 'commercial_event' evidence row (ref commercial:<object_id>) when the record reaches a bound status and its
--    Consumer is the commitment's made_to; the keeper is touched and evaluates it as usual. No binding, no evidence:
--    nothing is matched by guessing. A mismatch is an owner-visible exception and writes nothing.
--    runtime.nexloop_commercial_bind_commitment is the owner-internal interface; its caller is the NX-028 human Action
--    (no role is granted it here, like 0106 T7).
-- Published migrations are not edited.

create table runtime.nexloop_commercial_wakes (
 tenant_id text not null,world text not null,record_key text not null check(record_key~'^[0-9a-f]{64}$'),revision bigint not null,
 plans integer not null,evidence integer not null,recorded_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,world,record_key,revision)
);
create table runtime.nexloop_commercial_commitment_bindings (
 tenant_id text not null,world text not null,commitment_id text not null check(commitment_id~'^[0-9a-f]{64}$'),
 connector_id text not null,record_kind text not null check(record_kind in ('order','payment','renewal','refund')),
 external_id text not null check(char_length(external_id) between 1 and 200),
 record_key text not null check(record_key~'^[0-9a-f]{64}$'),statuses text[] not null check(cardinality(statuses)>=1),
 bound_by text not null check(char_length(bound_by) between 1 and 300),bound_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,world,commitment_id,record_key)
);
create index nexloop_commercial_commitment_bindings_record on runtime.nexloop_commercial_commitment_bindings(tenant_id,world,record_key);
do $tables$
declare t text;
begin
 foreach t in array array['runtime.nexloop_commercial_wakes','runtime.nexloop_commercial_commitment_bindings'] loop
  execute format('alter table %s owner to nexloop_owner',t);
  execute format('alter table %s enable row level security',t);
  execute format('alter table %s force row level security',t);
  execute format('create policy tenant_boundary on %s to nexloop_owner using(tenant_id=current_setting(''eios.tenant_id'',true)) with check(tenant_id=current_setting(''eios.tenant_id'',true))',t);
  execute format('revoke all on %s from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity,nexloop_configurator',t);
  execute format('create trigger nx027_append_only before update or delete on %s for each row execute function control.nexloop_nx022_append_only()',t);
 end loop;
end $tables$;

-- Evidence for the commitments bound to one record, from its current object state.
create function runtime.nexloop_commercial_commitment_evidence(p_tenant text,p_world text,p_key text) returns integer
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);r runtime.nexloop_commercial_records;o ontology.objects;b runtime.nexloop_commercial_commitment_bindings;
 cm ontology.objects;n integer:=0;k integer;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into r from runtime.nexloop_commercial_records where tenant_id=p_tenant and world=p_world and record_key=p_key;
 select * into o from ontology.objects where tenant_id=p_tenant and world=p_world and type_name='CommercialRecord' and object_id=r.object_id;
 if o.object_id is null then perform set_config('eios.tenant_id',coalesce(prior,''),true);return 0;end if;
 for b in select * from runtime.nexloop_commercial_commitment_bindings x where x.tenant_id=p_tenant and x.world=p_world and x.record_key=p_key
   and (o.properties->>'status')=any(x.statuses) order by x.commitment_id loop
  select * into cm from ontology.objects where tenant_id=p_tenant and world=p_world and type_name='Commitment' and object_id=b.commitment_id;
  if cm.object_id is null or o.properties->>'consumer_ref' is null or cm.properties->>'made_to' is distinct from o.properties->>'consumer_ref' then
   perform runtime.nexloop_commercial_raise(p_tenant,p_world,'record:'||p_key,'commitment_consumer_mismatch',
    jsonb_build_object('commitment_id',b.commitment_id,'link_state',o.properties->>'link_state'));
   continue;
  end if;
  insert into runtime.nexloop_commitment_evidence(tenant_id,world,commitment_id,kind,ref,occurred_at,recorded_by,detail)
   values(p_tenant,p_world,b.commitment_id,'commercial_event','commercial:'||o.object_id,(o.properties->>'status_at')::timestamptz,'commercial-recorder',
    jsonb_build_object('record_kind',o.properties->>'kind','status',o.properties->>'status','revision',o.nexloop_revision,'verification','verified_signed'))
   on conflict do nothing;
  get diagnostics k=row_count;
  -- A provider clock slightly ahead (max_future_skew_seconds) is evaluated when the evidence time is reached.
  if k>0 then perform runtime.nexloop_commitment_touch(p_tenant,p_world,b.commitment_id,greatest(clock_timestamp(),(o.properties->>'status_at')::timestamptz));n:=n+1;end if;
 end loop;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return n;
end $$;
alter function runtime.nexloop_commercial_commitment_evidence(text,text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_commercial_commitment_evidence(text,text,text) from public;

create function runtime.nexloop_commercial_bind_commitment(p_tenant text,p_world text,p_commitment text,p_connector text,p_kind text,p_external text,
  p_statuses text[],p_bound_by text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);cm ontology.objects;v_key text;v_statuses text[];v_allowed text[];n integer;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into cm from ontology.objects where tenant_id=p_tenant and world=p_world and type_name='Commitment' and object_id=p_commitment;
 if cm.object_id is null or cm.properties->>'status' not in ('conditional','open','in_progress','breached') then
  raise exception 'commitment unavailable for a commercial binding' using errcode='42501';end if;
 if not exists(select 1 from control.nexloop_commercial_connectors c where c.connector_id=p_connector and c.tenant_id=p_tenant and c.world=p_world) then
  raise exception 'commercial connector unavailable' using errcode='42501';end if;
 v_allowed:=case p_kind when 'order' then array['paid','refunded_partial','refunded'] when 'payment' then array['succeeded','refunded_partial','refunded']
  when 'renewal' then array['succeeded','refunded_partial','refunded'] when 'refund' then array['succeeded'] end;
 v_statuses:=coalesce(p_statuses,case p_kind when 'order' then array['paid'] else array['succeeded'] end);
 if v_allowed is null or cardinality(v_statuses)=0 or not v_statuses<@v_allowed or coalesce(p_external,'')='' or char_length(p_external)>200 then
  raise exception 'commercial binding invalid' using errcode='22023';end if;
 v_key:=runtime.nexloop_commercial_key(p_tenant,p_world,p_connector,p_kind,p_external);
 insert into runtime.nexloop_commercial_commitment_bindings(tenant_id,world,commitment_id,connector_id,record_kind,external_id,record_key,statuses,bound_by)
  values(p_tenant,p_world,p_commitment,p_connector,p_kind,p_external,v_key,v_statuses,p_bound_by) on conflict do nothing;
 get diagnostics n=row_count;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 -- A record that already reached a bound status is evidence now.
 return jsonb_build_object('record_key',v_key,'bound',n>0,'evidence',runtime.nexloop_commercial_commitment_evidence(p_tenant,p_world,v_key));
end $$;
alter function runtime.nexloop_commercial_bind_commitment(text,text,text,text,text,text,text[],text) owner to nexloop_owner;
revoke all on function runtime.nexloop_commercial_bind_commitment(text,text,text,text,text,text,text[],text) from public;

create function runtime.nexloop_commercial_wake_plans(p_tenant text,p_world text,p_key text,p_props jsonb,p_object text) returns integer
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare p record;n integer:=0;
begin
 if p_props->>'link_state'<>'linked' or coalesce(p_props->>'consumer_ref','')!~'^[a-f0-9]{64}$' then return 0;end if;
 for p in select a.plan_id from runtime.nexloop_active_plans(p_tenant,p_world) a where a.consumer_id=p_props->>'consumer_ref' order by a.plan_id loop
  perform authz.nexloop_plan_feed_touch(p_tenant,p_world,p.plan_id,jsonb_build_object('kind','commercial_event','cause','external',
   'ref','commercial:'||p_object,'record_kind',p_props->>'kind','status',p_props->>'status','at',clock_timestamp()),null);
  n:=n+1;
 end loop;
 return n;
end $$;
alter function runtime.nexloop_commercial_wake_plans(text,text,text,jsonb,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_commercial_wake_plans(text,text,text,jsonb,text) from public;

create or replace function runtime.nexloop_commercial_on_recorded(p_tenant text,p_world text,p_key text,p_props jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);r runtime.nexloop_commercial_records;v_revision bigint;v_obs integer;v_plans integer:=0;
 v_evidence integer:=0;
begin
 v_obs:=runtime.nexloop_commercial_project(p_tenant,p_world,p_key,p_props);
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into r from runtime.nexloop_commercial_records where tenant_id=p_tenant and world=p_world and record_key=p_key;
 select nexloop_revision into v_revision from ontology.objects where tenant_id=p_tenant and world=p_world and type_name='CommercialRecord' and object_id=r.object_id;
 -- Once per object revision (the command holds the registry row lock): a replayed 'recorded' wakes nobody again.
 if not exists(select 1 from runtime.nexloop_commercial_wakes w where w.tenant_id=p_tenant and w.world=p_world and w.record_key=p_key and w.revision=v_revision) then
  v_plans:=runtime.nexloop_commercial_wake_plans(p_tenant,p_world,p_key,p_props,r.object_id);
  v_evidence:=runtime.nexloop_commercial_commitment_evidence(p_tenant,p_world,p_key);
  perform set_config('eios.tenant_id',p_tenant,true);
  insert into runtime.nexloop_commercial_wakes(tenant_id,world,record_key,revision,plans,evidence) values(p_tenant,p_world,p_key,v_revision,v_plans,v_evidence);
 end if;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return jsonb_build_object('observations',v_obs,'plans',v_plans,'commitment_evidence',v_evidence);
end $$;
