-- AUTH-DOS (L2 finding during AUTH-FP; L6 decision; adr/ADR-044-browser-session-epoch-binding.md): a browser session no longer binds
-- the account row's revision. control.nexloop_record_browser_account_failure (0023) raises account.revision on every failed login, and
-- control.nexloop_save_browser_account_password (0024: rehash, failure reset after a good login) on every save; the session checks
-- compared it with the session's credential_revision, so anyone who knew a username could end every session of that account with one
-- wrong password. account.revision stays the row's optimistic-concurrency version (0023's failure receipts and 0024's expected revision
-- reconcile on it); what ends sessions is session_epoch, which every write that must end them raises:
-- control.nexloop_browser_password_write (every password reset: 0198 operator reset since 0334, owner-issued codes 0333/0348),
-- control.nexloop_workbench_kick (reset issue, member changes 0340/0346/0348) and control.nexloop_workbench_member_erase (0352, also
-- disables the account). Changed, nothing else:
-- * control.nexloop_read_browser_session (0031) and authz.nexloop_browser_identity_snapshot (0403): the account revision comparison is
--   gone; session_epoch, status, subject, must_change_password/restricted, membership and deadline checks stay; the read gains
--   pg_temp last in its definer search_path;
-- * the same two: a lockout (locked_until > now) no longer refuses an established session (owner 2026-11-02, ADR-044 §4); it stops
--   new password logins only (eios.identity.local_accounts and control.nexloop_browser_evidence, unchanged). To end sessions, reset
--   or change the password (session_epoch + 1);
-- * authz.nexloop_browser_fingerprint_account_keys (0403): without revision, lockout_level and locked_until, so a failed login or a
--   lockout does not make a held claim stale.
-- The session's own credential_revision (recorded at login) is unchanged and still hashed with the session.
-- Effect at deployment: live browser claims signed with the old account hash are refused once.
-- Temporary number; the dispatcher renumbers.
create or replace function control.nexloop_read_browser_session(p_tenant text,p_application text,p_operator text,p_id text,p_digest bytea)
 returns jsonb language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare entry control.nexloop_browser_sessions;body jsonb;subject_body jsonb;account_body jsonb;member jsonb;app_revision integer;now_at timestamptz;
begin
 if session_user<>'nexloop_identity' or p_operator is distinct from session_user
  or (p_id is null and p_digest is null) or (p_id is not null and p_digest is not null)
  or (p_digest is not null and octet_length(p_digest)<>32) or (p_id is not null and length(p_id) not between 1 and 320) then
  raise exception 'identity unavailable' using errcode='42501';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into entry from control.nexloop_browser_sessions s where s.tenant_id=p_tenant and s.application_id=p_application
  and ((p_id is not null and s.session_id=p_id) or (p_digest is not null and s.session_token_digest=p_digest));
 if not found then return null;end if;
 body:=entry.payload;now_at:=clock_timestamp();
 if body->'revoked_at' is distinct from 'null'::jsonb or (body->>'idle_expires_at')::timestamptz<=now_at or (body->>'absolute_expires_at')::timestamptz<=now_at then return null;end if;
 select a.revision into app_revision from control.nexloop_browser_applications a where a.tenant_id=p_tenant and a.application_id=p_application and a.active;
 if not found or app_revision::text is distinct from body->>'application_revision' then return null;end if;
 select s.payload into subject_body from control.nexloop_browser_subjects s where s.subject_id=body->>'subject_id';
 if not found or subject_body->>'kind' is distinct from 'human' or subject_body->>'status' is distinct from 'active'
  or subject_body->'revision' is distinct from body->'subject_revision' then return null;end if;
 perform set_config('eios.tenant_id',body->>'credential_tenant_id',true);
 select a.payload into account_body from control.nexloop_browser_accounts a where a.tenant_id=body->>'credential_tenant_id' and a.local_account_id=body->>'credential_id' and a.password_hash is not null;
 if not found or body->>'credential_kind' is distinct from 'local_account'
  or account_body->>'status' is distinct from 'active' or account_body->'subject_id' is distinct from body->'subject_id'
  or account_body->'session_epoch' is distinct from body->'credential_session_epoch'
  or account_body->'must_change_password' is distinct from body->'restricted' then return null;end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 select m.payload into member from control.nexloop_browser_memberships m where m.tenant_id=p_tenant and m.principal_id=body->>'principal_id';
 if not found or member->>'status' is distinct from 'active' or member->'subject_id' is distinct from body->'subject_id'
  or member->'revision' is distinct from body->'membership_revision' or member->>'valid_from' is null
  or (member->>'valid_from')::timestamptz>now_at or (member->>'valid_until')::timestamptz<=now_at then return null;end if;
 return body||jsonb_build_object('session_token_digest',encode(entry.session_token_digest,'hex'),'csrf_token_digest',encode(entry.csrf_token_digest,'hex'));
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
  or acc->'session_epoch' is distinct from b->'credential_session_epoch'
  or acc->'must_change_password' is distinct from 'false'::jsonb or b->'restricted' is distinct from 'false'::jsonb
  then raise exception 'browser business account stale' using errcode='42501';end if;
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
  'directory_hash',encode(sha256(convert_to(jsonb_build_object('session',(select jsonb_object_agg(e.key,e.value) from jsonb_each(b) e where e.key=any(authz.nexloop_browser_fingerprint_session_keys())),'subject',sub,'account',(select jsonb_object_agg(e.key,e.value) from jsonb_each(acc) e where e.key=any(authz.nexloop_browser_fingerprint_account_keys())),'membership',mem,'application_revision',apprev,
    'configuration',to_jsonb(config),'tenant_revision',authz.nexloop_effective_tenant_revision(realm.tenant_id,realm.authority_revision),'tenant_status',realm.status)::text,'UTF8')),'hex'),
  'browser_application_id',s.application_id,'agent_invocation',null,'run_context',null);
end $$;

create or replace function authz.nexloop_browser_fingerprint_account_keys() returns text[]
 language sql immutable set search_path=pg_catalog,pg_temp as $$
 select array['local_account_id','tenant_id','subject_id','status','session_epoch','must_change_password']::text[] $$;
