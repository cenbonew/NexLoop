-- NX-062 M3 (docs/implementation/NX-062-design.md §2.1, §3.1 steps 1-2, §3.5; L6 C4): the machine_key realm's admission - identity,
-- rate limits from a versioned policy that fails closed, and counted outcomes for the alert rules - and the capability list of a key.
-- The migration number is temporary; it lives only in the file name and the catalog.
--
-- * control.nexloop_machine_rate_policies: append-only revisions published by trusted configuration (nexloop_configurator, from
--   deploy/configuration/machine-rate-limits.v<N>.json through control.nexloop_publish_machine_rate_policy); the latest revision is in
--   force. With no revision every machine request is refused (rate_policy_missing) and counted - never unlimited (C4).
-- * runtime.nexloop_machine_rate_buckets: fixed one-minute windows per key and per tenant, keyed by the key id and the tenant derived
--   from the authenticated key (never a client address); rows older than a day are deleted by the admission itself.
-- * runtime.nexloop_machine_call_counts: per UTC day, tenant ('' when the key did not authenticate) and outcome (admitted, denied,
--   rate_limited, rate_policy_missing), for the metrics block machine_access and the alert rules.
-- * authz.nexloop_machine_admit(prefix, digest): the realm's first step for nexloop_api. It never raises for a refused key: it returns
--   {status} - ok with the identity, denied, rate_limited with retry_after, rate_policy_missing - and commits the count, so a refusal is
--   seen by the alerts. One HMAC was computed by the API with the row's pepper version (C2); validity is read at clock_timestamp().
-- * authz.nexloop_machine_capability_list(prefix, digest): the capabilities a key may call - its scope ∩ the current allowlist ∩ the
--   client's grants. Machine grants arrive with M5 (design §10 slice change), so until then the list is empty for every key.
-- * runtime.nexloop_metrics_snapshot is renamed once (..._before_machine_access) and wrapped: the real world adds machine_access.
set local timezone = 'UTC';

create table control.nexloop_machine_rate_policies (
 revision integer primary key check(revision>=1),
 per_key_per_minute integer not null check(per_key_per_minute between 1 and 10000),
 per_tenant_per_minute integer not null check(per_tenant_per_minute between 1 and 100000),
 manifest_version integer not null check(manifest_version>=1),
 digest text not null check(digest~'^[0-9a-f]{64}$'),
 recorded_at timestamptz not null default clock_timestamp(),
 check(per_tenant_per_minute>=per_key_per_minute)
);
create table runtime.nexloop_machine_rate_buckets (
 bucket text not null check(bucket in ('key','tenant')),
 bucket_key text not null check(length(bucket_key) between 1 and 200),
 window_start timestamptz not null,
 calls integer not null check(calls>=0),
 primary key(bucket,bucket_key,window_start)
);
create table runtime.nexloop_machine_call_counts (
 day date not null,
 tenant_id text not null,
 outcome text not null check(outcome in ('admitted','denied','rate_limited','rate_policy_missing')),
 calls bigint not null check(calls>=0),
 primary key(day,tenant_id,outcome)
);
create trigger nx062_append_only before update or delete on control.nexloop_machine_rate_policies for each row execute function control.nexloop_nx022_append_only();
create trigger nx062_no_truncate before truncate on control.nexloop_machine_rate_policies for each statement execute function control.nexloop_nx022_append_only();
do $$declare t text;begin
 foreach t in array array['control.nexloop_machine_rate_policies','runtime.nexloop_machine_rate_buckets','runtime.nexloop_machine_call_counts'] loop
  execute format('alter table %s owner to nexloop_owner',t);
  execute format('alter table %s enable row level security',t);
  execute format('alter table %s force row level security',t);
  execute format('create policy nx062_owner on %s to nexloop_owner using(true) with check(true)',t);
  execute format('revoke all on %s from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity,nexloop_configurator',t);
 end loop;
end $$;

-- Trusted configuration publishes a policy; an unchanged policy (same digest as the latest) writes nothing.
create function control.nexloop_publish_machine_rate_policy(p_policy jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare latest control.nexloop_machine_rate_policies;v_digest text;v_revision integer;
begin
 if session_user is distinct from 'nexloop_configurator' then raise exception 'machine rate policy requires trusted configuration' using errcode='42501';end if;
 if jsonb_typeof(p_policy) is distinct from 'object'
  or (select array_agg(k order by k) from jsonb_object_keys(p_policy) k) is distinct from array['manifest_version','per_key_per_minute','per_tenant_per_minute','schema_version']
  or p_policy->>'schema_version' is distinct from 'nexloop-machine-rate-limits/1'
  or jsonb_typeof(p_policy->'manifest_version') is distinct from 'number' or jsonb_typeof(p_policy->'per_key_per_minute') is distinct from 'number'
  or jsonb_typeof(p_policy->'per_tenant_per_minute') is distinct from 'number' then
  raise exception 'machine rate policy invalid' using errcode='22023';end if;
 v_digest:=encode(sha256(convert_to(p_policy::text,'UTF8')),'hex');
 select * into latest from control.nexloop_machine_rate_policies order by revision desc limit 1;
 if found and latest.digest=v_digest then return jsonb_build_object('revision',latest.revision,'changed',false);end if;
 if found and (p_policy->>'manifest_version')::integer<=latest.manifest_version then raise exception 'machine rate policy version must increase' using errcode='22023';end if;
 v_revision:=coalesce(latest.revision,0)+1;
 insert into control.nexloop_machine_rate_policies(revision,per_key_per_minute,per_tenant_per_minute,manifest_version,digest)
  values(v_revision,(p_policy->>'per_key_per_minute')::integer,(p_policy->>'per_tenant_per_minute')::integer,(p_policy->>'manifest_version')::integer,v_digest);
 return jsonb_build_object('revision',v_revision,'changed',true);
end $$;

create function runtime.nexloop_machine_count(p_tenant text,p_outcome text) returns void
 language sql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
 insert into runtime.nexloop_machine_call_counts(day,tenant_id,outcome,calls) values((clock_timestamp() at time zone 'UTC')::date,coalesce(p_tenant,''),p_outcome,1)
  on conflict(day,tenant_id,outcome) do update set calls=runtime.nexloop_machine_call_counts.calls+1
$$;

-- One window step: count this call in its minute; the calls of the window after this one.
create function runtime.nexloop_machine_bucket_step(p_bucket text,p_key text,p_window timestamptz) returns integer
 language sql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
 insert into runtime.nexloop_machine_rate_buckets(bucket,bucket_key,window_start,calls) values(p_bucket,p_key,p_window,1)
  on conflict(bucket,bucket_key,window_start) do update set calls=runtime.nexloop_machine_rate_buckets.calls+1
  returning calls
$$;

create function authz.nexloop_machine_admit(p_prefix text,p_digest text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare ident jsonb;pol control.nexloop_machine_rate_policies;v_tenant text;v_key text;v_now timestamptz:=clock_timestamp();
 v_window timestamptz:=date_trunc('minute',clock_timestamp());k integer;t integer;
begin
 begin
  ident:=authz.nexloop_machine_identity_snapshot(p_prefix,p_digest);
 exception when insufficient_privilege then
  perform runtime.nexloop_machine_count('','denied');
  return jsonb_build_object('status','denied');
 end;
 v_tenant:=ident->'binding'->>'tenant_id';v_key:=ident->'binding'->>'credential_id';
 select * into pol from control.nexloop_machine_rate_policies order by revision desc limit 1;
 if not found then
  perform runtime.nexloop_machine_count(v_tenant,'rate_policy_missing');
  return jsonb_build_object('status','rate_policy_missing');
 end if;
 delete from runtime.nexloop_machine_rate_buckets where window_start<v_now-interval '1 day';
 k:=runtime.nexloop_machine_bucket_step('key',v_key,v_window);
 t:=runtime.nexloop_machine_bucket_step('tenant',v_tenant,v_window);
 if k>pol.per_key_per_minute or t>pol.per_tenant_per_minute then
  perform runtime.nexloop_machine_count(v_tenant,'rate_limited');
  return jsonb_build_object('status','rate_limited','retry_after',greatest(1,ceil(extract(epoch from v_window+interval '1 minute'-v_now))::integer));
 end if;
 perform runtime.nexloop_machine_count(v_tenant,'admitted');
 return jsonb_build_object('status','ok','identity',ident);
end $$;

-- The capability list: scope ∩ current allowlist ∩ the client's grants. No machine grant exists before M5, so the grant term is false.
create function authz.nexloop_machine_capability_list(p_prefix text,p_digest text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare ident jsonb:=authz.nexloop_machine_identity_snapshot(p_prefix,p_digest);v jsonb;
begin
 select coalesce(jsonb_agg(jsonb_build_object('capability',c.capability,'kind',c.kind,'description',c.description) order by c.capability),'[]'::jsonb) into v
  from (select distinct on (capability) capability,kind,description,state from control.nexloop_machine_capabilities order by capability,revision desc) c
  where c.state='active' and c.capability in (select jsonb_array_elements_text(ident->'scope'))
   and control.nexloop_machine_capability_current(c.capability)
   and false;  -- M5: the client's current grant of the capability (machine fact snapshot, design §10 C7)
 return v;
end $$;

-- Metrics block (real world): whether a rate policy is in force and the last 24 hours of outcomes (this tenant; denied keys carry no tenant).
create function runtime.nexloop_machine_access_snapshot(p_tenant text) returns jsonb
 language sql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
 select jsonb_build_object(
  'rate_policy_missing',case when exists(select 1 from control.nexloop_machine_rate_policies) then 0 else 1 end,
  'denied_24h',coalesce((select sum(calls) from runtime.nexloop_machine_call_counts where outcome='denied' and tenant_id=''
     and day>=((clock_timestamp()-interval '1 day') at time zone 'UTC')::date),0),
  'refused_policy_missing_24h',coalesce((select sum(calls) from runtime.nexloop_machine_call_counts where outcome='rate_policy_missing' and tenant_id=p_tenant
     and day>=((clock_timestamp()-interval '1 day') at time zone 'UTC')::date),0),
  'rate_limited_24h',coalesce((select sum(calls) from runtime.nexloop_machine_call_counts where outcome='rate_limited' and tenant_id=p_tenant
     and day>=((clock_timestamp()-interval '1 day') at time zone 'UTC')::date),0),
  'admitted_24h',coalesce((select sum(calls) from runtime.nexloop_machine_call_counts where outcome='admitted' and tenant_id=p_tenant
     and day>=((clock_timestamp()-interval '1 day') at time zone 'UTC')::date),0))
$$;

alter function runtime.nexloop_metrics_snapshot(text,text) rename to nexloop_metrics_snapshot_before_machine_access;
revoke all on function runtime.nexloop_metrics_snapshot_before_machine_access(text,text) from public;
create function runtime.nexloop_metrics_snapshot(p_tenant text,p_world text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v jsonb:=runtime.nexloop_metrics_snapshot_before_machine_access(p_tenant,p_world);
begin
 if p_world is distinct from 'real' then return v;end if;
 return v||jsonb_build_object('machine_access',runtime.nexloop_machine_access_snapshot(p_tenant));
end $$;
-- As the 0434 fix (nx062_service_snapshot_grants): the wrapper gets every EXECUTE grantee of the renamed function, which keeps none.
do $$declare g text;
begin
 for g in select r.rolname from pg_proc p cross join aclexplode(p.proacl) a join pg_roles r on r.oid=a.grantee
   where p.oid='runtime.nexloop_metrics_snapshot_before_machine_access(text,text)'::regprocedure and a.privilege_type='EXECUTE'
    and a.grantee<>p.proowner group by r.rolname loop
  execute format('grant execute on function runtime.nexloop_metrics_snapshot(text,text) to %I',g);
  execute format('revoke execute on function runtime.nexloop_metrics_snapshot_before_machine_access(text,text) from %I',g);
 end loop;
end $$;

do $$declare f text;begin
 foreach f in array array['control.nexloop_publish_machine_rate_policy(jsonb)','runtime.nexloop_machine_count(text,text)',
  'runtime.nexloop_machine_bucket_step(text,text,timestamp with time zone)','authz.nexloop_machine_admit(text,text)',
  'authz.nexloop_machine_capability_list(text,text)','runtime.nexloop_machine_access_snapshot(text)','runtime.nexloop_metrics_snapshot(text,text)'] loop
  execute format('alter function %s owner to nexloop_owner',f);
  execute format('revoke all on function %s from public',f);
 end loop;
end $$;
grant execute on function control.nexloop_publish_machine_rate_policy(jsonb) to nexloop_configurator;
grant execute on function authz.nexloop_machine_admit(text,text),authz.nexloop_machine_capability_list(text,text) to nexloop_api;
