-- AUTH-FP (L6, medium): the browser directory hash (authz.nexloop_browser_identity_snapshot, last defined in 0341) hashed the whole
-- session row and the whole account row, so fields every request or login attempt writes - the session's last_seen_at and
-- idle_expires_at (touched by each CSRF refresh), the account's failed_attempts and updated_at - changed the fingerprint, and a
-- concurrent legitimate request of the same session made the other one's claim stale (SSE drops, sporadic 503 on reads; the noise
-- also hid real revocation). The fingerprint now covers only what decides authority:
-- * session: identity and binding (session, tenant, subject, principal, application and its revision, subject kind and revision,
--   credential kind/tenant/id/revision, credential_session_epoch, provider id/revision/configuration fingerprint), authentication
--   methods, restricted, membership_revision, absolute_expires_at, revoked_at and the session revision. Left out: created_at (fixed),
--   last_seen_at and idle_expires_at (activity; the idle deadline is still enforced on every call by this function's deadline check),
--   the CSRF digest (never in the payload);
-- * account: local_account_id, tenant_id, subject_id, status, revision, session_epoch, must_change_password, lockout_level and
--   locked_until. Left out: failed_attempts and updated_at (written by failed logins without changing authority), username and
--   verified_email (no authority), password_hash/password_history (a password change raises revision and session_epoch);
-- * subject, membership (its trusted attributes and revision), application revision, configuration, effective tenant revision and
--   tenant status: unchanged. Every check of the function is unchanged; only the hashed projection differs.
-- * authz.nexloop_browser_authority_fact_snapshot (0045's body): the record hash of the browser_authentication fact leaves out
--   expires_at, the effective deadline least(idle, absolute) that every touch moves; the payload still carries it (the Python decision
--   is bounded by it), absolute_expires_at, status and the session revision stay hashed, and the idle deadline is still refused on
--   every call by the identity snapshot. Every other kind and check unchanged; definer search_path gains pg_temp.
-- Effect at deployment: every live browser claim signed with the old hash is refused once (the hash changes); clients re-read.
-- Temporary number; the dispatcher renumbers.
create function authz.nexloop_browser_fingerprint_session_keys() returns text[]
 language sql immutable set search_path=pg_catalog,pg_temp as $$
 select array['session_id','tenant_id','subject_id','principal_id','application_id','application_revision','subject_kind','subject_revision',
  'credential_kind','credential_tenant_id','credential_id','credential_revision','credential_session_epoch','provider_id','provider_revision',
  'provider_configuration_fingerprint','authentication_methods','restricted','membership_revision','absolute_expires_at','revoked_at','revision']::text[] $$;
create function authz.nexloop_browser_fingerprint_account_keys() returns text[]
 language sql immutable set search_path=pg_catalog,pg_temp as $$
 select array['local_account_id','tenant_id','subject_id','status','revision','session_epoch','must_change_password','lockout_level','locked_until']::text[] $$;
alter function authz.nexloop_browser_fingerprint_session_keys() owner to nexloop_owner;
alter function authz.nexloop_browser_fingerprint_account_keys() owner to nexloop_owner;
revoke all on function authz.nexloop_browser_fingerprint_session_keys() from public;
revoke all on function authz.nexloop_browser_fingerprint_account_keys() from public;

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
  'directory_hash',encode(sha256(convert_to(jsonb_build_object('session',(select jsonb_object_agg(e.key,e.value) from jsonb_each(b) e where e.key=any(authz.nexloop_browser_fingerprint_session_keys())),'subject',sub,'account',(select jsonb_object_agg(e.key,e.value) from jsonb_each(acc) e where e.key=any(authz.nexloop_browser_fingerprint_account_keys())),'membership',mem,'application_revision',apprev,
    'configuration',to_jsonb(config),'tenant_revision',authz.nexloop_effective_tenant_revision(realm.tenant_id,realm.authority_revision),'tenant_status',realm.status)::text,'UTF8')),'hex'),
  'browser_application_id',s.application_id,'agent_invocation',null,'run_context',null);
end $$;

create or replace function authz.nexloop_browser_authority_fact_snapshot(p_digest text,p_world text,p_kind text,p_key text[]) returns jsonb
language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare identity jsonb;binding jsonb;value jsonb;body jsonb;principal text;valid boolean:=false;
begin
 identity:=authz.nexloop_browser_identity_snapshot(p_digest,p_world);binding:=identity->'binding';principal:=binding->>'subject_principal_id';
 case p_kind
 when 'subject' then valid:=p_key=array[binding->>'subject_id'];
 when 'membership' then valid:=p_key=array[binding->>'subject_id',principal];
 when 'actor','subject_authority' then valid:=p_key=array[principal];
 when 'browser_authentication' then valid:=p_key=array[binding->>'session_id'];
 when 'application' then valid:=p_key=array[binding->>'caller_application_id',binding->>'caller_application_version'];
 when 'grants' then valid:=cardinality(p_key)=2 and p_key[1]=principal;
 when 'scope','controls','policies' then valid:=cardinality(p_key)=3 and p_key[1]=principal;
 when 'resource_graph' then valid:=cardinality(p_key)=1;
 when 'revision' then valid:=p_key=array['catalog'];
 else valid:=false;end case;
 if valid is distinct from true then raise exception 'browser authority fact binding denied' using errcode='42501';end if;
 if p_kind='subject' then
  select su.payload into body from control.nexloop_browser_subjects su where su.subject_id=binding->>'subject_id';
  value:=jsonb_build_object('tenant_id',binding->>'tenant_id','subject_id',body->>'subject_id','kind',body->>'kind','status',body->>'status','revision',body->'revision','repository_witness','nexloop-postgres-authority-v1');
 elsif p_kind='membership' then
  select bm.payload into body from control.nexloop_browser_memberships bm where bm.tenant_id=binding->>'tenant_id' and bm.principal_id=principal;
  value:=jsonb_build_object('tenant_id',binding->>'tenant_id','subject_id',body->>'subject_id','principal_id',principal,'kind',body->>'kind','status',body->>'status','valid_from',body->'valid_from','valid_until',body->'valid_until','revision',body->'revision','repository_witness','nexloop-postgres-authority-v1');
 elsif p_kind='browser_authentication' then
  value:=binding||jsonb_build_object('status','active','expires_at',identity->'expires_at','absolute_expires_at',identity->'absolute_expires_at','repository_witness','nexloop-postgres-authority-v1');
 else
  select af.payload into value from authz.nexloop_authority_facts af where af.tenant_id=binding->>'tenant_id' and af.fact_kind=p_kind and af.entity_key=p_key;
 end if;
 if value is null then return null;end if;
 if p_kind='actor' and (value->>'kind' is distinct from 'human' or value->>'actor_principal_id' is distinct from principal) then raise exception 'browser actor unavailable' using errcode='42501';end if;
 return jsonb_build_object('payload',value,'record_hash',encode(sha256(convert_to((case when p_kind='browser_authentication' then value-'expires_at' else value end)::text,'UTF8')),'hex'));
end $$;
