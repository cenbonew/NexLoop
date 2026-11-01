-- S81 / ADR-043 (dispatcher 2026-11-01; temporary number 0392): entry tickets - the owner opens the WebChat as the pinned synthetic
-- test customer, or the reference endpoint staff page as the read-only test staff, from the workbench 入口 page, without a password.
--   1. control.nexloop_entry_tickets (one row per ticket: sha256 of the ticket, never the ticket; the pinned target principal or staff
--      label; the issuing member; the client digest; times; used_at and outcome) and control.nexloop_entry_ticket_events (issue and
--      redemption, success or refusal code). Control schema, row security, no table privilege: written only by the functions here.
--   2. control.nexloop_entry_target_refusal(tenant, principal): null when the principal is a synthetic test customer, else a code -
--      it owns a Consumer in world real (0046, one per principal), whose persona is simulated_agent (ADR-034, 0220), not erasing
--      (0383), with an active browser membership. Checked at issue and again at redemption (ADR-043 condition 2); the pinned principal
--      itself is the caller's deployment configuration (condition 1).
--   3. control.nexloop_entry_ticket_issue (nexloop_identity): re-reads the workbench session by its token digest (the workbench
--      application, not revoked, not expired) and the member's current role (owner only, control.nexloop_workbench_role), checks the
--      target, records the ticket (TTL 1..60 s) and the event. A refusal is recorded and returned, never raised, so it stays audited.
--   4. control.nexloop_entry_ticket_take / control.nexloop_entry_ticket_session (nexloop_identity, WebChat): take marks the ticket used
--      atomically (one update: unused, unexpired, target webchat) and returns the principal's identity facts; session creates the
--      browser session (authentication_methods [entry_ticket]) in a second transaction after rechecking those facts and the target,
--      within 60 s of the take, once. The reference endpoint redeems its own HMAC tickets (ADR-043); only the issue is recorded here.
--   5. Erasure: deleting a Consumer's owner row (the NX-029 erasure, 0306/0388) and recording a member erasure (0352) pseudonymize the
--      principal in both tables, clear the client digest and end an unused ticket (outcome erased). Triggers, tested in
--      test_entry_tickets_pg (T14); the member erasure coverage lists both tables with that reason.
--   6. Retention: authz.nexloop_retention_command (0353) gains the verb entry_tickets_clear: rows older than 30 days (copy guard).

-- 1. -------------------------------------------------------------------------------------------------------------------------------
create table control.nexloop_entry_tickets (
 ticket_digest text primary key check(ticket_digest~'^[a-f0-9]{64}$'),
 tenant_id text not null references control.nexloop_tenants(tenant_id) on delete restrict,
 target text not null check(target in ('webchat','reference_staff')),
 target_principal text not null check(length(target_principal) between 1 and 320),
 issued_by_principal text not null check(length(issued_by_principal) between 1 and 320),
 client_digest text check(client_digest~'^[a-f0-9]{64}$'),
 issued_at timestamptz not null default clock_timestamp(),expires_at timestamptz not null,
 used_at timestamptz,outcome text check(outcome in ('session','erased')),
 check(expires_at>issued_at and expires_at<=issued_at+interval '60 seconds')
);
create index nexloop_entry_tickets_issued on control.nexloop_entry_tickets(tenant_id,issued_at);
create table control.nexloop_entry_ticket_events (
 event_id bigint generated always as identity primary key,
 tenant_id text not null references control.nexloop_tenants(tenant_id) on delete restrict,
 kind text not null check(kind in ('issue','redeem')),target text not null check(target in ('webchat','reference_staff')),
 target_principal text,issued_by_principal text,client_digest text check(client_digest~'^[a-f0-9]{64}$'),
 outcome text not null check(outcome~'^[a-z_]{1,40}$'),at timestamptz not null default clock_timestamp()
);
create index nexloop_entry_ticket_events_at on control.nexloop_entry_ticket_events(tenant_id,at);
alter table control.nexloop_entry_tickets owner to nexloop_owner;
alter table control.nexloop_entry_ticket_events owner to nexloop_owner;
alter table control.nexloop_entry_tickets enable row level security;
alter table control.nexloop_entry_tickets force row level security;
alter table control.nexloop_entry_ticket_events enable row level security;
alter table control.nexloop_entry_ticket_events force row level security;
create policy tenant_boundary on control.nexloop_entry_tickets to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
create policy tenant_boundary on control.nexloop_entry_ticket_events to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
revoke all on control.nexloop_entry_tickets,control.nexloop_entry_ticket_events
 from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity,nexloop_configurator;

create function control.nexloop_entry_ticket_event(p_tenant text,p_kind text,p_target text,p_principal text,p_issuer text,p_client text,p_outcome text) returns void
 language sql security definer set search_path=pg_catalog,pg_temp as $$
 insert into control.nexloop_entry_ticket_events(tenant_id,kind,target,target_principal,issued_by_principal,client_digest,outcome)
  values(p_tenant,p_kind,p_target,p_principal,p_issuer,p_client,p_outcome) $$;
alter function control.nexloop_entry_ticket_event(text,text,text,text,text,text,text) owner to nexloop_owner;
revoke all on function control.nexloop_entry_ticket_event(text,text,text,text,text,text,text) from public;

-- 2. -------------------------------------------------------------------------------------------------------------------------------
create function control.nexloop_entry_target_refusal(p_tenant text,p_principal text) returns text
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_consumer text;m jsonb;now_at timestamptz:=clock_timestamp();
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 select consumer_id into v_consumer from control.nexloop_consumer_owners where tenant_id=p_tenant and world='real' and principal_id=p_principal;
 if v_consumer is null then return 'not_synthetic';end if;
 if runtime.nexloop_consumer_persona(p_tenant,'real',v_consumer) is distinct from 'simulated_agent' then return 'not_synthetic';end if;
 if runtime.nexloop_consumer_erasing(p_tenant,'real',v_consumer) then return 'not_synthetic';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 select payload into m from control.nexloop_browser_memberships where tenant_id=p_tenant and principal_id=p_principal;
 if m is null or m->>'status' is distinct from 'active' or (m->>'valid_from')::timestamptz>now_at or (m->>'valid_until')::timestamptz<=now_at then
  return 'not_synthetic';end if;
 return null;
end $$;
alter function control.nexloop_entry_target_refusal(text,text) owner to nexloop_owner;
revoke all on function control.nexloop_entry_target_refusal(text,text) from public;

-- The principal's identity facts for a browser session of p_application (as 0365's resolve, by principal, local account).
create function control.nexloop_entry_principal_facts(p_tenant text,p_application text,p_principal text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare m jsonb;s jsonb;a control.nexloop_browser_accounts;app integer;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 select revision into app from control.nexloop_browser_applications where tenant_id=p_tenant and application_id=p_application and active;
 select payload into m from control.nexloop_browser_memberships where tenant_id=p_tenant and principal_id=p_principal;
 if app is null or m is null then return null;end if;
 select payload into s from control.nexloop_browser_subjects where subject_id=m->>'subject_id';
 select * into a from control.nexloop_browser_accounts where tenant_id=p_tenant and subject_id=m->>'subject_id';
 if s is null or a.local_account_id is null or s->>'kind' is distinct from 'human' or s->>'status' is distinct from 'active'
  or a.payload->>'status' is distinct from 'active' or (a.payload->>'locked_until')::timestamptz>clock_timestamp() then return null;end if;
 return jsonb_build_object('membership',m,'application_revision',app,'subject_revision',(s->>'revision')::integer,
  'credential_id',a.local_account_id,'credential_revision',(a.payload->>'revision')::integer,
  'credential_session_epoch',(a.payload->>'session_epoch')::integer,'restricted',(a.payload->>'must_change_password')::boolean);
end $$;
alter function control.nexloop_entry_principal_facts(text,text,text) owner to nexloop_owner;
revoke all on function control.nexloop_entry_principal_facts(text,text,text) from public;

-- 3. -------------------------------------------------------------------------------------------------------------------------------
create function control.nexloop_entry_ticket_issue(p_tenant text,p_application text,p_session_digest bytea,p_target text,p_pinned text,
  p_ticket_digest text,p_client_digest text,p_ttl integer) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on set timezone='UTC' as $$
declare r control.nexloop_browser_sessions;v_principal text;v_refusal text;v_until timestamptz;now_at timestamptz:=clock_timestamp();
begin
 if session_user<>'nexloop_identity' or p_session_digest is null or octet_length(p_session_digest)<>32
  or coalesce(p_ticket_digest,'')!~'^[a-f0-9]{64}$' or (p_client_digest is not null and p_client_digest!~'^[a-f0-9]{64}$')
  or p_target is null or p_target not in ('webchat','reference_staff') or p_ttl is null or p_ttl not between 1 and 60
  or coalesce(p_pinned,'')='' or length(p_pinned)>320 then
  raise exception 'entry ticket rejected' using errcode='22023';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 -- The workbench session, re-read here: the workbench application of the tenant, not revoked, not past either expiry.
 select * into r from control.nexloop_browser_sessions where session_token_digest=p_session_digest and tenant_id=p_tenant and application_id=p_application;
 if not found or p_application is distinct from (control.nexloop_workbench_current(p_tenant)).application_id
  or r.payload->'revoked_at' is distinct from 'null'::jsonb or (r.payload->>'idle_expires_at')::timestamptz<=now_at
  or (r.payload->>'absolute_expires_at')::timestamptz<=now_at then
  raise exception 'entry ticket unauthenticated' using errcode='42501';end if;
 v_principal:=r.payload->>'principal_id';
 if control.nexloop_workbench_role(p_tenant,v_principal) is distinct from 'owner' then v_refusal:='not_owner';
 elsif p_target='webchat' then v_refusal:=control.nexloop_entry_target_refusal(p_tenant,p_pinned);
 elsif p_pinned!~'^[a-z][a-z0-9-]{0,31}$' then v_refusal:='staff_label_invalid';
 end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 if v_refusal is not null then
  perform control.nexloop_entry_ticket_event(p_tenant,'issue',p_target,p_pinned,v_principal,p_client_digest,v_refusal);
  return jsonb_build_object('issued',false,'code',v_refusal);
 end if;
 v_until:=now_at+make_interval(secs=>p_ttl);
 insert into control.nexloop_entry_tickets(ticket_digest,tenant_id,target,target_principal,issued_by_principal,client_digest,issued_at,expires_at)
  values(p_ticket_digest,p_tenant,p_target,p_pinned,v_principal,p_client_digest,now_at,v_until);
 perform control.nexloop_entry_ticket_event(p_tenant,'issue',p_target,p_pinned,v_principal,p_client_digest,'issued');
 return jsonb_build_object('issued',true,'target',p_target,'expires_at',v_until);
end $$;
alter function control.nexloop_entry_ticket_issue(text,text,bytea,text,text,text,text,integer) owner to nexloop_owner;
revoke all on function control.nexloop_entry_ticket_issue(text,text,bytea,text,text,text,text,integer) from public;
grant execute on function control.nexloop_entry_ticket_issue(text,text,bytea,text,text,text,text,integer) to nexloop_identity;

-- 4. -------------------------------------------------------------------------------------------------------------------------------
create function control.nexloop_entry_ticket_take(p_tenant text,p_application text,p_ticket_digest text,p_client_digest text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on set timezone='UTC' as $$
declare t control.nexloop_entry_tickets;v_code text;v_facts jsonb;
begin
 if session_user<>'nexloop_identity' or coalesce(p_ticket_digest,'')!~'^[a-f0-9]{64}$'
  or (p_client_digest is not null and p_client_digest!~'^[a-f0-9]{64}$') then
  raise exception 'entry ticket rejected' using errcode='22023';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 update control.nexloop_entry_tickets set used_at=clock_timestamp()
  where ticket_digest=p_ticket_digest and tenant_id=p_tenant and target='webchat' and used_at is null and expires_at>clock_timestamp()
  returning * into t;
 if not found then
  select * into t from control.nexloop_entry_tickets where ticket_digest=p_ticket_digest and tenant_id=p_tenant;
  v_code:=case when not found then 'unknown' when t.target<>'webchat' then 'wrong_target' when t.used_at is not null then 'used' else 'expired' end;
  perform control.nexloop_entry_ticket_event(p_tenant,'redeem','webchat',t.target_principal,t.issued_by_principal,p_client_digest,v_code);
  return jsonb_build_object('taken',false,'code',v_code);
 end if;
 v_code:=control.nexloop_entry_target_refusal(p_tenant,t.target_principal);
 if v_code is null then
  v_facts:=control.nexloop_entry_principal_facts(p_tenant,p_application,t.target_principal);
  if v_facts is null then v_code:='identity_unavailable';end if;
 end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 if v_code is not null then
  perform control.nexloop_entry_ticket_event(p_tenant,'redeem','webchat',t.target_principal,t.issued_by_principal,p_client_digest,v_code);
  return jsonb_build_object('taken',false,'code',v_code);
 end if;
 return jsonb_build_object('taken',true,'facts',v_facts);
end $$;
alter function control.nexloop_entry_ticket_take(text,text,text,text) owner to nexloop_owner;
revoke all on function control.nexloop_entry_ticket_take(text,text,text,text) from public;
grant execute on function control.nexloop_entry_ticket_take(text,text,text,text) to nexloop_identity;

create function control.nexloop_entry_ticket_session(p_tenant text,p_application text,p_ticket_digest text,p_client_digest text,p_body jsonb,
  p_token bytea,p_csrf bytea,p_trace text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on set timezone='UTC' as $$
declare t control.nexloop_entry_tickets;facts jsonb;expected jsonb;field_name text;now_at timestamptz:=clock_timestamp();
begin
 if session_user<>'nexloop_identity' or jsonb_typeof(p_body) is distinct from 'object' or coalesce(p_ticket_digest,'')!~'^[a-f0-9]{64}$'
  or p_token is null or p_csrf is null or octet_length(p_token)<>32 or octet_length(p_csrf)<>32 or p_token=p_csrf
  or p_trace is null or length(p_trace) not between 1 and 320
  or p_body->>'tenant_id' is distinct from p_tenant or p_body->>'application_id' is distinct from p_application then
  raise exception 'identity unavailable' using errcode='42501';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 update control.nexloop_entry_tickets set outcome='session' where ticket_digest=p_ticket_digest and tenant_id=p_tenant and target='webchat'
  and used_at is not null and used_at>now_at-interval '60 seconds' and outcome is null returning * into t;
 if not found or control.nexloop_entry_target_refusal(p_tenant,t.target_principal) is not null then
  raise exception 'authentication rejected' using errcode='EID03';end if;
 facts:=control.nexloop_entry_principal_facts(p_tenant,p_application,t.target_principal);
 if facts is null then raise exception 'authentication rejected' using errcode='EID03';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 expected:=jsonb_build_object('subject_id',facts->'membership'->'subject_id','principal_id',facts->'membership'->'principal_id',
  'membership_revision',facts->'membership'->'revision','subject_kind','human','subject_revision',facts->'subject_revision',
  'credential_kind','local_account','credential_tenant_id',p_tenant,'credential_id',facts->'credential_id',
  'credential_revision',facts->'credential_revision','credential_session_epoch',facts->'credential_session_epoch',
  'provider_id',null,'provider_revision',null,'provider_configuration_fingerprint',null,
  'authentication_methods',jsonb_build_array('entry_ticket'),'restricted',facts->'restricted','application_revision',facts->'application_revision');
 foreach field_name in array array(select jsonb_object_keys(expected)) loop
  if p_body->field_name is distinct from expected->field_name then raise exception 'authentication rejected' using errcode='EID03';end if;
 end loop;
 if p_body->>'session_id' is null or length(p_body->>'session_id') not between 1 and 320 or p_body->'revision' is distinct from '1'::jsonb
  or p_body->'revoked_at' is distinct from 'null'::jsonb or p_body->>'created_at' is null
  or p_body->>'last_seen_at' is distinct from p_body->>'created_at' or p_body->>'idle_expires_at' is null or p_body->>'absolute_expires_at' is null
  or (p_body->>'created_at')::timestamptz>now_at or (p_body->>'created_at')::timestamptz<now_at-interval '1 minute'
  or (p_body->>'idle_expires_at')::timestamptz<>(p_body->>'created_at')::timestamptz+interval '7 days'
  or (p_body->>'absolute_expires_at')::timestamptz<>(p_body->>'created_at')::timestamptz+interval '30 days' then
  raise exception 'authentication rejected' using errcode='EID03';end if;
 insert into control.nexloop_browser_sessions values(p_body->>'session_id',p_token,p_csrf,p_tenant,p_application,p_body);
 insert into control.nexloop_browser_login_events(tenant_id,subject_id,event_type,outcome,operator_principal_id,request_id,trace_id,details,created_at)
  values(p_tenant,p_body->>'subject_id','entry_ticket_login','success','nexloop_identity','entry-ticket:'||(p_body->>'session_id'),p_trace,
   jsonb_build_object('principal_id',p_body->>'principal_id','issued_by_principal',t.issued_by_principal),now_at);
 perform control.nexloop_entry_ticket_event(p_tenant,'redeem','webchat',t.target_principal,t.issued_by_principal,p_client_digest,'session');
 return p_body;
end $$;
alter function control.nexloop_entry_ticket_session(text,text,text,text,jsonb,bytea,bytea,text) owner to nexloop_owner;
revoke all on function control.nexloop_entry_ticket_session(text,text,text,text,jsonb,bytea,bytea,text) from public;
grant execute on function control.nexloop_entry_ticket_session(text,text,text,text,jsonb,bytea,bytea,text) to nexloop_identity;

-- 5. -------------------------------------------------------------------------------------------------------------------------------
create function control.nexloop_entry_ticket_consumer_erase() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_pseudonym text:='pseudonym:'||encode(sha256(convert_to('entry-ticket:'||old.principal_id,'UTF8')),'hex');
begin
 perform set_config('eios.tenant_id',old.tenant_id,true);
 update control.nexloop_entry_tickets set target_principal=v_pseudonym,client_digest=null,used_at=coalesce(used_at,clock_timestamp()),
  outcome=coalesce(outcome,'erased') where tenant_id=old.tenant_id and target='webchat' and target_principal=old.principal_id;
 update control.nexloop_entry_ticket_events set target_principal=v_pseudonym,client_digest=null
  where tenant_id=old.tenant_id and target='webchat' and target_principal=old.principal_id;
 return old;
end $$;
alter function control.nexloop_entry_ticket_consumer_erase() owner to nexloop_owner;
revoke all on function control.nexloop_entry_ticket_consumer_erase() from public;
create trigger nexloop_entry_ticket_consumer_erase before delete on control.nexloop_consumer_owners
 for each row execute function control.nexloop_entry_ticket_consumer_erase();

create function control.nexloop_entry_ticket_member_erase() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare p text;v_pseudonym text;
begin
 perform set_config('eios.tenant_id',new.tenant_id,true);
 foreach p in array new.principals loop
  v_pseudonym:='pseudonym:'||encode(sha256(convert_to('entry-ticket:'||p,'UTF8')),'hex');
  update control.nexloop_entry_tickets set issued_by_principal=v_pseudonym,client_digest=null,used_at=coalesce(used_at,clock_timestamp()),
   outcome=coalesce(outcome,'erased') where tenant_id=new.tenant_id and issued_by_principal=p;
  update control.nexloop_entry_ticket_events set issued_by_principal=v_pseudonym,client_digest=null where tenant_id=new.tenant_id and issued_by_principal=p;
 end loop;
 return null;
end $$;
alter function control.nexloop_entry_ticket_member_erase() owner to nexloop_owner;
revoke all on function control.nexloop_entry_ticket_member_erase() from public;
create trigger nexloop_entry_ticket_member_erase after insert on control.nexloop_workbench_member_erasures
 for each row execute function control.nexloop_entry_ticket_member_erase();

-- 6. -------------------------------------------------------------------------------------------------------------------------------
create function control.nexloop_entry_tickets_clear(p_tenant text,p_limit integer) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare n_tickets integer;n_events integer;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 delete from control.nexloop_entry_tickets where ctid in (select ctid from control.nexloop_entry_tickets
  where tenant_id=p_tenant and issued_at<clock_timestamp()-interval '30 days' limit greatest(coalesce(p_limit,100),1));
 get diagnostics n_tickets=row_count;
 delete from control.nexloop_entry_ticket_events where ctid in (select ctid from control.nexloop_entry_ticket_events
  where tenant_id=p_tenant and at<clock_timestamp()-interval '30 days' limit greatest(coalesce(p_limit,100),1));
 get diagnostics n_events=row_count;
 return jsonb_build_object('tickets',n_tickets,'events',n_events);
end $$;
alter function control.nexloop_entry_tickets_clear(text,integer) owner to nexloop_owner;
revoke all on function control.nexloop_entry_tickets_clear(text,integer) from public;

-- As 0353's, with the verb entry_tickets_clear.
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
 elsif c->>'verb'='entry_tickets_clear' then
  -- S81 (0392, ADR-043): entry tickets and their events older than 30 days.
  return control.nexloop_entry_tickets_clear(t,(s->'sweep'->>'batch')::integer);
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
