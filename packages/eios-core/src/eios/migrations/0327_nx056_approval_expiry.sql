-- NX-056 A3 (docs/design/unified-approvals.md v5 §7, §8): the expiry sweep and the approval_waiting alert. Temporary number;
-- the dispatcher renumbers.
--
-- 1. control.nexloop_approval_expire_due: the tenant's requests past due_by that are still waiting, or approved and never consumed
--    (L6 A3 review important 2: an approved one past due_by can no longer be executed and must not linger), oldest first, bounded,
--    each ended through control.nexloop_approval_expire (0321: -> expired, a system decision, audit approval.expired). It only ends a
--    request; nothing is ever executed (the upstream ApprovalExpirySweeper's contract: idempotent, a request locked by a concurrent
--    decision or execution is skipped and seen by the next tick).
-- 2. authz.nexloop_retention_command (0129) is redefined with one more verb, 'approvals_expire', so the retention keeper's
--    existing signed port (domain worker, eios:action:nexloop.retention.execute:1) runs the sweep each tick. The other verbs
--    are unchanged.
-- 3. runtime.nexloop_metrics_snapshot is renamed once and wrapped: the wrapper adds the 'approvals' block (counts and ages only,
--    no body): waiting, oldest_waiting_age_seconds and approved_unconsumed count requests still before due_by; overdue counts those past
--    due_by still waiting or approved and unconsumed - the sweep's heartbeat (it should stay 0); and (A7, 0327) solo_mode_enabled,
--    self_approvals_24h and eligibility_reductions_24h for the single-person mode's alerts. Rules in alert-rules.v14.json.
-- Published migrations are not edited.

create function control.nexloop_approval_expire_due(p_tenant text,p_world text,p_limit integer) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare a uuid;n integer:=0;e integer:=0;
begin
 if p_limit is null or p_limit not between 1 and 1000 then raise exception 'approval expiry limit' using errcode='22023';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 for a in select approval_id from control.nexloop_approval_requests where tenant_id=p_tenant and world=p_world and status in ('waiting_approval','approved')
   and due_by<=clock_timestamp() and not exists(select 1 from control.nexloop_approval_consumptions c where c.tenant_id=p_tenant and c.world=p_world
    and c.approval_id=nexloop_approval_requests.approval_id)
   order by due_by,approval_id limit p_limit for update skip locked loop
  n:=n+1;
  if control.nexloop_approval_expire(p_tenant,p_world,a) then e:=e+1;end if;
 end loop;
 return jsonb_build_object('scanned',n,'expired',e);
end $$;
alter function control.nexloop_approval_expire_due(text,text,integer) owner to nexloop_owner;
revoke all on function control.nexloop_approval_expire_due(text,text,integer) from public;

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
   ||control.nexloop_approval_redact_due(t,p_world,(s->'sweep'->>'batch')::integer);  -- A5 (0326): frozen bodies past retention
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
alter function authz.nexloop_retention_command(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_retention_command(text,text,text,text,text) from public;
grant execute on function authz.nexloop_retention_command(text,text,text,text,text) to nexloop_domain_worker;

create function runtime.nexloop_approval_metrics(p_tenant text,p_world text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);v jsonb;
begin
 perform set_config('eios.tenant_id',coalesce(p_tenant,''),true);
 select jsonb_build_object('waiting',count(*) filter(where r.status='waiting_approval' and r.due_by>clock_timestamp()),
   'oldest_waiting_age_seconds',coalesce(floor(extract(epoch from clock_timestamp()-min(r.requested_at) filter(where r.status='waiting_approval'
     and r.due_by>clock_timestamp())))::bigint,0),
   'approved_unconsumed',count(*) filter(where r.status='approved' and r.due_by>clock_timestamp() and not exists(select 1 from control.nexloop_approval_consumptions k
     where k.approval_id=r.approval_id)),
   'overdue',count(*) filter(where r.status in ('waiting_approval','approved') and r.due_by<=clock_timestamp() and not exists(
     select 1 from control.nexloop_approval_consumptions k where k.approval_id=r.approval_id)))
  into v from control.nexloop_approval_requests r where r.tenant_id=p_tenant and r.world=p_world;
 -- A7 (design §15, alert approval_solo_mode): the switch, self-approvals and reduced eligible counts of the last 24 hours.
 v:=v||jsonb_build_object('solo_mode_enabled',case when control.nexloop_approval_solo_enabled(p_tenant,p_world) then 1 else 0 end,
  'self_approvals_24h',(select count(*) from control.nexloop_approval_decisions d where d.tenant_id=p_tenant and d.world=p_world and d.self_approved
    and d.decided_at>clock_timestamp()-interval '24 hours'),
  'eligibility_reductions_24h',(select count(*) from runtime.audit_events a where a.tenant_id=p_tenant and a.action='approval.eligibility_reduced'
    and a.created_at>clock_timestamp()-interval '24 hours'));
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return v;
end $$;
alter function runtime.nexloop_approval_metrics(text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_approval_metrics(text,text) from public;

alter function runtime.nexloop_metrics_snapshot(text,text) rename to nexloop_metrics_snapshot_before_approvals;
revoke all on function runtime.nexloop_metrics_snapshot_before_approvals(text,text) from public;
create function runtime.nexloop_metrics_snapshot(p_tenant text,p_world text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
begin
 return runtime.nexloop_metrics_snapshot_before_approvals(p_tenant,p_world)||jsonb_build_object('approvals',runtime.nexloop_approval_metrics(p_tenant,p_world));
end $$;
alter function runtime.nexloop_metrics_snapshot(text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_metrics_snapshot(text,text) from public;
