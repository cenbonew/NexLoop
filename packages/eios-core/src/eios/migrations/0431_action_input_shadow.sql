-- D23 option B, batch 0 (docs/implementation/d23-option-b-design.md §3 C3; dispatcher 2026-11-09): shadow validation of candidate
-- input schemas. A candidate is validated at dispatch next to the published schema, counted, never enforced; nothing of the request
-- and nobody's identity is kept. The migration number lives only in the file name and the catalog.
--
-- 1. control.nexloop_action_input_candidates: per tenant and world, a candidate input schema for an active Action version (its exact
--    resource id), written by trusted configuration only (control.nexloop_configure_input_candidates, session user
--    nexloop_configurator); the set is replaced as a whole and keeps its revision.
-- 2. runtime.nexloop_action_input_shadow: per tenant, world, Action version, candidate digest and UTC day, two counts: calls and the
--    calls the candidate would have refused. Action names and counts only: no request body, no principal, no Consumer - so it holds
--    no personal data and is outside erasure coverage (docs/implementation/erasure-coverage.md applies to tables naming a Consumer or
--    a member; this one names neither); it goes with its tenant (FK).
-- 3. authz.nexloop_action_input_candidate(p_digest, p_world, p_resource): the candidate (schema and digest) for the caller's tenant,
--    tenant from the caller's credential (service identity or browser session realm), never from the caller; null when none.
-- 4. authz.nexloop_action_input_shadow_record(p_digest, p_world, p_resource, p_digest_candidate, p_rejected): adds one call (and one
--    would-reject); a candidate that is not the current one for that Action is not counted.
-- 5. authz.nexloop_action_input_shadow_report(p_tenant, p_world, p_since): per candidate, calls and would_reject since p_since and
--    a status: no_data (no call: never published, C3), fail (any would-reject), pass, or stale (the Action version is no longer
--    active). For the read-only metrics login and the owner's read session.
create table control.nexloop_action_input_candidates (
 tenant_id text not null references control.nexloop_tenants(tenant_id),world text not null,
 resource_id text not null check(resource_id~'^eios:action:[A-Za-z0-9][A-Za-z0-9._-]{0,159}:[1-9][0-9]{0,8}$'),
 input_schema jsonb not null check(jsonb_typeof(input_schema)='object'),candidate_digest text not null check(candidate_digest~'^[0-9a-f]{64}$'),
 revision integer not null check(revision>0),set_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,world,resource_id)
);
create table runtime.nexloop_action_input_shadow (
 tenant_id text not null references control.nexloop_tenants(tenant_id),world text not null,resource_id text not null,
 candidate_digest text not null check(candidate_digest~'^[0-9a-f]{64}$'),day date not null,
 calls bigint not null default 0 check(calls>=0),would_reject bigint not null default 0 check(would_reject>=0 and would_reject<=calls),
 primary key(tenant_id,world,resource_id,candidate_digest,day)
);
do $$ declare t text; begin
 foreach t in array array['control.nexloop_action_input_candidates','runtime.nexloop_action_input_shadow'] loop
  execute format('alter table %s owner to nexloop_owner',t);
  execute format('alter table %s enable row level security',t);
  execute format('alter table %s force row level security',t);
  execute format('create policy tenant_rows on %s to nexloop_owner using(tenant_id=current_setting(''eios.tenant_id'',true)) with check(tenant_id=current_setting(''eios.tenant_id'',true))',t);
  execute format('revoke all on %s from public',t);
 end loop;
end $$;

create function authz.nexloop_action_input_tenant(p_digest text,p_world text) returns text
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v text;
begin
 if coalesce(p_digest,'')!~'^[a-f0-9]{64}$' then return null;end if;
 begin
  v:=authz.nexloop_service_identity(p_digest,p_world)->'binding'->>'tenant_id';
 exception when others then v:=null;
 end;
 if v is null then
  select r.tenant_id into v from authz.nexloop_browser_token_realms r where r.token_digest=p_digest;
 end if;
 return v;
end $$;
alter function authz.nexloop_action_input_tenant(text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_action_input_tenant(text,text) from public;

create function authz.nexloop_action_input_candidate(p_digest text,p_world text,p_resource text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);v_tenant text:=authz.nexloop_action_input_tenant(p_digest,p_world);v jsonb;
begin
 if v_tenant is null then return null;end if;
 perform set_config('eios.tenant_id',v_tenant,true);
 select jsonb_build_object('input_schema',c.input_schema,'candidate_digest',c.candidate_digest) into v from control.nexloop_action_input_candidates c
  where c.tenant_id=v_tenant and c.world=p_world and c.resource_id=p_resource;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return v;
end $$;
alter function authz.nexloop_action_input_candidate(text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_action_input_candidate(text,text,text) from public;

create function authz.nexloop_action_input_shadow_record(p_digest text,p_world text,p_resource text,p_candidate text,p_rejected boolean) returns boolean
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);v_tenant text:=authz.nexloop_action_input_tenant(p_digest,p_world);
begin
 if v_tenant is null or p_rejected is null then return false;end if;
 perform set_config('eios.tenant_id',v_tenant,true);
 if not exists(select 1 from control.nexloop_action_input_candidates c where c.tenant_id=v_tenant and c.world=p_world and c.resource_id=p_resource
   and c.candidate_digest=p_candidate) then
  perform set_config('eios.tenant_id',coalesce(prior,''),true);
  return false;
 end if;
 insert into runtime.nexloop_action_input_shadow(tenant_id,world,resource_id,candidate_digest,day,calls,would_reject)
  values(v_tenant,p_world,p_resource,p_candidate,(clock_timestamp() at time zone 'UTC')::date,1,case when p_rejected then 1 else 0 end)
  on conflict(tenant_id,world,resource_id,candidate_digest,day) do update
   set calls=runtime.nexloop_action_input_shadow.calls+1,would_reject=runtime.nexloop_action_input_shadow.would_reject+excluded.would_reject;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return true;
end $$;
alter function authz.nexloop_action_input_shadow_record(text,text,text,text,boolean) owner to nexloop_owner;
revoke all on function authz.nexloop_action_input_shadow_record(text,text,text,text,boolean) from public;
grant execute on function authz.nexloop_action_input_candidate(text,text,text),authz.nexloop_action_input_shadow_record(text,text,text,text,boolean)
 to nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime;

-- Candidates: [{resource_id, input_schema}] for one tenant and world, replacing the set; every resource an active Action version.
create function control.nexloop_configure_input_candidates(p_tenant text,p_world text,p_candidates jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);item jsonb;v_revision integer;n integer:=0;
begin
 if session_user is distinct from 'nexloop_configurator' then raise exception 'input candidates require trusted configuration' using errcode='42501';end if;
 if p_tenant is null or p_world is null or jsonb_typeof(p_candidates) is distinct from 'array' or jsonb_array_length(p_candidates)>500 then
  raise exception 'input candidates invalid' using errcode='22023';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 select coalesce(max(revision),0)+1 into v_revision from control.nexloop_action_input_candidates where tenant_id=p_tenant and world=p_world;
 delete from control.nexloop_action_input_candidates where tenant_id=p_tenant and world=p_world;
 for item in select value from jsonb_array_elements(p_candidates) loop
  if jsonb_typeof(item) is distinct from 'object' or (select array_agg(k order by k) from jsonb_object_keys(item) k) is distinct from array['input_schema','resource_id']
   or jsonb_typeof(item->'input_schema') is distinct from 'object'
   or not exists(select 1 from control.nexloop_action_definitions d where d.tenant_id=p_tenant and d.world=p_world and d.resource_id=item->>'resource_id' and d.active) then
   raise exception 'input candidates invalid' using errcode='22023';end if;
  insert into control.nexloop_action_input_candidates(tenant_id,world,resource_id,input_schema,candidate_digest,revision)
   values(p_tenant,p_world,item->>'resource_id',item->'input_schema',encode(sha256(convert_to((item->'input_schema')::text,'UTF8')),'hex'),v_revision);
  n:=n+1;
 end loop;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return jsonb_build_object('revision',v_revision,'candidates',n);
end $$;
alter function control.nexloop_configure_input_candidates(text,text,jsonb) owner to nexloop_owner;
revoke all on function control.nexloop_configure_input_candidates(text,text,jsonb) from public;
grant execute on function control.nexloop_configure_input_candidates(text,text,jsonb) to nexloop_configurator;

create function authz.nexloop_action_input_shadow_report(p_tenant text,p_world text,p_since timestamptz) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);v jsonb;
begin
 if session_user not in ('nexloop_metrics','nexloop_bootstrap') or p_tenant is null or p_world is null or p_since is null then
  raise exception 'input shadow report unavailable' using errcode='42501';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 select coalesce(jsonb_agg(jsonb_build_object('resource_id',c.resource_id,'candidate_digest',c.candidate_digest,'calls',coalesce(s.calls,0),
   'would_reject',coalesce(s.would_reject,0),
   'status',case when not coalesce(d.active,false) then 'stale' when coalesce(s.calls,0)=0 then 'no_data' when s.would_reject>0 then 'fail' else 'pass' end)
   order by c.resource_id),'[]'::jsonb) into v
  from control.nexloop_action_input_candidates c
  left join control.nexloop_action_definitions d on d.tenant_id=c.tenant_id and d.world=c.world and d.resource_id=c.resource_id
  left join lateral (select sum(x.calls)::bigint calls,sum(x.would_reject)::bigint would_reject from runtime.nexloop_action_input_shadow x
    where x.tenant_id=c.tenant_id and x.world=c.world and x.resource_id=c.resource_id and x.candidate_digest=c.candidate_digest
     and x.day>=(p_since at time zone 'UTC')::date) s on true
  where c.tenant_id=p_tenant and c.world=p_world;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return v;
end $$;
alter function authz.nexloop_action_input_shadow_report(text,text,timestamptz) owner to nexloop_owner;
revoke all on function authz.nexloop_action_input_shadow_report(text,text,timestamptz) from public;
grant execute on function authz.nexloop_action_input_shadow_report(text,text,timestamptz) to nexloop_metrics;
