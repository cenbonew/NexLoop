-- NX-035 local part (temporary number 0181): restore fence, journal replay, external effect window, opening by scope and the
-- migration gate. Design docs/implementation/NX-035-design.md §6, §7.2 (D1–D8 settled); local-part rulings of the dispatcher
-- 2026-10-11 (Q2: no reconstructed intents in this slice — a channel with unverifiable external effects, with intents still
-- open at the restore time, or that cannot be queried by time stays paused through the NX-022 action_type scope; Q3: the
-- migration gate has no exemption).
--
-- 1. control.nexloop_restore_state: one row, owner only, written by the restore tool on the restored instance before any
--    application role may log in: restored → replayed → verified_readonly → open. No row means the database was never
--    restored. control.nexloop_restore_scopes (tenant, world) and control.nexloop_restore_channels (per action type: counts
--    of the effect-window reconciliation, numbers only).
-- 2. Fence: authz.nexloop_assert_dispatch_controls (0068; the check both the Run path and the effect path run at dispatch) is
--    renamed once and kept (…_before_restore_v0181) and wrapped. While a restore row exists that is not 'open', every
--    dispatch is refused with NXR01 restore_unverified (recorded as a dispatch refusal by the existing paths, NX-030 D8),
--    except in world 'real' of a tenant whose scope the owner has opened. Test and simulation worlds open with the whole
--    restore. Without a restore row the wrapper adds one read of an empty table.
-- 3. Replay (owner, in 'restored'): journal entries newer than the restored database — deletions through NX-029
--    runtime.nexloop_replay_deletions, contact restrictions and erased contact keys by their original keys, credential and
--    membership revocations; then every browser session and Run credential is revoked regardless of the journal.
-- 4. Effect window (owner): per tenant, world and action type — intents not terminal at the restore time (they are only queried
--    and reconciled through the existing query path, never resent), external references in the window from channels that can
--    be queried by time, how many match a terminal intent, how many are unknown to the database (unverifiable). A channel that
--    cannot be queried by time, has unverifiable references, or has intents open at the restore time is paused (NX-022
--    set_control on scope action_type) until the owner resumes it after manual handling.
-- 5. nexloop.restore.open_scope:1 (capability restore.open_scope, operation open_restore_scope): owner only, through the 0124
--    registry; opens one tenant/world scope once the instance is verified_readonly. When every scope is open the state is
--    'open' and the actual RPO (window end − restore target) and RTO (restore start → verified_readonly) are recorded.
--    A paused channel stays paused after its scope opens.
-- 6. authz.nexloop_migration_gate(): every reason a migration must not run now (no exemption, no switch).
-- Published migrations are not edited.

-- 1. State ----------------------------------------------------------------------------------------------------------------
create table control.nexloop_restore_state (
 singleton boolean primary key default true check(singleton),
 restore_ref text not null check(restore_ref~'^[A-Za-z0-9._:-]{1,64}$'),
 state text not null check(state in ('restored','replayed','verified_readonly','open')),
 restored_from jsonb not null check(jsonb_typeof(restored_from)='object'),old_timeline integer check(old_timeline>=1),
 window_start timestamptz not null,window_end timestamptz,journal_from bigint not null check(journal_from>=0),journal_to bigint,
 restore_started_at timestamptz not null,restored_at timestamptz not null default clock_timestamp(),
 replayed_at timestamptz,reconciled_at timestamptz,verified_at timestamptz,opened_at timestamptz,
 rto_seconds numeric,rpo_seconds numeric,
 check((state in ('replayed','verified_readonly','open'))=(replayed_at is not null)),
 check((state in ('verified_readonly','open'))=(verified_at is not null and reconciled_at is not null and window_end is not null)),
 check((state='open')=(opened_at is not null))
);
create table control.nexloop_restore_scopes (
 tenant_id text not null,world text not null,opened boolean not null default false,opened_by text,intent_id text,opened_at timestamptz,
 primary key(tenant_id,world),check(opened=(opened_at is not null) and opened=(opened_by is not null))
);
create table control.nexloop_restore_channels (
 tenant_id text not null,world text not null,action_type text not null check(action_type~'^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,254}$'),
 time_queryable boolean not null,open_at_restore integer not null check(open_at_restore>=0),external_in_window integer not null check(external_in_window>=0),
 consistent integer not null check(consistent>=0),unverifiable integer not null check(unverifiable>=0),paused boolean not null,
 recorded_at timestamptz not null default clock_timestamp(),primary key(tenant_id,world,action_type)
);
do $tables$
declare t text;
begin
 foreach t in array array['control.nexloop_restore_state','control.nexloop_restore_scopes','control.nexloop_restore_channels'] loop
  execute format('alter table %s owner to nexloop_owner',t);
  execute format('alter table %s enable row level security',t);
  execute format('alter table %s force row level security',t);
  execute format('create policy deployment_restore on %s to nexloop_owner using(true) with check(true)',t);
  execute format('revoke all on %s from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity,nexloop_configurator,nexloop_metrics,nexloop_backup_recorder,nexloop_backup',t);
 end loop;
end $tables$;
create trigger nx035_no_delete before delete on control.nexloop_restore_state for each row execute function control.nexloop_nx022_append_only();
create trigger nx035_no_delete before delete on control.nexloop_restore_scopes for each row execute function control.nexloop_nx022_append_only();
create trigger nx035_append_only before update or delete on control.nexloop_restore_channels for each row execute function control.nexloop_nx022_append_only();

-- 2. Fence ----------------------------------------------------------------------------------------------------------------
create function runtime.nexloop_restore_assert_open(p_tenant text,p_world text) returns void
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp as $$
declare s control.nexloop_restore_state;
begin
 select * into s from control.nexloop_restore_state;
 if not found or s.state='open' then return;end if;
 if s.state='verified_readonly' and exists(select 1 from control.nexloop_restore_scopes where tenant_id=p_tenant and world=p_world and opened) then return;end if;
 raise exception 'restored database not verified for dispatch (state %)',s.state using errcode='NXR01';
end $$;

alter function authz.nexloop_assert_dispatch_controls(text,text,jsonb) rename to nexloop_assert_dispatch_controls_before_restore_v0181;
revoke all on function authz.nexloop_assert_dispatch_controls_before_restore_v0181(text,text,jsonb) from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler;
create function authz.nexloop_assert_dispatch_controls(p_digest text,p_world text,p_snapshot jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare ident jsonb;
begin
 if exists(select 1 from control.nexloop_restore_state where state<>'open') then
  -- The caller's own tenant, from its verified service identity (the same check the kept body runs first).
  ident:=control.nexloop_nx022_identity(p_digest,p_world,true);
  perform runtime.nexloop_restore_assert_open(ident->'binding'->>'tenant_id',p_world);
 end if;
 return authz.nexloop_assert_dispatch_controls_before_restore_v0181(p_digest,p_world,p_snapshot);
end $$;

-- Readiness (design §6.1): not ready while restored or replayed; reads and inbound only while verified_readonly; normal once open.
create function runtime.nexloop_restore_readiness() returns jsonb
 language sql stable security definer set search_path=pg_catalog,pg_temp as $$
 select case when s.state is null or s.state='open' then jsonb_build_object('ready',true,'reads',true,'dispatch',true,'state',coalesce(s.state,'never_restored'))
  when s.state='verified_readonly' then jsonb_build_object('ready',true,'reads',true,'dispatch',false,'state',s.state)
  else jsonb_build_object('ready',false,'reads',false,'dispatch',false,'state',s.state) end
 from (select null) d left join control.nexloop_restore_state s on true
$$;
create function authz.nexloop_restore_readiness() returns jsonb
 language sql stable security definer set search_path=pg_catalog,pg_temp as $$ select runtime.nexloop_restore_readiness() $$;

-- 3. Restore tool steps (owner only, on the restored instance) ----------------------------------------------------------------
create function runtime.nexloop_restore_begin(p_ref text,p_from jsonb,p_window_start timestamptz,p_started timestamptz,p_old_timeline integer) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare s control.nexloop_restore_state;
begin
 if coalesce(p_ref,'')!~'^[A-Za-z0-9._:-]{1,64}$' or jsonb_typeof(p_from) is distinct from 'object' or p_window_start is null or p_started is null
  or exists(select 1 from jsonb_each(p_from) e where e.key not in ('backup_id','target_time','target_name','target_lsn') or jsonb_typeof(e.value)<>'string') then
  raise exception 'restore invalid' using errcode='22023';end if;
 select * into s from control.nexloop_restore_state;
 if found then
  if s.restore_ref=p_ref then return jsonb_build_object('state',s.state,'replay',true);end if;
  if s.state<>'open' then raise exception 'another restore is in progress' using errcode='42501';end if;
  -- A database restored before (and opened) is restored again: the new restore starts over.
  update control.nexloop_restore_state set restore_ref=p_ref,state='restored',restored_from=p_from,old_timeline=p_old_timeline,window_start=p_window_start,
   window_end=null,journal_from=(select coalesce(max(seq),0) from control.nexloop_restore_journal),journal_to=null,restore_started_at=p_started,
   restored_at=clock_timestamp(),replayed_at=null,reconciled_at=null,verified_at=null,opened_at=null,rto_seconds=null,rpo_seconds=null;
  update control.nexloop_restore_scopes set opened=false,opened_by=null,intent_id=null,opened_at=null;
 else
  insert into control.nexloop_restore_state(restore_ref,state,restored_from,old_timeline,window_start,journal_from,restore_started_at)
   values(p_ref,'restored',p_from,p_old_timeline,p_window_start,(select coalesce(max(seq),0) from control.nexloop_restore_journal),p_started);
 end if;
 return jsonb_build_object('state','restored','replay',false,'journal_from',(select journal_from from control.nexloop_restore_state));
end $$;

create function runtime.nexloop_restore_require(p_ref text,p_state text) returns control.nexloop_restore_state
 language plpgsql security definer set search_path=pg_catalog,pg_temp as $$
declare s control.nexloop_restore_state;
begin
 select * into s from control.nexloop_restore_state for update;
 if not found or s.restore_ref is distinct from p_ref or s.state is distinct from p_state then
  raise exception 'restore step out of order' using errcode='42501';end if;
 return s;
end $$;

-- Journal entries as authz.nexloop_restore_journal_since exports them: [{seq, kind, entry, recorded_at}], every seq above the
-- restored database's own journal. Idempotent; each entry is applied by its original key.
create function runtime.nexloop_restore_replay(p_ref text,p_entries jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on set timezone='UTC' as $$
declare s control.nexloop_restore_state;x jsonb;e jsonb;deletions jsonb:='[]'::jsonb;n_contact integer:=0;n_keys integer:=0;n_cred integer:=0;n_member integer:=0;
 r jsonb:='{}'::jsonb;v_max bigint;
begin
 s:=runtime.nexloop_restore_require(p_ref,'restored');
 if jsonb_typeof(p_entries) is distinct from 'array' then raise exception 'restore replay invalid' using errcode='22023';end if;
 for x in select value from jsonb_array_elements(p_entries) order by (value->>'seq')::bigint loop
  e:=x->'entry';
  if jsonb_typeof(e) is distinct from 'object' or coalesce(x->>'seq','')!~'^[1-9][0-9]{0,18}$' or (x->>'seq')::bigint<=s.journal_from then
   raise exception 'restore replay entry invalid' using errcode='22023';end if;
  v_max:=greatest(coalesce(v_max,0),(x->>'seq')::bigint);
  perform set_config('eios.tenant_id',e->>'tenant_id',true);
  if x->>'kind'='deletion' and e->>'subject'='deletion_ledger' then
   deletions:=deletions||jsonb_build_array(jsonb_build_object('seq',e->'seq','tenant_id',e->'tenant_id','world',e->'world','kind',e->'kind',
    'subject_ref',e->'subject_ref','request_ref',e->'request_ref','cutoff',e->'cutoff'));
  elsif x->>'kind'='contact_restriction' and e->>'subject'='contact_restriction' then
   -- Without the customer's words (the journal never carries them); the newer control revision wins.
   insert into control.nexloop_contact_restrictions(tenant_id,world,consumer_id,active,control_revision,message_id,conversation_id,rule_version,rule_id,
     certainty,matched_text,restricted_at,released_at,released_by,release_intent,release_reason)
    values(e->>'tenant_id',e->>'world',e->>'consumer_id',(e->>'active')::boolean,(e->>'control_revision')::bigint,e->>'message_id',e->>'conversation_id',
     (e->>'rule_version')::integer,e->>'rule_id',e->>'certainty','',(e->>'restricted_at')::timestamptz,(e->>'released_at')::timestamptz,e->>'released_by',
     e->>'release_intent',case when (e->>'active')::boolean then null else 'restored from journal '||p_ref end)
    on conflict(tenant_id,world,consumer_id) do update set active=excluded.active,control_revision=excluded.control_revision,released_at=excluded.released_at,
     released_by=excluded.released_by,release_intent=excluded.release_intent,release_reason=excluded.release_reason
    where control.nexloop_contact_restrictions.control_revision<excluded.control_revision;
   n_contact:=n_contact+1;
  elsif x->>'kind'='contact_restriction' and e->>'subject'='erased_contact_key' then
   insert into control.nexloop_erased_contact_keys(tenant_id,world,key_digest,request_id,created_at,revoked_at,revoked_by)
    values(e->>'tenant_id',e->>'world',e->>'key_digest',e->>'request_id',(e->>'created_at')::timestamptz,(e->>'revoked_at')::timestamptz,e->>'revoked_by')
    on conflict(tenant_id,world,key_digest) do update set revoked_at=excluded.revoked_at,revoked_by=excluded.revoked_by
    where control.nexloop_erased_contact_keys.revoked_at is null and excluded.revoked_at is not null;
   n_keys:=n_keys+1;
  elsif x->>'kind'='revocation' and e->>'subject'='service_credential' then
   update authz.nexloop_service_credentials set status='revoked' where credential_id=e->>'credential_id' and tenant_id=e->>'tenant_id' and status<>'revoked';
   n_cred:=n_cred+1;
  elsif x->>'kind'='revocation' and e->>'subject'='browser_membership' then
   update control.nexloop_browser_memberships set payload=payload||jsonb_build_object('status',e->>'status','revision',greatest((payload->>'revision')::integer+1,(e->>'revision')::integer))
    where tenant_id=e->>'tenant_id' and principal_id=e->>'principal_id' and (payload->>'revision')::integer<(e->>'revision')::integer;
   n_member:=n_member+1;
  else raise exception 'restore replay entry invalid' using errcode='22023';end if;
 end loop;
 if jsonb_array_length(deletions)>0 then
  perform runtime.nexloop_replay_begin(p_ref,s.journal_from);
  for i in 0..(jsonb_array_length(deletions)-1)/1000 loop
   r:=runtime.nexloop_replay_deletions(p_ref,(select jsonb_agg(value order by ord) from jsonb_array_elements(deletions) with ordinality d(value,ord)
    where ord>i*1000 and ord<=(i+1)*1000));
  end loop;
 end if;
 update control.nexloop_restore_state set journal_to=greatest(coalesce(journal_to,journal_from),coalesce(v_max,0));
 return jsonb_build_object('deletions',jsonb_array_length(deletions),'deletion_result',r,'contact_restrictions',n_contact,'erased_contact_keys',n_keys,
  'service_credentials',n_cred,'memberships',n_member);
end $$;

-- End of replay: every browser session and Run credential of the old timeline is revoked whatever the journal says (§6.2 step
-- 5); the NX-029 replay bracket closes; state → replayed.
create function runtime.nexloop_restore_replayed(p_ref text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare s control.nexloop_restore_state;n_sessions integer:=0;n_runs integer:=0;n integer;t text;
begin
 s:=runtime.nexloop_restore_require(p_ref,'restored');
 for t in select tenant_id from control.nexloop_tenants loop
  perform set_config('eios.tenant_id',t,true);
  delete from control.nexloop_browser_sessions where tenant_id=t;get diagnostics n=row_count;n_sessions:=n_sessions+n;
 end loop;
 update authz.nexloop_run_credentials set status='revoked' where status<>'revoked';get diagnostics n_runs=row_count;
 if exists(select 1 from control.nexloop_deletion_replays where restore_ref=p_ref and state='replaying') then perform runtime.nexloop_replay_finish(p_ref);end if;
 update control.nexloop_restore_state set state='replayed',replayed_at=clock_timestamp();
 return jsonb_build_object('state','replayed','browser_sessions_revoked',n_sessions,'run_credentials_revoked',n_runs);
end $$;

-- p_evidence: [{tenant_id, world, action_type, time_queryable, external_intent_ids: [uuid, …]}] — what each channel reports for
-- the window (time-queryable channels list the intents they saw executed in [window_start, window_end]). Every action type
-- that has intents but no evidence entry is treated as not queryable by time.
create function runtime.nexloop_restore_reconcile(p_ref text,p_window_end timestamptz,p_evidence jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare s control.nexloop_restore_state;c record;v_paused integer:=0;v_rows jsonb:='[]'::jsonb;v_tenant text;
begin
 s:=runtime.nexloop_restore_require(p_ref,'replayed');
 if p_window_end is null or p_window_end<s.window_start or jsonb_typeof(p_evidence) is distinct from 'array'
  or exists(select 1 from jsonb_array_elements(p_evidence) x where jsonb_typeof(x) is distinct from 'object'
   or exists(select 1 from jsonb_object_keys(x) k where k not in ('tenant_id','world','action_type','time_queryable','external_intent_ids'))
   or jsonb_typeof(x->'time_queryable') is distinct from 'boolean' or jsonb_typeof(x->'external_intent_ids') is distinct from 'array'
   or coalesce(x->>'action_type','')!~'^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,254}$'
   or exists(select 1 from jsonb_array_elements_text(x->'external_intent_ids') i where i!~'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')) then
  raise exception 'restore reconcile invalid' using errcode='22023';end if;
 for v_tenant in select tenant_id from control.nexloop_tenants union select x->>'tenant_id' from jsonb_array_elements(p_evidence) x loop
 perform set_config('eios.tenant_id',v_tenant,true);
 for c in
  with ev as (select x->>'tenant_id' tenant_id,x->>'world' world,x->>'action_type' action_type,(x->>'time_queryable')::boolean time_queryable,
     array(select i::uuid from jsonb_array_elements_text(x->'external_intent_ids') i) ids from jsonb_array_elements(p_evidence) x where x->>'tenant_id'=v_tenant),
   types as (select tenant_id,world,action_name action_type from runtime.nexloop_effect_intents where tenant_id=v_tenant union select tenant_id,world,action_type from ev)
  select t.tenant_id,t.world,t.action_type,coalesce(ev.time_queryable,false) time_queryable,
   (select count(*) from runtime.nexloop_effect_intents i where i.tenant_id=t.tenant_id and i.world=t.world and i.action_name=t.action_type
     and i.state in ('accepted','dispatching','unknown','observed_fulfilled'))::integer open_at_restore,
   coalesce(cardinality(ev.ids),0) external_in_window,
   (select count(*) from runtime.nexloop_effect_intents i where i.intent_id=any(coalesce(ev.ids,'{}')) and i.tenant_id=t.tenant_id and i.world=t.world
     and i.action_name=t.action_type and i.state in ('fulfilled','confirmed'))::integer consistent,
   (select count(*) from unnest(coalesce(ev.ids,'{}')) u(id) where not exists(select 1 from runtime.nexloop_effect_intents i where i.intent_id=u.id
     and i.tenant_id=t.tenant_id and i.world=t.world and i.action_name=t.action_type))::integer unverifiable
  from types t left join ev on ev.tenant_id=t.tenant_id and ev.world=t.world and ev.action_type=t.action_type
 loop
  if not c.time_queryable or c.unverifiable>0 or c.open_at_restore>0 then
   -- Kept blocked through the owner controls of NX-022; the owner resumes the channel after handling it.
   perform set_config('eios.tenant_id',c.tenant_id,true);
   perform control.nexloop_nx022_owner_change(c.tenant_id,c.world,'restore:'||p_ref,'restore:'||p_ref||':'||c.action_type,
    jsonb_build_object('operation','set_control','scope_kind','action_type','scope_ref',c.action_type,'paused',true,
     'reason','restore '||p_ref||': external effects in the restore window not verified'));
   v_paused:=v_paused+1;
  end if;
  insert into control.nexloop_restore_channels(tenant_id,world,action_type,time_queryable,open_at_restore,external_in_window,consistent,unverifiable,paused)
   values(c.tenant_id,c.world,c.action_type,c.time_queryable,c.open_at_restore,c.external_in_window,c.consistent,c.unverifiable,
    not c.time_queryable or c.unverifiable>0 or c.open_at_restore>0);
  v_rows:=v_rows||jsonb_build_array(jsonb_build_object('tenant_id',c.tenant_id,'world',c.world,'action_type',c.action_type,'time_queryable',c.time_queryable,
   'open_at_restore',c.open_at_restore,'external_in_window',c.external_in_window,'consistent',c.consistent,'unverifiable',c.unverifiable,
   'paused',not c.time_queryable or c.unverifiable>0 or c.open_at_restore>0));
 end loop;
 end loop;
 update control.nexloop_restore_state set window_end=p_window_end,reconciled_at=clock_timestamp();
 return jsonb_build_object('channels',v_rows,'paused',v_paused);
end $$;

create function runtime.nexloop_restore_verified(p_ref text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare s control.nexloop_restore_state;v_rto numeric;
begin
 s:=runtime.nexloop_restore_require(p_ref,'replayed');
 if s.reconciled_at is null then raise exception 'restore step out of order' using errcode='42501';end if;
 -- One scope per active tenant in world 'real' (the owner opens it from the workbench); test and simulation worlds stay
 -- closed until every real scope is open and the whole restore is open.
 insert into control.nexloop_restore_scopes(tenant_id,world)
  select t.tenant_id,'real' from control.nexloop_tenants t where t.status='active'
  on conflict do nothing;
 v_rto:=round(extract(epoch from clock_timestamp()-s.restore_started_at)::numeric,3);
 update control.nexloop_restore_state set state='verified_readonly',verified_at=clock_timestamp(),rto_seconds=v_rto;
 return jsonb_build_object('state','verified_readonly','rto_seconds',v_rto,'scopes',(select count(*) from control.nexloop_restore_scopes));
end $$;

-- 4. Opening by scope (governed human Action, owner only) ----------------------------------------------------------------------
create function control.nexloop_governed_restore_open_scope(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language plpgsql set search_path=pg_catalog,pg_temp as $$
declare s control.nexloop_restore_state;sc control.nexloop_restore_scopes;v_rpo numeric;v_open boolean:=false;
begin
 if body->>'operation' is distinct from 'open_restore_scope' or coalesce(body->>'restore_ref','')!~'^[A-Za-z0-9._:-]{1,64}$'
  then
  raise exception 'restore scope invalid' using errcode='22023';end if;
 if control.nexloop_workbench_role(p_tenant,p_principal) is distinct from 'owner' then raise exception 'restore scope is owner only' using errcode='42501';end if;
 select * into s from control.nexloop_restore_state for update;
 if not found or s.restore_ref is distinct from body->>'restore_ref' or s.state not in ('verified_readonly','open') then
  raise exception 'restore not verified' using errcode='42501';end if;
 select * into sc from control.nexloop_restore_scopes where tenant_id=p_tenant and world=p_world for update;
 if not found then raise exception 'restore scope unknown' using errcode='22023';end if;
 -- D8: a channel whose window could not be verified is not opened by this Action; it stays paused (NX-022) until resumed.
 if exists(select 1 from control.nexloop_restore_channels c where c.tenant_id=p_tenant and c.world=p_world and (c.unverifiable>0 or not c.time_queryable)
   and not exists(select 1 from control.nexloop_control_scopes cs where cs.tenant_id=p_tenant and cs.world=p_world and cs.scope_kind='action_type'
     and cs.scope_ref=c.action_type and cs.paused)
   and not exists(select 1 from control.nexloop_control_events ce where ce.tenant_id=p_tenant and ce.world=p_world and ce.scope_kind='action_type'
     and ce.scope_ref=c.action_type and ce.event_kind='resume' and ce.recorded_at>s.reconciled_at)) then
  raise exception 'unverified channel is not paused' using errcode='42501';end if;
 if not sc.opened then
  update control.nexloop_restore_scopes set opened=true,opened_by=p_principal,intent_id=p_intent,opened_at=clock_timestamp() where tenant_id=p_tenant and world=p_world;
 end if;
 if s.state='verified_readonly' and not exists(select 1 from control.nexloop_restore_scopes where not opened) then
  v_rpo:=round(extract(epoch from s.window_end-s.window_start)::numeric,3);
  update control.nexloop_restore_state set state='open',opened_at=clock_timestamp(),rpo_seconds=v_rpo;v_open:=true;
 end if;
 return jsonb_build_object('tenant_id',p_tenant,'world',p_world,'opened',true,'replay',sc.opened,'restore_open',v_open or s.state='open',
  'paused_channels',(select coalesce(jsonb_agg(c.action_type order by c.action_type),'[]'::jsonb) from control.nexloop_restore_channels c
   where c.tenant_id=p_tenant and c.world=p_world and c.paused));
end $$;
alter function control.nexloop_governed_restore_open_scope(text,text,text,text,text,jsonb) owner to nexloop_owner;
revoke all on function control.nexloop_governed_restore_open_scope(text,text,text,text,text,jsonb) from public;
insert into control.nexloop_governed_capabilities(capability,operation,subject_rule,handler,source_task) values
 ('restore.open_scope','open_restore_scope','human','control.nexloop_governed_restore_open_scope(text,text,text,text,text,jsonb)','NX-035');

-- 5. Migration gate (design §7.2; AT-056) ------------------------------------------------------------------------------------
-- Every reason a migration must not run now; the migrate tool runs nothing (and reads no migration file) unless the list is
-- empty. Fixed thresholds: the latest verified PG backup at most 60 minutes old, WAL archived within 5 minutes and no archive
-- failure since, restore journal exported within 5 minutes. No exemption exists.
create function authz.nexloop_migration_gate() returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare b runtime.nexloop_backup_manifests;a pg_stat_archiver;ident jsonb:=runtime.nexloop_cluster_identity();cat jsonb:=runtime.nexloop_catalog_state();
 reasons text[]:='{}';now_at timestamptz:=clock_timestamp();last_journal bigint;v_state text;
begin
 select * into b from runtime.nexloop_backup_manifests where kind in ('pg_full','pg_diff','pg_incr') and verify_status<>'pending'
  order by completed_at desc,manifest_id desc limit 1;
 if not found then reasons:=reasons||'no_backup_manifest'::text;
 else
  if b.verify_status<>'verified' then reasons:=reasons||'latest_backup_not_verified'::text;end if;
  if b.completed_at<now_at-interval '60 minutes' then reasons:=reasons||'backup_too_old'::text;end if;
  if b.system_identifier is distinct from ident->>'system_identifier' or b.timeline is distinct from (ident->>'timeline')::integer then
   reasons:=reasons||'backup_from_another_cluster'::text;end if;
  if b.catalog_digest is distinct from cat->>'digest' then reasons:=reasons||'migrated_after_backup'::text;end if;
 end if;
 select * into a from pg_stat_archiver;
 if current_setting('archive_mode')='off' then reasons:=reasons||'wal_archiving_off'::text;
 elsif a.last_archived_time is null or a.last_archived_time<now_at-interval '5 minutes' then reasons:=reasons||'wal_archive_lagging'::text;
 end if;
 if a.last_failed_time is not null and (a.last_archived_time is null or a.last_failed_time>=a.last_archived_time) then reasons:=reasons||'wal_archive_failing'::text;end if;
 select max(journal_seq) into last_journal from runtime.nexloop_backup_manifests where kind='journal' and verify_status='verified';
 if exists(select 1 from control.nexloop_restore_journal where seq>coalesce(last_journal,0) and recorded_at<now_at-interval '5 minutes') then
  reasons:=reasons||'restore_journal_export_lagging'::text;end if;
 select state into v_state from control.nexloop_restore_state;
 if v_state is not null and v_state<>'open' then reasons:=reasons||'restore_fence_active'::text;end if;
 return jsonb_build_object('allowed',cardinality(reasons)=0,'reasons',to_jsonb(reasons),'backup_id',b.backup_id,'catalog_revision',cat->>'revision');
end $$;

do $owners$
declare f text;
begin
 foreach f in array array['runtime.nexloop_restore_assert_open(text,text)','authz.nexloop_assert_dispatch_controls(text,text,jsonb)',
   'runtime.nexloop_restore_readiness()','authz.nexloop_restore_readiness()',
   'runtime.nexloop_restore_begin(text,jsonb,timestamptz,timestamptz,integer)','runtime.nexloop_restore_require(text,text)',
   'runtime.nexloop_restore_replay(text,jsonb)','runtime.nexloop_restore_replayed(text)','runtime.nexloop_restore_reconcile(text,timestamptz,jsonb)',
   'runtime.nexloop_restore_verified(text)','control.nexloop_governed_restore_open_scope(text,text,text,text,text,jsonb)','authz.nexloop_migration_gate()'] loop
  execute 'alter function '||f||' owner to nexloop_owner';execute 'revoke all on function '||f||' from public';
 end loop;
end $owners$;
grant execute on function authz.nexloop_assert_dispatch_controls(text,text,jsonb) to nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler;
grant execute on function authz.nexloop_restore_readiness() to nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_metrics;
