-- NX-058 slice E2 (docs/design/nx058-member-erasure.md v2.5 §2.5; owner Q-A "A+90天", E6 = A): 90 days after a staff erasure, the
-- plaintext username and email copied into the member's failed-login events (0023's details.result_snapshot) become the account's erased-
-- pseudonym. The migration number is temporary; it lives only in the file name and the catalog.
--
-- * control.nexloop_workbench_member_login_clearances: one row per cleared subject (append-only, owner only, forced RLS; ids, time, count);
--   a subject with a row is done, so a second run changes nothing.
-- * control.nexloop_workbench_member_login_clear(tenant, limit): erased subjects whose erasure is 90 days old or more and that have no
--   clearance yet (skip locked, at most limit per call): result_snapshot.username and result_snapshot.verified_email of their login events
--   become the pseudonym; every other key stays (account_id, account_revision, the rest of the snapshot).
-- * authz.nexloop_retention_command (0328): verb member_login_clear (the retention keeper's run_once step). Copy guard.
-- * Cost (design §2.5, erasure-coverage.md): 0023's failure receipt comparison no longer matches those events - only a replay of the same
--   request id 90 days or more later.
-- * After a restore: the erasure replay (0352) brings the erasure ledger back with its original time; a clearance missing from the restored
--   database is simply redone by the next run (no deletion ledger entry needed).
set local timezone = 'UTC';

create table control.nexloop_workbench_member_login_clearances (
 tenant_id text not null references control.nexloop_tenants(tenant_id) on delete restrict,subject_id text not null,
 events integer not null check(events>=0),cleared_at timestamptz not null default clock_timestamp(),primary key(tenant_id,subject_id)
);
alter table control.nexloop_workbench_member_login_clearances owner to nexloop_owner;
alter table control.nexloop_workbench_member_login_clearances enable row level security;
alter table control.nexloop_workbench_member_login_clearances force row level security;
create policy tenant_boundary on control.nexloop_workbench_member_login_clearances to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
revoke all on control.nexloop_workbench_member_login_clearances from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity,nexloop_configurator;
create trigger nx058_append_only before update or delete on control.nexloop_workbench_member_login_clearances for each row execute function control.nexloop_nx022_append_only();

create function control.nexloop_workbench_member_login_clear(p_tenant text,p_limit integer) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare e record;v_pseudonym text;n integer;n_subjects integer:=0;n_events integer:=0;
begin
 if p_limit is null or p_limit not between 1 and 10000 then raise exception 'member login clear invalid' using errcode='22023';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 for e in select x.subject_id from control.nexloop_workbench_member_erasures x where x.tenant_id=p_tenant and x.erased_at<=clock_timestamp()-interval '90 days'
   and not exists(select 1 from control.nexloop_workbench_member_login_clearances k where k.tenant_id=x.tenant_id and k.subject_id=x.subject_id)
   order by x.erased_at,x.subject_id limit p_limit for update skip locked loop
  select username into v_pseudonym from control.nexloop_browser_accounts where tenant_id=p_tenant and subject_id=e.subject_id and username like 'erased-%' limit 1;
  n:=0;
  if v_pseudonym is not null then
   update control.nexloop_browser_login_events set details=jsonb_set(details,'{result_snapshot}',(details->'result_snapshot')
     ||(case when details->'result_snapshot' ? 'username' then jsonb_build_object('username',v_pseudonym) else '{}'::jsonb end)
     ||(case when details->'result_snapshot' ? 'verified_email' then jsonb_build_object('verified_email',v_pseudonym||'@erased.invalid') else '{}'::jsonb end))
    where tenant_id=p_tenant and subject_id=e.subject_id and jsonb_typeof(details->'result_snapshot')='object'
     and ((details->'result_snapshot' ? 'username' and details->'result_snapshot'->>'username' is distinct from v_pseudonym)
      or (details->'result_snapshot' ? 'verified_email' and details->'result_snapshot'->>'verified_email' is distinct from v_pseudonym||'@erased.invalid'));
   get diagnostics n=row_count;
  end if;
  insert into control.nexloop_workbench_member_login_clearances(tenant_id,subject_id,events) values(p_tenant,e.subject_id,n) on conflict do nothing;
  n_subjects:=n_subjects+1;n_events:=n_events+n;
 end loop;
 if n_subjects>0 then
  insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,payload,created_at)
   values('workbench-login-clear:'||gen_random_uuid(),'nexloop.workbench_member.login_details_cleared',p_tenant,'system:retention','retention',
    'member-login-clear',jsonb_build_object('subjects',n_subjects,'events',n_events),clock_timestamp());
 end if;
 return jsonb_build_object('subjects',n_subjects,'events',n_events);
end $$;
alter function control.nexloop_workbench_member_login_clear(text,integer) owner to nexloop_owner;
revoke all on function control.nexloop_workbench_member_login_clear(text,integer) from public;

create or replace function authz.nexloop_retention_command(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare t text:=authz.nexloop_plan_port_tenant(p_digest,p_world,p_text,p_signature,p_payload,'nexloop-retention-v1',
  'eios:action:nexloop.retention.execute:1',array['nexloop_domain_worker']);c jsonb:=p_payload::jsonb;s jsonb:=runtime.nexloop_retention_settings_current();
 v jsonb;v_principal text:=(p_text::jsonb)->>'principal_id';v_interval integer:=(s->'sweep'->>'interval_seconds')::integer;
begin
 perform set_config('eios.tenant_id',t,true);
 if c->>'verb'='due' then
  select coalesce(jsonb_agg(k order by k),'[]'::jsonb) into v from unnest(runtime.nexloop_retention_classes()) k
   where not exists(select 1 from runtime.nexloop_retention_sweeps w where w.tenant_id=t and w.world=p_world and w.item_class=k
     and w.last_completed_at is not null and w.last_completed_at>=coalesce(w.last_started_at,w.last_completed_at)
     and w.last_completed_at>clock_timestamp()-make_interval(secs=>v_interval));
  return jsonb_build_object('due',v,'batch',(s->'sweep'->>'batch')::integer,'max_passes',(s->'sweep'->>'max_passes')::integer,'settings_version',(s->>'version')::integer);
 elsif c->>'verb'='sweep' then
  if not (c->>'item_class'=any(runtime.nexloop_retention_classes())) or coalesce(c->>'sweep_ref','')!~'^sweep:[0-9a-f-]{36}$' then
   raise exception 'retention sweep invalid' using errcode='22023';end if;
  insert into runtime.nexloop_retention_sweeps(tenant_id,world,item_class,last_started_at) values(t,p_world,c->>'item_class',clock_timestamp())
   on conflict(tenant_id,world,item_class) do update set last_started_at=clock_timestamp();
  v:=runtime.nexloop_retention_sweep(t,p_world,c->>'item_class',(s->'sweep'->>'batch')::integer,(c->>'sweep_ref')||':'||(c->>'item_class'),'service:'||v_principal);
  perform set_config('eios.tenant_id',t,true);
  if not (v->>'more')::boolean then
   update runtime.nexloop_retention_sweeps set last_completed_at=clock_timestamp(),last_counts=v->'counts'
    where tenant_id=t and world=p_world and item_class=c->>'item_class';
  end if;
  return v;
 elsif c->>'verb'='approvals_expire' then
  -- NX-056 A3: end waiting approvals past due_by (never executes one).
  return control.nexloop_approval_expire_due(t,p_world,(s->'sweep'->>'batch')::integer)
   ||control.nexloop_approval_redact_due(t,p_world,(s->'sweep'->>'batch')::integer);  -- A5 (0331): frozen bodies past retention
 elsif c->>'verb'='member_login_clear' then
  -- NX-058 slice E2 (design §2.5, E6 = A): the failed-login snapshots of members erased 90 days ago or more.
  return control.nexloop_workbench_member_login_clear(t,(s->'sweep'->>'batch')::integer);
 elsif c->>'verb'='status' then
  select coalesce(jsonb_agg(jsonb_build_object('item_class',k.item_class,'action',k.action,'tombstones',k.n) order by k.item_class,k.action),'[]'::jsonb) into v
   from (select item_class,action,count(*) n from runtime.nexloop_erasure_tombstones where tenant_id=t and world=p_world group by item_class,action) k;
  return jsonb_build_object('world',p_world,'settings_version',(s->>'version')::integer,'tombstones',v,
   'sweeps',(select coalesce(jsonb_agg(jsonb_build_object('item_class',w.item_class,'last_completed_at',runtime.nexloop_commercial_ts(w.last_completed_at),
     'last_counts',w.last_counts) order by w.item_class),'[]'::jsonb) from runtime.nexloop_retention_sweeps w where w.tenant_id=t and w.world=p_world),
   'warnings',runtime.nexloop_retention_warnings(t,p_world));
 end if;
 raise exception 'retention command invalid' using errcode='22023';
end $$;
