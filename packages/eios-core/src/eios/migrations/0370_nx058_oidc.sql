-- NX-058 slice F (docs/design/account-self-service.md §5; owner 2026-10-28: code now, tested against an in-process IdP only; the stage IdP
-- is decided later): workbench single sign-on by OIDC (nexloop_eios.workbench_oidc does the protocol). The migration number is temporary.
--
-- * Links: control.nexloop_workbench_external_identities maps (provider, issuer, IdP subject) to one existing workbench member. A first
--   login never creates anything: an IdP subject nobody linked is refused. One link per member and per IdP subject. Links are made and
--   removed only by the governed owner Actions nexloop.workbench_member.link_external / unlink_external, for operators and reviewers (an
--   owner target is refused: owner links wait for an NX-056 approval policy, design S5). login_mode sso_only or sso_or_password.
-- * sso_only: the member's password no longer opens a session (a trigger refuses password Evidence for the subject, so the login and the
--   password change both fail closed), and no reset code can be issued for it (a trigger on the reset codes, so issue_reset and
--   issue_owner_reset both refuse with target_sso_only). Linking as sso_only ends the member's sessions and unused reset codes.
-- * Unlinking ends the member's sessions (SSO sessions share the account's session epoch). Revoking a member removes its link in the same
--   transaction (a trigger on the membership), so a revoked member - the only kind the staff erasure takes - has none left.
-- * Login: the start stores the HMACs of a state and of this browser's binding cookie for 5 minutes; the callback takes them once. The
--   session is the member's local-account session (credential_kind local_account, so every 0027/0031 read and every epoch kick applies)
--   with authentication_methods [oidc]; it is created here after rechecking the link, the membership, the subject and the account, and a
--   login event names the provider. Failures write an audit event with a fixed reason; the browser is told nothing more.
set local timezone = 'UTC';

create table control.nexloop_workbench_external_identities (
 tenant_id text not null references control.nexloop_tenants(tenant_id),
 provider_id text not null check(provider_id~'^[a-z][a-z0-9_-]{0,31}$'),
 issuer text not null check(char_length(issuer) between 9 and 2048 and issuer like 'https://%'),
 external_subject text not null check(char_length(external_subject) between 1 and 255),
 principal_id text not null,subject_id text not null,
 login_mode text not null check(login_mode in ('sso_only','sso_or_password')),
 linked_by_principal text not null,request_id text not null,linked_at timestamptz not null,
 primary key(tenant_id,provider_id,external_subject),
 unique(tenant_id,principal_id)
);
alter table control.nexloop_workbench_external_identities owner to nexloop_owner;
alter table control.nexloop_workbench_external_identities enable row level security;
alter table control.nexloop_workbench_external_identities force row level security;
create policy tenant_boundary on control.nexloop_workbench_external_identities to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
revoke all on control.nexloop_workbench_external_identities from public;
create index nexloop_workbench_external_identities_subject on control.nexloop_workbench_external_identities(tenant_id,subject_id);

-- Link and unlink history (append-only, the replay record of the Actions): principals, provider and mode only, never the IdP subject.
create table control.nexloop_workbench_external_identity_events (
 event_id uuid primary key default gen_random_uuid(),tenant_id text not null references control.nexloop_tenants(tenant_id),
 request_id text not null check(char_length(request_id) between 1 and 200),
 kind text not null check(kind in ('linked','unlinked','member_revoked')),
 target_principal text not null,provider_id text not null,login_mode text not null,
 actor_principal text not null,created_at timestamptz not null,
 unique(tenant_id,request_id,kind)
);
alter table control.nexloop_workbench_external_identity_events owner to nexloop_owner;
alter table control.nexloop_workbench_external_identity_events enable row level security;
alter table control.nexloop_workbench_external_identity_events force row level security;
create policy tenant_boundary on control.nexloop_workbench_external_identity_events to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
revoke all on control.nexloop_workbench_external_identity_events from public;
create function control.nexloop_workbench_external_identity_events_append_only() returns trigger language plpgsql
 set search_path=pg_catalog,pg_temp as $$ begin raise exception 'external identity events are append-only' using errcode='42501'; end $$;
create trigger append_only before update or delete on control.nexloop_workbench_external_identity_events
 for each row execute function control.nexloop_workbench_external_identity_events_append_only();

-- One login in flight: HMAC(state) and HMAC(browser binding) for 5 minutes, taken once.
create table control.nexloop_workbench_oidc_states (
 state_hmac text primary key check(state_hmac~'^[0-9a-f]{64}$'),tenant_id text not null references control.nexloop_tenants(tenant_id),
 provider_id text not null check(provider_id~'^[a-z][a-z0-9_-]{0,31}$'),
 binding_hmac text not null check(binding_hmac~'^[0-9a-f]{64}$'),
 created_at timestamptz not null,expires_at timestamptz not null,taken_at timestamptz
);
alter table control.nexloop_workbench_oidc_states owner to nexloop_owner;
alter table control.nexloop_workbench_oidc_states enable row level security;
alter table control.nexloop_workbench_oidc_states force row level security;
create policy tenant_boundary on control.nexloop_workbench_oidc_states to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
revoke all on control.nexloop_workbench_oidc_states from public;

-- Whether a subject of the tenant may use its password: false once linked as sso_only.
create function control.nexloop_workbench_password_allowed(p_tenant text,p_subject text) returns boolean
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare saved text:=current_setting('eios.tenant_id',true);found_link boolean;
begin
 -- Called from triggers too: the tenant boundary is set for this read and the caller's setting restored.
 perform set_config('eios.tenant_id',p_tenant,true);
 found_link:=exists(select 1 from control.nexloop_workbench_external_identities where tenant_id=p_tenant and subject_id=p_subject and login_mode='sso_only');
 perform set_config('eios.tenant_id',coalesce(saved,''),true);
 return not found_link;
end $$;

-- sso_only: no password Evidence (session.create or password.change) for the subject; the login answers like a wrong password.
create function control.nexloop_workbench_sso_only_evidence() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp as $$
begin
 if new.facts->'authentication_methods' ? 'password' and not control.nexloop_workbench_password_allowed(new.tenant_id,new.facts->>'subject_id') then
  raise exception 'authentication rejected' using errcode='EID03';end if;
 return new;
end $$;
create trigger nx058_sso_only before insert on control.nexloop_browser_evidence
 for each row execute function control.nexloop_workbench_sso_only_evidence();

-- sso_only: no reset code (design S5), whichever Action issues it.
create function control.nexloop_workbench_sso_only_reset() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp as $$
begin
 if not control.nexloop_workbench_password_allowed(new.tenant_id,new.target_subject) then
  raise exception 'workbench member: target_sso_only' using errcode='22023';end if;
 return new;
end $$;
create trigger nx058_sso_only before insert on control.nexloop_workbench_reset_codes
 for each row execute function control.nexloop_workbench_sso_only_reset();

-- A revoked member keeps no link (I4; the staff erasure takes revoked members only).
create function control.nexloop_workbench_unlink_revoked() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare l control.nexloop_workbench_external_identities;saved text:=current_setting('eios.tenant_id',true);
begin
 if new.payload->>'status'='revoked' and old.payload->>'status' is distinct from 'revoked' then
  perform set_config('eios.tenant_id',new.tenant_id,true);
  delete from control.nexloop_workbench_external_identities where tenant_id=new.tenant_id and principal_id=new.principal_id returning * into l;
  if found then
   insert into control.nexloop_workbench_external_identity_events(tenant_id,request_id,kind,target_principal,provider_id,login_mode,actor_principal,created_at)
    values(new.tenant_id,'member-revoked:'||gen_random_uuid(),'member_revoked',l.principal_id,l.provider_id,l.login_mode,'membership',clock_timestamp());
  end if;
  perform set_config('eios.tenant_id',coalesce(saved,''),true);
 end if;
 return null;
end $$;
create trigger nx058_unlink_revoked after update of payload on control.nexloop_browser_memberships
 for each row execute function control.nexloop_workbench_unlink_revoked();

create function control.nexloop_governed_link_workbench_external(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare a control.nexloop_browser_accounts;e control.nexloop_workbench_external_identity_events;target text:=body->>'target_principal';
begin
 if jsonb_typeof(body) is distinct from 'object' or exists(select 1 from jsonb_object_keys(body) k
   where k not in ('operation','request_id','target_principal','provider_id','issuer','external_subject','login_mode'))
  or jsonb_typeof(body->'request_id') is distinct from 'string' or char_length(body->>'request_id') not between 1 and 200
  or jsonb_typeof(body->'target_principal') is distinct from 'string' or char_length(target) not between 1 and 320
  or coalesce(body->>'provider_id','')!~'^[a-z][a-z0-9_-]{0,31}$'
  or jsonb_typeof(body->'issuer') is distinct from 'string' or char_length(body->>'issuer') not between 9 and 2048 or body->>'issuer' not like 'https://%'
  or jsonb_typeof(body->'external_subject') is distinct from 'string' or char_length(body->>'external_subject') not between 1 and 255
  or body->>'external_subject'<>btrim(body->>'external_subject')
  or coalesce(body->>'login_mode','') not in ('sso_only','sso_or_password') then
  raise exception 'workbench member: invalid' using errcode='22023';end if;
 if p_world is distinct from 'real' then raise exception 'workbench member: owner_world_only' using errcode='22023';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 perform control.nexloop_workbench_member_lock(p_tenant);
 select * into e from control.nexloop_workbench_external_identity_events where tenant_id=p_tenant and request_id=body->>'request_id' and kind='linked';
 if found then
  if e.target_principal is distinct from target or e.provider_id is distinct from body->>'provider_id' or e.login_mode is distinct from body->>'login_mode' then
   raise exception 'workbench member: request_reused' using errcode='22023';end if;
  return jsonb_build_object('target_principal',target,'provider_id',e.provider_id,'login_mode',e.login_mode,'replayed',true);
 end if;
 if not control.nexloop_workbench_member_active(p_tenant,p_principal,true) then raise exception 'workbench member: issuer_not_owner' using errcode='22023';end if;
 if target=p_principal then raise exception 'workbench member: target_is_requester' using errcode='22023';end if;
 -- An owner's link waits for an approval policy (design S5); operators and reviewers only, rechecked under the lock (V1).
 if control.nexloop_workbench_role(p_tenant,target)='owner' then raise exception 'workbench member: target_role_changed' using errcode='22023';end if;
 if not control.nexloop_workbench_member_active(p_tenant,target,false) then raise exception 'workbench member: target_not_active' using errcode='22023';end if;
 a:=control.nexloop_workbench_member_account(p_tenant,target);
 if a.local_account_id is null or a.payload->>'status' is distinct from 'active' then raise exception 'workbench member: target_not_active' using errcode='22023';end if;
 if exists(select 1 from control.nexloop_workbench_external_identities where tenant_id=p_tenant and principal_id=target) then
  raise exception 'workbench member: already_linked' using errcode='22023';end if;
 if exists(select 1 from control.nexloop_workbench_external_identities where tenant_id=p_tenant and provider_id=body->>'provider_id' and external_subject=body->>'external_subject') then
  raise exception 'workbench member: external_identity_taken' using errcode='22023';end if;
 insert into control.nexloop_workbench_external_identities(tenant_id,provider_id,issuer,external_subject,principal_id,subject_id,login_mode,linked_by_principal,request_id,linked_at)
  values(p_tenant,body->>'provider_id',body->>'issuer',body->>'external_subject',target,a.subject_id,body->>'login_mode',p_principal,body->>'request_id',clock_timestamp());
 insert into control.nexloop_workbench_external_identity_events(tenant_id,request_id,kind,target_principal,provider_id,login_mode,actor_principal,created_at)
  values(p_tenant,body->>'request_id','linked',target,body->>'provider_id',body->>'login_mode',p_principal,clock_timestamp());
 if body->>'login_mode'='sso_only' then
  -- The password stops opening sessions: the ones it opened end, and so do unused reset codes.
  perform control.nexloop_workbench_reset_revoke(p_tenant,a.subject_id,'member_changed');
  perform control.nexloop_workbench_kick(p_tenant,a);
 end if;
 insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,payload,created_at)
  values('workbench-sso-link:'||p_tenant||':'||(body->>'request_id'),'nexloop.workbench_member.link_external',p_tenant,p_principal,'workbench_member',p_intent,
   jsonb_build_object('target_principal',target,'provider_id',body->>'provider_id','login_mode',body->>'login_mode'),clock_timestamp());
 return jsonb_build_object('target_principal',target,'provider_id',body->>'provider_id','login_mode',body->>'login_mode','replayed',false);
end $$;

create function control.nexloop_governed_unlink_workbench_external(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare a control.nexloop_browser_accounts;e control.nexloop_workbench_external_identity_events;l control.nexloop_workbench_external_identities;target text:=body->>'target_principal';
begin
 if jsonb_typeof(body) is distinct from 'object' or exists(select 1 from jsonb_object_keys(body) k where k not in ('operation','request_id','target_principal'))
  or jsonb_typeof(body->'request_id') is distinct from 'string' or char_length(body->>'request_id') not between 1 and 200
  or jsonb_typeof(body->'target_principal') is distinct from 'string' or char_length(target) not between 1 and 320 then
  raise exception 'workbench member: invalid' using errcode='22023';end if;
 if p_world is distinct from 'real' then raise exception 'workbench member: owner_world_only' using errcode='22023';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 perform control.nexloop_workbench_member_lock(p_tenant);
 select * into e from control.nexloop_workbench_external_identity_events where tenant_id=p_tenant and request_id=body->>'request_id' and kind='unlinked';
 if found then
  if e.target_principal is distinct from target then raise exception 'workbench member: request_reused' using errcode='22023';end if;
  return jsonb_build_object('target_principal',target,'provider_id',e.provider_id,'replayed',true);
 end if;
 if not control.nexloop_workbench_member_active(p_tenant,p_principal,true) then raise exception 'workbench member: issuer_not_owner' using errcode='22023';end if;
 if target=p_principal then raise exception 'workbench member: target_is_requester' using errcode='22023';end if;
 if control.nexloop_workbench_role(p_tenant,target)='owner' then raise exception 'workbench member: target_role_changed' using errcode='22023';end if;
 delete from control.nexloop_workbench_external_identities where tenant_id=p_tenant and principal_id=target returning * into l;
 if not found then raise exception 'workbench member: not_linked' using errcode='22023';end if;
 insert into control.nexloop_workbench_external_identity_events(tenant_id,request_id,kind,target_principal,provider_id,login_mode,actor_principal,created_at)
  values(p_tenant,body->>'request_id','unlinked',target,l.provider_id,l.login_mode,p_principal,clock_timestamp());
 -- Every session ends (SSO sessions share the account's epoch); the member signs in again with what is left (the password, unless sso_only
 -- had made it unusable: then an owner issues a reset code, which is possible again now).
 a:=control.nexloop_workbench_member_account(p_tenant,target);
 if a.local_account_id is not null then perform control.nexloop_workbench_kick(p_tenant,a);end if;
 insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,payload,created_at)
  values('workbench-sso-unlink:'||p_tenant||':'||(body->>'request_id'),'nexloop.workbench_member.unlink_external',p_tenant,p_principal,'workbench_member',p_intent,
   jsonb_build_object('target_principal',target,'provider_id',l.provider_id,'login_mode',l.login_mode),clock_timestamp());
 return jsonb_build_object('target_principal',target,'provider_id',l.provider_id,'replayed',false);
end $$;

-- The start (nexloop_identity): one state of a configured provider, 5 minutes. States older than a day are cleared here.
create function control.nexloop_workbench_oidc_begin(p_tenant text,p_provider text,p_state_hmac text,p_binding_hmac text) returns void
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
begin
 if session_user<>'nexloop_identity' or coalesce(p_state_hmac,'')!~'^[0-9a-f]{64}$' or coalesce(p_binding_hmac,'')!~'^[0-9a-f]{64}$'
  or coalesce(p_provider,'')!~'^[a-z][a-z0-9_-]{0,31}$' then raise exception 'identity unavailable' using errcode='42501';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 delete from control.nexloop_workbench_oidc_states where tenant_id=p_tenant and expires_at<clock_timestamp()-interval '1 day';
 insert into control.nexloop_workbench_oidc_states(state_hmac,tenant_id,provider_id,binding_hmac,created_at,expires_at)
  values(p_state_hmac,p_tenant,p_provider,p_binding_hmac,clock_timestamp(),clock_timestamp()+interval '5 minutes');
end $$;

-- The callback (nexloop_identity): takes the state once, whatever follows; its provider when it was unexpired and this browser's.
create function control.nexloop_workbench_oidc_take(p_tenant text,p_state_hmac text,p_binding_hmac text) returns text
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare s control.nexloop_workbench_oidc_states;
begin
 if session_user<>'nexloop_identity' or coalesce(p_state_hmac,'')!~'^[0-9a-f]{64}$' or coalesce(p_binding_hmac,'')!~'^[0-9a-f]{64}$' then
  raise exception 'identity unavailable' using errcode='42501';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 update control.nexloop_workbench_oidc_states set taken_at=clock_timestamp()
  where tenant_id=p_tenant and state_hmac=p_state_hmac and taken_at is null returning * into s;
 if not found or s.expires_at<=clock_timestamp() or s.binding_hmac<>p_binding_hmac then return null;end if;
 return s.provider_id;
end $$;

-- The facts of the member a verified IdP subject is linked to, for a session (nexloop_identity); null when there is none to sign in now.
create function control.nexloop_workbench_oidc_resolve(p_tenant text,p_application text,p_provider text,p_issuer text,p_external text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare l control.nexloop_workbench_external_identities;m jsonb;s jsonb;a control.nexloop_browser_accounts;app integer;now_at timestamptz:=clock_timestamp();
begin
 if session_user<>'nexloop_identity' then raise exception 'identity unavailable' using errcode='42501';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into l from control.nexloop_workbench_external_identities where tenant_id=p_tenant and provider_id=p_provider and external_subject=p_external;
 if not found or l.issuer is distinct from p_issuer then return null;end if;
 select revision into app from control.nexloop_browser_applications where tenant_id=p_tenant and application_id=p_application and active;
 select payload into m from control.nexloop_browser_memberships where tenant_id=p_tenant and principal_id=l.principal_id;
 select payload into s from control.nexloop_browser_subjects where subject_id=l.subject_id;
 select * into a from control.nexloop_browser_accounts where tenant_id=p_tenant and subject_id=l.subject_id;
 if app is null or m is null or s is null or a.local_account_id is null or control.nexloop_workbench_role(p_tenant,l.principal_id) is null
  or m->>'status' is distinct from 'active' or m->>'subject_id' is distinct from l.subject_id
  or (m->>'valid_from')::timestamptz>now_at or (m->>'valid_until')::timestamptz<=now_at
  or s->>'kind' is distinct from 'human' or s->>'status' is distinct from 'active'
  or a.password_hash is null or a.payload->>'status' is distinct from 'active' or (a.payload->>'locked_until')::timestamptz>now_at then
  return jsonb_build_object('unavailable',true);end if;
 return jsonb_build_object('unavailable',false,'membership',m,'application_revision',app,'subject_revision',(s->>'revision')::integer,
  'credential_id',a.local_account_id,'credential_revision',(a.payload->>'revision')::integer,
  'credential_session_epoch',(a.payload->>'session_epoch')::integer,'restricted',(a.payload->>'must_change_password')::boolean);
end $$;

-- The session (nexloop_identity): every fact rechecked against the link, membership, subject and account now; then inserted like 0026
-- inserts a password session, and a login event names the provider (its public configuration fingerprint, never a token or the subject).
create function control.nexloop_create_workbench_oidc_session(p_tenant text,p_application text,p_provider text,p_issuer text,p_external text,
  p_fingerprint text,p_body jsonb,p_token bytea,p_csrf bytea,p_trace text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare facts jsonb;now_at timestamptz;field_name text;expected jsonb;
begin
 if session_user<>'nexloop_identity' or jsonb_typeof(p_body) is distinct from 'object' or coalesce(p_fingerprint,'')!~'^[0-9a-f]{64}$'
  or p_token is null or p_csrf is null or octet_length(p_token)<>32 or octet_length(p_csrf)<>32 or p_token=p_csrf
  or p_trace is null or length(p_trace) not between 1 and 320
  or p_body->>'tenant_id' is distinct from p_tenant or p_body->>'application_id' is distinct from p_application then
  raise exception 'identity unavailable' using errcode='42501';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 perform control.nexloop_workbench_member_lock(p_tenant);
 facts:=control.nexloop_workbench_oidc_resolve(p_tenant,p_application,p_provider,p_issuer,p_external);
 if facts is null or (facts->>'unavailable')::boolean then raise exception 'authentication rejected' using errcode='EID03';end if;
 expected:=jsonb_build_object('subject_id',facts->'membership'->'subject_id','principal_id',facts->'membership'->'principal_id',
  'membership_revision',facts->'membership'->'revision','subject_kind','human','subject_revision',facts->'subject_revision',
  'credential_kind','local_account','credential_tenant_id',p_tenant,'credential_id',facts->'credential_id',
  'credential_revision',facts->'credential_revision','credential_session_epoch',facts->'credential_session_epoch',
  'provider_id',null,'provider_revision',null,'provider_configuration_fingerprint',null,
  'authentication_methods',jsonb_build_array('oidc'),'restricted',facts->'restricted','application_revision',facts->'application_revision');
 foreach field_name in array array(select jsonb_object_keys(expected)) loop
  if p_body->field_name is distinct from expected->field_name then raise exception 'authentication rejected' using errcode='EID03';end if;
 end loop;
 now_at:=clock_timestamp();
 if p_body->>'session_id' is null or length(p_body->>'session_id') not between 1 and 320 or p_body->'revision' is distinct from '1'::jsonb
  or p_body->'revoked_at' is distinct from 'null'::jsonb or p_body->>'created_at' is null
  or p_body->>'last_seen_at' is distinct from p_body->>'created_at' or p_body->>'idle_expires_at' is null or p_body->>'absolute_expires_at' is null
  or (p_body->>'created_at')::timestamptz>now_at or (p_body->>'created_at')::timestamptz<now_at-interval '1 minute'
  or (p_body->>'idle_expires_at')::timestamptz<>(p_body->>'created_at')::timestamptz+interval '7 days'
  or (p_body->>'absolute_expires_at')::timestamptz<>(p_body->>'created_at')::timestamptz+interval '30 days' then
  raise exception 'authentication rejected' using errcode='EID03';end if;
 insert into control.nexloop_browser_sessions values(p_body->>'session_id',p_token,p_csrf,p_tenant,p_application,p_body);
 insert into control.nexloop_browser_login_events(tenant_id,subject_id,event_type,outcome,operator_principal_id,request_id,trace_id,details,created_at)
  values(p_tenant,p_body->>'subject_id','oidc_login','success','nexloop_identity','oidc-login:'||(p_body->>'session_id'),p_trace,
   jsonb_build_object('provider_id',p_provider,'provider_configuration_fingerprint',p_fingerprint,'principal_id',p_body->>'principal_id'),now_at);
 return p_body;
end $$;

-- A refused SSO login (nexloop_identity): an audit event with the fixed reason and the provider, nothing the browser sent.
create function control.nexloop_workbench_oidc_failed(p_tenant text,p_provider text,p_reason text,p_trace text) returns void
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare id uuid:=gen_random_uuid();
begin
 if session_user<>'nexloop_identity' or (p_provider is not null and p_provider!~'^[a-z][a-z0-9_-]{0,31}$')
  or coalesce(p_reason,'') not in ('state_invalid','provider_error','token_exchange_failed','id_token_invalid','not_linked','member_unavailable')
  or p_trace is null or length(p_trace) not between 1 and 320 then raise exception 'identity unavailable' using errcode='42501';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,payload,created_at)
  values('workbench-sso-failed:'||id,'nexloop.workbench_member.sso_login_failed',p_tenant,'nexloop_identity',p_trace,'workbench-sso-failed:'||id,
   jsonb_build_object('provider_id',p_provider,'reason',p_reason),clock_timestamp());
end $$;

do $$ declare f text; begin
 foreach f in array array['control.nexloop_workbench_password_allowed(text,text)','control.nexloop_workbench_sso_only_evidence()',
   'control.nexloop_workbench_sso_only_reset()','control.nexloop_workbench_unlink_revoked()',
   'control.nexloop_workbench_external_identity_events_append_only()',
   'control.nexloop_governed_link_workbench_external(text,text,text,text,text,jsonb)',
   'control.nexloop_governed_unlink_workbench_external(text,text,text,text,text,jsonb)',
   'control.nexloop_workbench_oidc_begin(text,text,text,text)','control.nexloop_workbench_oidc_take(text,text,text)',
   'control.nexloop_workbench_oidc_resolve(text,text,text,text,text)',
   'control.nexloop_create_workbench_oidc_session(text,text,text,text,text,text,jsonb,bytea,bytea,text)',
   'control.nexloop_workbench_oidc_failed(text,text,text,text)'] loop
  execute format('alter function %s owner to nexloop_owner',f);
  execute format('revoke all on function %s from public',f);
 end loop;
end $$;
grant execute on function control.nexloop_workbench_oidc_begin(text,text,text,text),control.nexloop_workbench_oidc_take(text,text,text),
 control.nexloop_workbench_oidc_resolve(text,text,text,text,text),
 control.nexloop_create_workbench_oidc_session(text,text,text,text,text,text,jsonb,bytea,bytea,text),
 control.nexloop_workbench_oidc_failed(text,text,text,text) to nexloop_identity;
insert into control.nexloop_governed_capabilities(capability,operation,subject_rule,handler,source_task) values
 ('workbench_member.link_external','link_workbench_external','human','control.nexloop_governed_link_workbench_external(text,text,text,text,text,jsonb)','NX-058'),
 ('workbench_member.unlink_external','unlink_workbench_external','human','control.nexloop_governed_unlink_workbench_external(text,text,text,text,text,jsonb)','NX-058');
