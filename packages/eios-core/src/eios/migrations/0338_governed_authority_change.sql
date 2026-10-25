-- NX-058 slice C (dispatcher 2026-10-25, option 1 of the L2 report): a governed handler that changes authority facts (a workbench
-- member's grants) makes the epoch trigger (0004) raise the tenant's authority_revision inside the governed transaction, and the governed
-- entry's tail recheck of the requester then failed ('browser action authority stale': the browser directory hash includes the tenant
-- revision, 0045). The migration number is temporary; it lives only in the file name and the catalog.
--
-- * control.nexloop_authority_handler_mark(tenant): owner-held, granted to no role, called only by owner-held governed handlers before
--   they write authority facts. It locks the tenant row FOR UPDATE (so after it only this transaction can change that row) and records
--   (txid, tenant, revision before). One mark per transaction: a second call refuses (L6 slice C review I-3 - a transaction carries one
--   governed handler's authority change, never a second write riding on the first). No caller can write the mark table or call the
--   function; there is no session flag to set. Marks of finished transactions older than a day are deleted at the next mark.
--   Lock order as NX-056 (L6 slice C review I-2): the tenant's approval lock (control.nexloop_approval_tenant_lock, 0331) first, then the
--   tenant row. The workbench member lock (0333) becomes that same approval lock, so a member change through the NX-056 gate (approval
--   lock, then the handler) and one without approval take the locks in one order.
-- * authz.nexloop_effective_tenant_revision(tenant, current): the revision before, only when a mark of this very transaction exists for
--   the tenant and the revision has risen since it - i.e. the writes of this transaction after its mark (the row lock excludes everyone
--   else; no xmin test, so a handler run inside a savepoint - the NX-056 gate - is covered, review I-1); the current revision otherwise.
-- * authz.nexloop_browser_identity_snapshot (0045's body): the directory hash uses that effective revision - the one change (and pg_temp
--   last in its search_path, as every re-created definer). Everything
--   else in the hash and in 0045's rechecks stays current: the requester's session, subject, account (revision, session_epoch) and
--   membership. The member a change acts on is not the requester (the handlers refuse it), and its own session, account or membership
--   change invalidates its permits at once. Registered in tests/test_function_copy_guard.py.
create table control.nexloop_authority_handler_marks (
 txid bigint primary key,tenant_id text not null references control.nexloop_tenants(tenant_id),prior_revision bigint not null,
 created_at timestamptz not null default clock_timestamp()
);
alter table control.nexloop_authority_handler_marks owner to nexloop_owner;
alter table control.nexloop_authority_handler_marks enable row level security;
alter table control.nexloop_authority_handler_marks force row level security;
create policy tenant_boundary on control.nexloop_authority_handler_marks to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
revoke all on control.nexloop_authority_handler_marks from public;

create function control.nexloop_authority_handler_mark(p_tenant text) returns void
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v bigint;prior text:=current_setting('eios.tenant_id',true);
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 if exists(select 1 from control.nexloop_authority_handler_marks where txid=txid_current()) then
  raise exception 'governed authority change: one per transaction' using errcode='42501';end if;
 perform control.nexloop_approval_tenant_lock(p_tenant);
 select authority_revision into v from control.nexloop_tenants where tenant_id=p_tenant for update;
 if not found then raise exception 'governed authority change: tenant unavailable' using errcode='42501';end if;
 insert into control.nexloop_authority_handler_marks(txid,tenant_id,prior_revision) values(txid_current(),p_tenant,v);
 delete from control.nexloop_authority_handler_marks where tenant_id=p_tenant and txid<>txid_current() and created_at<clock_timestamp()-interval '1 day';
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
end $$;

create function authz.nexloop_effective_tenant_revision(p_tenant text,p_current bigint) returns bigint
 language plpgsql volatile security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare m control.nexloop_authority_handler_marks;prior text:=current_setting('eios.tenant_id',true);
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into m from control.nexloop_authority_handler_marks where txid=txid_current() and tenant_id=p_tenant;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 -- The mark holds the tenant row FOR UPDATE: every increment since it is this transaction's own write after the mark.
 if m.txid is not null and p_current>m.prior_revision then return m.prior_revision;end if;
 return p_current;
end $$;

create or replace function authz.nexloop_browser_identity_snapshot(p_digest text,p_world text) returns jsonb
language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on set timezone='UTC' as $$
declare s control.nexloop_browser_sessions;b jsonb;sub jsonb;acc jsonb;mem jsonb;apprev integer;
 locator authz.nexloop_browser_token_realms;config control.nexloop_browser_business_applications;app jsonb;realm control.nexloop_tenants;binding jsonb;deadline timestamptz;now_at timestamptz:=clock_timestamp();
begin
 if session_user<>'nexloop_api' or p_digest is null or p_digest !~ '^[a-f0-9]{64}$' or p_world is distinct from 'real' then
  raise exception 'browser business authority unavailable' using errcode='42501';end if;
 select lr.* into locator from authz.nexloop_browser_token_realms lr where lr.token_digest=p_digest;
 if not found then raise exception 'browser business authority unavailable' using errcode='42501';end if;
 perform set_config('eios.tenant_id',locator.tenant_id,true);
 select * into s from control.nexloop_browser_sessions bs where bs.tenant_id=locator.tenant_id and bs.session_id=locator.session_id and bs.session_token_digest=decode(p_digest,'hex');
 if not found then raise exception 'browser business authority unavailable' using errcode='42501';end if;
 b:=s.payload;
 select t.* into realm from control.nexloop_tenants t where t.tenant_id=s.tenant_id;
 if not found or realm.status<>'active' then raise exception 'browser business realm unavailable' using errcode='42501';end if;
 select ba.revision into apprev from control.nexloop_browser_applications ba where ba.tenant_id=s.tenant_id and ba.application_id=s.application_id and ba.active;
 if not found or apprev::text is distinct from b->>'application_revision' then raise exception 'browser business application stale' using errcode='42501';end if;
 select su.payload into sub from control.nexloop_browser_subjects su where su.subject_id=b->>'subject_id';
 if not found or sub->>'kind' is distinct from 'human' or sub->>'status' is distinct from 'active' or sub->'revision' is distinct from b->'subject_revision' or b->>'subject_kind' is distinct from 'human' then
  raise exception 'browser business subject stale' using errcode='42501';end if;
 select ac.payload into acc from control.nexloop_browser_accounts ac where ac.tenant_id=s.tenant_id and ac.local_account_id=b->>'credential_id' and ac.password_hash is not null;
 if not found or b->>'credential_kind' is distinct from 'local_account' or b->>'credential_tenant_id' is distinct from s.tenant_id
  or acc->>'status' is distinct from 'active' or acc->'subject_id' is distinct from b->'subject_id'
  or acc->'revision' is distinct from b->'credential_revision' or acc->'session_epoch' is distinct from b->'credential_session_epoch'
  or acc->'must_change_password' is distinct from 'false'::jsonb or b->'restricted' is distinct from 'false'::jsonb
  or (acc->>'locked_until')::timestamptz>now_at then raise exception 'browser business account stale' using errcode='42501';end if;
 select bm.payload into mem from control.nexloop_browser_memberships bm where bm.tenant_id=s.tenant_id and bm.principal_id=b->>'principal_id';
 if not found or mem->>'status' is distinct from 'active' or mem->'subject_id' is distinct from b->'subject_id'
  or mem->'revision' is distinct from b->'membership_revision' or mem->>'valid_from' is null
  or (mem->>'valid_from')::timestamptz>now_at or (mem->>'valid_until')::timestamptz<=now_at
  or b->'revoked_at' is distinct from 'null'::jsonb or b->>'idle_expires_at' is null or b->>'absolute_expires_at' is null then
  raise exception 'browser business membership stale' using errcode='42501';end if;
 deadline:=least((b->>'idle_expires_at')::timestamptz,(b->>'absolute_expires_at')::timestamptz);
 if deadline<=now_at then raise exception 'browser business session expired' using errcode='42501';end if;
 select bc.* into config from control.nexloop_browser_business_applications bc where bc.tenant_id=s.tenant_id and bc.application_id=s.application_id;
 if not found then raise exception 'browser business application unpublished' using errcode='42501';end if;
 select af.payload into app from authz.nexloop_authority_facts af where af.tenant_id=s.tenant_id and af.fact_kind='application' and af.entity_key=array[config.caller_application_id,config.application_version];
 if not found or app->>'application_status' is distinct from 'active' or app->>'application_id' is distinct from config.caller_application_id
  or app->>'version' is distinct from config.application_version then raise exception 'browser business application unpublished' using errcode='42501';end if;
 binding:=jsonb_build_object('tenant_id',s.tenant_id,'credential_tenant_id',b->>'credential_tenant_id','credential_id',b->>'credential_id',
  'subject_id',b->>'subject_id','subject_kind','human','subject_principal_id',b->>'principal_id','subject_revision',b->'subject_revision',
  'membership_revision',b->'membership_revision','credential_revision',b->'credential_revision','credential_epoch',b->'credential_session_epoch',
  'caller_application_id',config.caller_application_id,'caller_application_version',config.application_version,'caller_application_digest',app->>'version_digest',
  'requested_scopes',config.requested_scopes,'credential_kind','local_account','session_id',s.session_id,'session_revision',b->'revision');
 return jsonb_build_object('binding',binding,'identity_kind','browser','expires_at',deadline,'absolute_expires_at',b->'absolute_expires_at',
  'directory_hash',encode(sha256(convert_to(jsonb_build_object('session',b,'subject',sub,'account',acc,'membership',mem,'application_revision',apprev,
    'configuration',to_jsonb(config),'tenant_revision',authz.nexloop_effective_tenant_revision(realm.tenant_id,realm.authority_revision),'tenant_status',realm.status)::text,'UTF8')),'hex'),
  'browser_application_id',s.application_id,'agent_invocation',null,'run_context',null);
end $$;

alter function control.nexloop_authority_handler_mark(text) owner to nexloop_owner;
alter function authz.nexloop_effective_tenant_revision(text,bigint) owner to nexloop_owner;
revoke all on function control.nexloop_authority_handler_mark(text) from public;
revoke all on function authz.nexloop_effective_tenant_revision(text,bigint) from public;

-- I-2: the workbench member lock (0333) is the tenant's approval lock of NX-056 (0331): one advisory lock, one order.
create or replace function control.nexloop_workbench_member_lock(p_tenant text) returns void language sql
 set search_path=pg_catalog,pg_temp as $$ select control.nexloop_approval_tenant_lock(p_tenant) $$;
