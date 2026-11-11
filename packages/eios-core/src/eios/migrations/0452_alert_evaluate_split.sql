-- NX-068 defect A+B (docs/planning-drafts/alert-evaluate-split-20261111.md, L6 conditional PASS with D1-D5): the alert evaluation no
-- longer holds the tenant row's governed-authorization FOR SHARE through the maintenance and the metrics snapshot. Temporary number; the
-- dispatcher renumbers. The evaluator (nexloop_eios.observability.AlertEvaluator.run_once) runs three transactions:
-- T1 authz.nexloop_alert_maintain_unbound(): the global maintenance (0131/0215/0449), no credential, no tenant row lock.
-- T2 authz.nexloop_alert_snapshot_take(tenant, maintenance_failed): the metrics snapshot of an active tenant into
--    runtime.nexloop_alert_snapshots (only the foreign key's KEY SHARE on the tenant row, compatible with a publication's epoch bump);
--    an unconsumed snapshot younger than 120 s is reused (D2); it answers the snapshot id only, the table is unreadable for services.
-- T3 authz.nexloop_alert_evaluate(...) (same signature; the signed body names the snapshot id): authorization (FOR SHARE from here), the
--    snapshot checked (this tenant, unconsumed, <= 120 s), the rules over it with taken_at as the observation time, monotonic per
--    alert state (a state evaluated at or after taken_at is left alone), the snapshot consumed (a second use is refused, D4).
-- D1: every run's outcome is recorded (runtime.nexloop_alert_evaluation_runs); the snapshot gains alert_evaluation {maintenance_failed_1h,
-- skipped_1h, minutes_since_success} for rules v39 (alert_maintenance_failed, alert_evaluation_skipped, alert_evaluation_stalled).

-- 1. Tables ------------------------------------------------------------------------------------------------------------------------
create table runtime.nexloop_alert_snapshots (
 snapshot_id uuid primary key default gen_random_uuid(),
 tenant_id text not null references control.nexloop_tenants(tenant_id),world text not null check(world='real'),
 taken_at timestamptz not null default clock_timestamp(),maintenance_failed boolean not null default false,
 snapshot jsonb not null default '{}'::jsonb check(jsonb_typeof(snapshot)='object'),consumed_at timestamptz
);
create index nexloop_alert_snapshots_open on runtime.nexloop_alert_snapshots(tenant_id,world,taken_at desc) where consumed_at is null;
create index nexloop_alert_snapshots_taken on runtime.nexloop_alert_snapshots(tenant_id,world,taken_at);
alter table runtime.nexloop_alert_snapshots owner to nexloop_owner;
alter table runtime.nexloop_alert_snapshots enable row level security;
alter table runtime.nexloop_alert_snapshots force row level security;
create policy alert_snapshots_tenant on runtime.nexloop_alert_snapshots to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
revoke all on runtime.nexloop_alert_snapshots from public,nexloop_api,nexloop_domain_worker,nexloop_scheduler,nexloop_action_worker,nexloop_identity,nexloop_metrics;

create table runtime.nexloop_alert_evaluation_runs (
 run_id bigint generated always as identity primary key,
 tenant_id text not null references control.nexloop_tenants(tenant_id),world text not null check(world='real'),
 at timestamptz not null default clock_timestamp(),
 outcome text not null check(outcome in ('evaluated','snapshot_unknown','snapshot_stale','snapshot_consumed','snapshot_missing'))
);
create index nexloop_alert_evaluation_runs_at on runtime.nexloop_alert_evaluation_runs(tenant_id,world,at);
alter table runtime.nexloop_alert_evaluation_runs owner to nexloop_owner;
alter table runtime.nexloop_alert_evaluation_runs enable row level security;
alter table runtime.nexloop_alert_evaluation_runs force row level security;
create policy alert_evaluation_runs_tenant on runtime.nexloop_alert_evaluation_runs to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
revoke all on runtime.nexloop_alert_evaluation_runs from public,nexloop_api,nexloop_domain_worker,nexloop_scheduler,nexloop_action_worker,nexloop_identity,nexloop_metrics;

-- NX-041 outbound guard: counts, codes and times of the alert evaluation itself; no external effect.
insert into control.nexloop_real_effect_tables(table_name,guarded,reason) values
 ('runtime.nexloop_alert_snapshots',false,'alert evaluation T2 metrics snapshots (counts, codes and times only; D3) consumed by T3; no external effect'),
 ('runtime.nexloop_alert_evaluation_runs',false,'alert evaluation outcomes (evaluated or why skipped) for rules v39; no external effect');

-- 2. The snapshot gains alert_evaluation (D1) ----------------------------------------------------------------------------------------
alter function runtime.nexloop_metrics_snapshot(text,text) rename to nexloop_metrics_snapshot_before_alert_split;
revoke all on function runtime.nexloop_metrics_snapshot_before_alert_split(text,text) from public;
create function runtime.nexloop_metrics_snapshot(p_tenant text,p_world text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v jsonb:=runtime.nexloop_metrics_snapshot_before_alert_split(p_tenant,p_world);failed bigint;skipped bigint;since numeric;
 last_ok timestamptz;first_skip timestamptz;
begin
 if jsonb_typeof(v) is distinct from 'object' then return v;end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 select count(*) into failed from runtime.nexloop_alert_snapshots
  where tenant_id=p_tenant and world=p_world and maintenance_failed and taken_at>clock_timestamp()-interval '1 hour';
 select count(*) filter (where outcome<>'evaluated' and at>clock_timestamp()-interval '1 hour'),max(at) filter (where outcome='evaluated'),
        min(at) filter (where outcome<>'evaluated')
   into skipped,last_ok,first_skip from runtime.nexloop_alert_evaluation_runs where tenant_id=p_tenant and world=p_world;
 -- Minutes without a successful evaluation: since the last success; never evaluated but skipped, since the first skip; nothing yet, 0.
 since:=floor(extract(epoch from clock_timestamp()-coalesce(last_ok,first_skip,clock_timestamp()))/60);
 return v||jsonb_build_object('alert_evaluation',jsonb_build_object('maintenance_failed_1h',failed,'skipped_1h',skipped,'minutes_since_success',since));
end $$;
alter function runtime.nexloop_metrics_snapshot(text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_metrics_snapshot(text,text) from public;
do $$declare g text;
begin
 for g in select r.rolname from pg_proc p cross join aclexplode(p.proacl) a join pg_roles r on r.oid=a.grantee
   where p.oid='runtime.nexloop_metrics_snapshot_before_alert_split(text,text)'::regprocedure and a.privilege_type='EXECUTE'
    and a.grantee<>p.proowner group by r.rolname loop
  execute format('grant execute on function runtime.nexloop_metrics_snapshot(text,text) to %I',g);
  execute format('revoke execute on function runtime.nexloop_metrics_snapshot_before_alert_split(text,text) from %I',g);
 end loop;
end $$;

-- 3. T1 and T2 (no credential; the alert evaluator's database role only) ----------------------------------------------------------------
create function authz.nexloop_alert_maintain_unbound() returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
begin
 return runtime.nexloop_observability_maintain();
end $$;
alter function authz.nexloop_alert_maintain_unbound() owner to nexloop_owner;
revoke all on function authz.nexloop_alert_maintain_unbound() from public;
grant execute on function authz.nexloop_alert_maintain_unbound() to nexloop_domain_worker;

create function authz.nexloop_alert_snapshot_take(p_tenant text,p_maintenance_failed boolean) returns uuid
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare id uuid;prior text:=current_setting('eios.tenant_id',true);
begin
 if p_maintenance_failed is null or not exists(select 1 from control.nexloop_tenants where tenant_id=p_tenant and status='active') then
  raise exception 'alert snapshot: tenant unavailable' using errcode='42501';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 -- D2: one open snapshot per tenant at a time; a repeated call within its validity reuses it (and keeps a maintenance failure).
 select snapshot_id into id from runtime.nexloop_alert_snapshots where tenant_id=p_tenant and world='real' and consumed_at is null
  and taken_at>=clock_timestamp()-interval '120 seconds' order by taken_at desc limit 1 for update;
 if id is not null then
  if p_maintenance_failed then update runtime.nexloop_alert_snapshots set maintenance_failed=true where snapshot_id=id;end if;
 else
  insert into runtime.nexloop_alert_snapshots(tenant_id,world,maintenance_failed) values(p_tenant,'real',p_maintenance_failed) returning snapshot_id into id;
  -- D3: the row's tenant column scopes it; the snapshot's own tenant_id label (for the Prometheus export) is not stored.
  update runtime.nexloop_alert_snapshots set snapshot=runtime.nexloop_metrics_snapshot(p_tenant,'real')-'tenant_id' where snapshot_id=id;
 end if;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return id;
end $$;
alter function authz.nexloop_alert_snapshot_take(text,boolean) owner to nexloop_owner;
revoke all on function authz.nexloop_alert_snapshot_take(text,boolean) from public;
grant execute on function authz.nexloop_alert_snapshot_take(text,boolean) to nexloop_domain_worker;

-- 4. T3: the governed evaluation over the snapshot ---------------------------------------------------------------------------------
create or replace function authz.nexloop_alert_evaluate(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare t text:=authz.nexloop_plan_port_tenant(p_digest,p_world,p_text,p_signature,p_payload,'nexloop-alert-evaluate-v1','eios:action:nexloop.alert.evaluate:1',
  array['nexloop_domain_worker']);
 snap jsonb;rs control.nexloop_alert_rules;r jsonb;base jsonb;item record;v_value numeric;v_key text;st control.nexloop_alert_state;
 v_breach boolean;now_at timestamptz;seen text[]:='{}';v_fired integer:=0;v_resolved integer:=0;sid uuid;sn runtime.nexloop_alert_snapshots;
 v_skip text;v_kept integer:=0;
begin
 if p_world is distinct from 'real' then return jsonb_build_object('evaluated',false,'reason','alerts_real_world_only');end if;
 perform set_config('eios.tenant_id',t,true);
 -- An evaluator of the previous release signs no snapshot_id (rolling deploy or rollback): the old single transaction - maintenance and
 -- snapshot here, under this authorization - so its alerts are never skipped; only a present but malformed id is snapshot_missing.
 if not (p_payload::jsonb ? 'snapshot_id') then
  perform runtime.nexloop_observability_maintain();
  sid:=authz.nexloop_alert_snapshot_take(t,false);
  perform set_config('eios.tenant_id',t,true);
 else
  begin sid:=(p_payload::jsonb->>'snapshot_id')::uuid;exception when others then sid:=null;end;
 end if;
 if sid is null then v_skip:='snapshot_missing';
 else
  select * into sn from runtime.nexloop_alert_snapshots where snapshot_id=sid and tenant_id=t and world=p_world for update;
  if sn.snapshot_id is null then v_skip:='snapshot_unknown';
  elsif sn.consumed_at is not null then v_skip:='snapshot_consumed';
  elsif sn.taken_at<clock_timestamp()-interval '120 seconds' then v_skip:='snapshot_stale';
  end if;
 end if;
 if v_skip is not null then
  insert into runtime.nexloop_alert_evaluation_runs(tenant_id,world,outcome) values(t,p_world,v_skip);
  return jsonb_build_object('evaluated',false,'reason',v_skip);
 end if;
 update runtime.nexloop_alert_snapshots set consumed_at=clock_timestamp() where snapshot_id=sid;
 insert into runtime.nexloop_alert_evaluation_runs(tenant_id,world,outcome) values(t,p_world,'evaluated');
 delete from runtime.nexloop_alert_snapshots where tenant_id=t and world=p_world and taken_at<clock_timestamp()-interval '1 day';
 delete from runtime.nexloop_alert_evaluation_runs where tenant_id=t and world=p_world and at<clock_timestamp()-interval '1 day';
 snap:=sn.snapshot;now_at:=sn.taken_at;
 select * into rs from control.nexloop_alert_rules where tenant_id=t order by version desc limit 1;
 if rs.tenant_id is null then return jsonb_build_object('evaluated',false,'reason','no_rules');end if;
 for r in select value from jsonb_array_elements(rs.rules) loop
  -- metric: a dotted path; one '*' segment iterates the children of that object (the child key is the selector).
  base:=snap#>string_to_array(split_part(r->>'metric','.*',1),'.');
  for item in
   select e.key sel,e.value#>string_to_array(substr(r->>'metric',strpos(r->>'metric','.*.')+3),'.') val
    from jsonb_each(case when strpos(r->>'metric','.*.')>0 and jsonb_typeof(base)='object' then base else '{}'::jsonb end) e
    where jsonb_typeof(e.value#>string_to_array(substr(r->>'metric',strpos(r->>'metric','.*.')+3),'.'))='number'
   union all select null::text,base where strpos(r->>'metric','*')=0 and coalesce(r->>'selector','')='' and jsonb_typeof(base)='number'
   union all select e.key,e.value from jsonb_each(case when jsonb_typeof(base)='object' then base else '{}'::jsonb end) e
    where strpos(r->>'metric','*')=0 and r->>'selector'='*' and jsonb_typeof(e.value)='number'
   union all select r->>'selector',base->(r->>'selector') where strpos(r->>'metric','*')=0 and coalesce(r->>'selector','') not in ('','*') and jsonb_typeof(base)='object' and jsonb_typeof(base->(r->>'selector'))='number'
  loop
   v_value:=(item.val#>>'{}')::numeric;v_key:=r->>'rule_id'||coalesce(':'||item.sel,'');seen:=seen||v_key;
   v_breach:=case r->>'condition' when 'gt' then v_value>(r->>'threshold')::numeric else v_value>=(r->>'threshold')::numeric end;
   select * into st from control.nexloop_alert_state where tenant_id=t and world=p_world and dedupe_key=v_key for update;
   if not found then
    insert into control.nexloop_alert_state(tenant_id,world,dedupe_key,rule_id,severity,selector,last_evaluated_at,rules_version)
     values(t,p_world,v_key,r->>'rule_id',r->>'severity',item.sel,now_at,rs.version) returning * into st;
   elsif st.last_evaluated_at>=now_at then
    -- Monotonic: a state already evaluated over a snapshot taken at or after this one is left alone (no older value, no event).
    v_kept:=v_kept+1;continue;
   end if;
   if v_breach then
    st.breached_since:=coalesce(st.breached_since,now_at);
    if not st.firing and st.breached_since<=now_at-make_interval(secs=>(r->>'for_seconds')::integer) then
     st.firing:=true;st.first_fired_at:=coalesce(st.first_fired_at,now_at);st.fire_count:=st.fire_count+1;st.last_fired_at:=now_at;v_fired:=v_fired+1;
     insert into control.nexloop_alert_events(tenant_id,world,dedupe_key,rule_id,severity,kind,selector,value,threshold,rules_version)
      values(t,p_world,v_key,r->>'rule_id',r->>'severity','firing',item.sel,v_value,(r->>'threshold')::numeric,rs.version);
    elsif st.firing then st.last_fired_at:=now_at;end if;
   else
    if st.firing then
     v_resolved:=v_resolved+1;
     insert into control.nexloop_alert_events(tenant_id,world,dedupe_key,rule_id,severity,kind,selector,value,threshold,rules_version)
      values(t,p_world,v_key,r->>'rule_id',r->>'severity','resolved',item.sel,v_value,(r->>'threshold')::numeric,rs.version);
    end if;
    st.firing:=false;st.breached_since:=null;
   end if;
   update control.nexloop_alert_state set breached_since=st.breached_since,firing=st.firing,first_fired_at=st.first_fired_at,last_fired_at=st.last_fired_at,
    fire_count=st.fire_count,last_value=v_value,last_evaluated_at=now_at,rules_version=rs.version,severity=r->>'severity'
    where tenant_id=t and world=p_world and dedupe_key=v_key;
  end loop;
 end loop;
 -- A selector or metric that disappeared (for example a queue drained to no rows) resolves its alert (same monotonic rule).
 for st in select * from control.nexloop_alert_state where tenant_id=t and world=p_world and not (dedupe_key=any(seen)) and last_evaluated_at<now_at for update loop
  if st.firing then
   v_resolved:=v_resolved+1;
   insert into control.nexloop_alert_events(tenant_id,world,dedupe_key,rule_id,severity,kind,selector,value,threshold,rules_version)
    values(t,p_world,st.dedupe_key,st.rule_id,st.severity,'resolved',st.selector,null,null,rs.version);
  end if;
  update control.nexloop_alert_state set firing=false,breached_since=null,last_evaluated_at=now_at,rules_version=rs.version where tenant_id=t and world=p_world and dedupe_key=st.dedupe_key;
 end loop;
 return jsonb_build_object('evaluated',true,'rules_version',rs.version,'fired',v_fired,'resolved',v_resolved,'kept_newer',v_kept,
  'firing',(select count(*) from control.nexloop_alert_state where tenant_id=t and world=p_world and firing));
end $$;
