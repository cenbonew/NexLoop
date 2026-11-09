-- D23 option B (L6 G1 on 0431 action_input_shadow): authz.nexloop_action_input_tenant caught every error of the service identity
-- resolution and turned it into "no tenant", so a database fault, a bug or a timeout silently dropped the shadow counts. Identity
-- resolution (authz.nexloop_service_identity, 0034, and the root identity it calls) refuses only with 42501 (insufficient_privilege):
-- only that is "not a service credential here" and falls through to the browser realm (fail closed for a machine digest or an
-- unknown one, as before). Any other error is raised; the governor's shadow_validate then leaves dispatch untouched, counts
-- nothing and counts the failure itself (action_governor.SHADOW_FAILURES, with a warning naming the error class).
create or replace function authz.nexloop_action_input_tenant(p_digest text,p_world text) returns text
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v text;
begin
 if coalesce(p_digest,'')!~'^[a-f0-9]{64}$' then return null;end if;
 begin
  v:=authz.nexloop_service_identity(p_digest,p_world)->'binding'->>'tenant_id';
 exception when insufficient_privilege then v:=null;
 end;
 if v is null then
  select r.tenant_id into v from authz.nexloop_browser_token_realms r where r.token_digest=p_digest;
 end if;
 return v;
end $$;
alter function authz.nexloop_action_input_tenant(text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_action_input_tenant(text,text) from public;

-- L6 J1 on 944be8b5: a process counter resets on restart and nobody reads it. Every shadow validation that could not run is also
-- counted in PostgreSQL, per world, UTC day and reason code (the error class name, never a message, request or principal), by
-- authz.nexloop_action_input_shadow_failure, which the governor calls on a fresh connection after the failure (if that call fails
-- too, only the log line and the process counter remain). The table holds no tenant: the failure may be the tenant lookup itself,
-- and a shadow that cannot run is a deployment fault, so every tenant's snapshot shows the world's count.
create table runtime.nexloop_action_input_shadow_failures (
 world text not null check(world~'^[a-z][a-z_]{0,31}$'),day date not null,reason text not null check(reason~'^[A-Za-z][A-Za-z0-9_]{0,63}$'),
 failures bigint not null check(failures>0),last_at timestamptz not null,primary key(world,day,reason)
);
alter table runtime.nexloop_action_input_shadow_failures owner to nexloop_owner;
revoke all on runtime.nexloop_action_input_shadow_failures from public;

create function authz.nexloop_action_input_shadow_failure(p_world text,p_reason text) returns boolean
 language plpgsql security definer set search_path=pg_catalog,pg_temp as $$
begin
 if coalesce(p_world,'')!~'^[a-z][a-z_]{0,31}$' or coalesce(p_reason,'')!~'^[A-Za-z][A-Za-z0-9_]{0,63}$' then return false;end if;
 insert into runtime.nexloop_action_input_shadow_failures(world,day,reason,failures,last_at)
  values(p_world,(clock_timestamp() at time zone 'UTC')::date,p_reason,1,clock_timestamp())
  on conflict(world,day,reason) do update set failures=runtime.nexloop_action_input_shadow_failures.failures+1,last_at=excluded.last_at;
 return true;
end $$;
alter function authz.nexloop_action_input_shadow_failure(text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_action_input_shadow_failure(text,text) from public;
grant execute on function authz.nexloop_action_input_shadow_failure(text,text)
 to nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime;

-- L6 J2 (G1 point 2): the per-candidate report never says "clean" for an Action that was never called (no_data), but nothing said
-- whether the shadow as a whole covered anything. authz.nexloop_action_input_shadow_summary gives the coverage: candidates
-- configured, candidates with at least one validated dispatch since p_since (covered), validated dispatches, would-rejects and the
-- shadow failures of the world since p_since; status not_ready when no candidate is configured, none is covered, or any shadow
-- failed (the counts may be missing), fail on any would-reject, else pass. Never pass on an empty or uncovered configuration.
create function authz.nexloop_action_input_shadow_summary(p_tenant text,p_world text,p_since timestamptz) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare r jsonb:=authz.nexloop_action_input_shadow_report(p_tenant,p_world,p_since);n int;k int;calls bigint;rej bigint;fails bigint;
begin
 select count(*),count(*) filter (where (x->>'calls')::bigint>0 and x->>'status'<>'stale'),coalesce(sum((x->>'calls')::bigint),0),
  coalesce(sum((x->>'would_reject')::bigint),0) into n,k,calls,rej from jsonb_array_elements(r) x;
 select coalesce(sum(f.failures),0) into fails from runtime.nexloop_action_input_shadow_failures f
  where f.world=p_world and f.day>=(p_since at time zone 'UTC')::date;
 return jsonb_build_object('candidates',n,'covered',k,'coverage',case when n=0 then 0 else round(k::numeric/n,4) end,'validated_calls',calls,
  'would_reject',rej,'shadow_failures',fails,
  'status',case when n=0 or k=0 or fails>0 then 'not_ready' when rej>0 then 'fail' else 'pass' end);
end $$;
alter function authz.nexloop_action_input_shadow_summary(text,text,timestamptz) owner to nexloop_owner;
revoke all on function authz.nexloop_action_input_shadow_summary(text,text,timestamptz) from public;
grant execute on function authz.nexloop_action_input_shadow_summary(text,text,timestamptz) to nexloop_metrics;

-- The metrics snapshot gains the block action_input_shadow {failures_24h} (the 0427 and 0437 rename-and-wrap pattern, on top of 0437's
-- _before_machine_access wrapper; one public name remains), read by the alert
-- rule action_input_shadow_failures (rules v36: > 0 in 24 hours, warning). Shadow failures are recorded per world only, so the
-- block is the same for every tenant of the world. failures_24h sums the days whose last failure is within 24 hours, so it may count
-- up to one day more than the last 24 hours (it only feeds a > 0 rule).
alter function runtime.nexloop_metrics_snapshot(text,text) rename to nexloop_metrics_snapshot_before_shadow_failures;
revoke all on function runtime.nexloop_metrics_snapshot_before_shadow_failures(text,text) from public;
create function runtime.nexloop_metrics_snapshot(p_tenant text,p_world text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v jsonb:=runtime.nexloop_metrics_snapshot_before_shadow_failures(p_tenant,p_world);n bigint;
begin
 if jsonb_typeof(v) is distinct from 'object' then return v;end if;
 select coalesce(sum(f.failures),0) into n from runtime.nexloop_action_input_shadow_failures f
  where f.world=p_world and f.last_at>clock_timestamp()-interval '24 hours';
 return v||jsonb_build_object('action_input_shadow',jsonb_build_object('failures_24h',n));
end $$;
alter function runtime.nexloop_metrics_snapshot(text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_metrics_snapshot(text,text) from public;
-- L6 P1 on a6692f1c: as 0437 (nx062_machine_realm) and the 0434 fix, the wrapper gets every EXECUTE grantee of the function it wraps
-- (the alert evaluator, the API, the metrics login), and the renamed function keeps none: nothing calls past the wrapper.
do $$declare g text;
begin
 for g in select r.rolname from pg_proc p cross join aclexplode(p.proacl) a join pg_roles r on r.oid=a.grantee
   where p.oid='runtime.nexloop_metrics_snapshot_before_shadow_failures(text,text)'::regprocedure and a.privilege_type='EXECUTE'
    and a.grantee<>p.proowner group by r.rolname loop
  execute format('grant execute on function runtime.nexloop_metrics_snapshot(text,text) to %I',g);
  execute format('revoke execute on function runtime.nexloop_metrics_snapshot_before_shadow_failures(text,text) from %I',g);
 end loop;
end $$;
