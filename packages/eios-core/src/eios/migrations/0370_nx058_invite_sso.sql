-- NX-058 slice F (design §5): an invitation may name the member "SSO only". The migration number is temporary; it lives only in the file
-- name and the catalog.
-- * invite (operators and reviewers only; invite_owner is unchanged and refuses the key, an owner's SSO waits for its own policy) takes an
--   optional sso_provider, a configured provider the workbench entry checked. Its replay compares it too.
-- * Accepting such an invitation sets no password (the entry stores an unusable hash it never shows) and leaves the member pending: until
--   an owner links its IdP subject the member has no way in - password Evidence and reset codes are refused as for sso_only, and the SSO
--   login finds no link. The link of a pending member must name the invitation's provider and sso_only; it ends the pending state.
-- * A pending state ends with the membership's revocation and with a promotion to owner (as a link does, 0365), so nothing outlives the
--   member and an owner keeps its password path (the owner reset).

alter table control.nexloop_workbench_invitations add column sso_provider text
 check(sso_provider is null or (sso_provider~'^[a-z][a-z0-9_-]{0,31}$' and role in ('operator','reviewer')));

create table control.nexloop_workbench_sso_pending (
 tenant_id text not null references control.nexloop_tenants(tenant_id),
 principal_id text not null,subject_id text not null,
 provider_id text not null check(provider_id~'^[a-z][a-z0-9_-]{0,31}$'),
 invitation_id uuid not null,created_at timestamptz not null,
 primary key(tenant_id,principal_id)
);
alter table control.nexloop_workbench_sso_pending owner to nexloop_owner;
alter table control.nexloop_workbench_sso_pending enable row level security;
alter table control.nexloop_workbench_sso_pending force row level security;
create policy tenant_boundary on control.nexloop_workbench_sso_pending to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
revoke all on control.nexloop_workbench_sso_pending from public;
create index nexloop_workbench_sso_pending_subject on control.nexloop_workbench_sso_pending(tenant_id,subject_id);

-- As 0335's, with the optional sso_provider.
create or replace function control.nexloop_governed_invite_workbench_member(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare r control.nexloop_workbench_invitations;v_hours integer;
begin
 if jsonb_typeof(body) is distinct from 'object' or exists(select 1 from jsonb_object_keys(body) k where k not in ('operation','request_id','username','role','token_hmac','issuer_source','expires_in_hours','sso_provider'))
  or jsonb_typeof(body->'request_id') is distinct from 'string' or char_length(body->>'request_id') not between 1 and 200
  or coalesce(body->>'username','')!~'^[a-z0-9][a-z0-9._-]{2,63}$' or coalesce(body->>'token_hmac','')!~'^[0-9a-f]{64}$'
  or coalesce(body->>'issuer_source','')!~'^[0-9a-f]{64}$' or jsonb_typeof(body->'expires_in_hours') is distinct from 'number'
  or (body->>'expires_in_hours')!~'^[0-9]{1,2}$' or (body->>'expires_in_hours')::integer not between 1 and 72
  or (body ? 'sso_provider' and (jsonb_typeof(body->'sso_provider') is distinct from 'string' or body->>'sso_provider'!~'^[a-z][a-z0-9_-]{0,31}$')) then
  raise exception 'workbench member: invalid' using errcode='22023';end if;
 -- invite is for operators and reviewers; an owner is invited by invite_owner, with approval (slice C, design §1b).
 if body->>'role' is null or body->>'role' not in ('operator','reviewer') then raise exception 'workbench member: role_not_allowed' using errcode='22023';end if;
 if p_world is distinct from 'real' then raise exception 'workbench member: owner_world_only' using errcode='22023';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 perform control.nexloop_workbench_member_lock(p_tenant);
 select * into r from control.nexloop_workbench_invitations where tenant_id=p_tenant and request_id=body->>'request_id';
 if found then
  if (r.username,r.role,r.token_hmac,r.sso_provider) is distinct from (body->>'username',body->>'role',body->>'token_hmac',body->>'sso_provider') then
   raise exception 'workbench member: request_reused' using errcode='22023';end if;
  return jsonb_build_object('invitation_id',r.invitation_id,'username',r.username,'role',r.role,'expires_at',r.expires_at,'sso_provider',r.sso_provider,'replayed',true);
 end if;
 if not control.nexloop_workbench_member_active(p_tenant,p_principal,true) then raise exception 'workbench member: issuer_not_owner' using errcode='22023';end if;
 if (select count(*) from control.nexloop_workbench_invitations where tenant_id=p_tenant and issued_by_principal=p_principal
     and issued_at>clock_timestamp()-interval '15 minutes')>=10 then raise exception 'workbench member: rate_limited' using errcode='22023';end if;
 if exists(select 1 from control.nexloop_browser_accounts a where a.tenant_id=p_tenant and lower(a.username)=body->>'username') then
  raise exception 'workbench member: username_taken' using errcode='22023';end if;
 update control.nexloop_workbench_invitations set state='revoked',revoked_reason='reissued' where tenant_id=p_tenant and username=body->>'username' and state='issued';
 v_hours:=(body->>'expires_in_hours')::integer;
 insert into control.nexloop_workbench_invitations(tenant_id,request_id,username,role,token_hmac,issued_by_principal,issued_by_subject,issued_at,expires_at,issuer_source,state,sso_provider)
  values(p_tenant,body->>'request_id',body->>'username',body->>'role',body->>'token_hmac',p_principal,p_subject,clock_timestamp(),
   clock_timestamp()+make_interval(hours=>v_hours),body->>'issuer_source','issued',body->>'sso_provider') returning * into r;
 insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,payload,created_at)
  values('workbench-invite:'||r.invitation_id,'nexloop.workbench_member.invite',p_tenant,p_principal,'workbench_member',p_intent,
   jsonb_build_object('invitation_id',r.invitation_id,'role',r.role,'expires_at',r.expires_at,'sso_provider',r.sso_provider),clock_timestamp());
 return jsonb_build_object('invitation_id',r.invitation_id,'username',r.username,'role',r.role,'expires_at',r.expires_at,'sso_provider',r.sso_provider,'replayed',false);
end $$;

-- As 0335's, and whether the invitation is SSO only (the entry then takes no password).
create or replace function control.nexloop_workbench_invitation_lookup(p_tenant text,p_token_hmac text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare r control.nexloop_workbench_invitations;
begin
 if session_user<>'nexloop_identity' or coalesce(p_token_hmac,'')!~'^[0-9a-f]{64}$' then raise exception 'identity unavailable' using errcode='42501';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into r from control.nexloop_workbench_invitations where tenant_id=p_tenant and token_hmac=p_token_hmac;
 if not found or r.state<>'issued' or r.expires_at<=clock_timestamp() then return null;end if;
 return jsonb_build_object('invitation_id',r.invitation_id,'username',r.username,'role',r.role,'sso_only',r.sso_provider is not null);
end $$;

-- The acceptance of an SSO-only invitation leaves the member pending, in the acceptance's transaction (every acceptance sets 'accepted').
create function control.nexloop_workbench_sso_invitation_accepted() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare saved text:=current_setting('eios.tenant_id',true);
begin
 if new.state='accepted' and old.state is distinct from 'accepted' and new.sso_provider is not null then
  if new.accepted_principal is null or new.accepting_subject is null then raise exception 'invitation invalid' using errcode='22023';end if;
  perform set_config('eios.tenant_id',new.tenant_id,true);
  insert into control.nexloop_workbench_sso_pending(tenant_id,principal_id,subject_id,provider_id,invitation_id,created_at)
   values(new.tenant_id,new.accepted_principal,new.accepting_subject,new.sso_provider,new.invitation_id,clock_timestamp());
  perform set_config('eios.tenant_id',coalesce(saved,''),true);
 end if;
 return null;
end $$;
create trigger nx058_sso_invitation_accepted after update of state on control.nexloop_workbench_invitations
 for each row execute function control.nexloop_workbench_sso_invitation_accepted();

-- As 0365's, and false while the member is pending: neither the password nor a reset code opens a way in before the link.
create or replace function control.nexloop_workbench_password_allowed(p_tenant text,p_subject text) returns boolean
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare saved text:=current_setting('eios.tenant_id',true);blocked boolean;
begin
 -- Called from triggers too: the tenant boundary is set for this read and the caller's setting restored.
 perform set_config('eios.tenant_id',p_tenant,true);
 blocked:=exists(select 1 from control.nexloop_workbench_external_identities where tenant_id=p_tenant and subject_id=p_subject and login_mode='sso_only')
  or exists(select 1 from control.nexloop_workbench_sso_pending where tenant_id=p_tenant and subject_id=p_subject);
 perform set_config('eios.tenant_id',coalesce(saved,''),true);
 return not blocked;
end $$;

-- The link of a pending member: the invitation's provider and sso_only only; the link ends the pending state. Every link goes through
-- link_external (0365), which inserts here under the member lock.
create function control.nexloop_workbench_sso_pending_link() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare p control.nexloop_workbench_sso_pending;saved text:=current_setting('eios.tenant_id',true);
begin
 perform set_config('eios.tenant_id',new.tenant_id,true);
 delete from control.nexloop_workbench_sso_pending where tenant_id=new.tenant_id and principal_id=new.principal_id returning * into p;
 perform set_config('eios.tenant_id',coalesce(saved,''),true);
 if p.principal_id is not null and (p.provider_id is distinct from new.provider_id or new.login_mode is distinct from 'sso_only') then
  raise exception 'workbench member: sso_invitation_mismatch' using errcode='22023';end if;
 return new;
end $$;
create trigger nx058_sso_pending_link before insert on control.nexloop_workbench_external_identities
 for each row execute function control.nexloop_workbench_sso_pending_link();

-- A revoked member, or one that became an owner, is no longer pending (as 0365 does with links).
create function control.nexloop_workbench_sso_pending_revoked() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare saved text:=current_setting('eios.tenant_id',true);
begin
 if new.payload->>'status'='revoked' and old.payload->>'status' is distinct from 'revoked' then
  perform set_config('eios.tenant_id',new.tenant_id,true);
  delete from control.nexloop_workbench_sso_pending where tenant_id=new.tenant_id and principal_id=new.principal_id;
  perform set_config('eios.tenant_id',coalesce(saved,''),true);
 end if;
 return null;
end $$;
create trigger nx058_sso_pending_revoked after update of payload on control.nexloop_browser_memberships
 for each row execute function control.nexloop_workbench_sso_pending_revoked();
create function control.nexloop_workbench_sso_pending_promoted() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare saved text:=current_setting('eios.tenant_id',true);
begin
 if new.role='owner' and new.event='start' then
  perform set_config('eios.tenant_id',new.tenant_id,true);
  delete from control.nexloop_workbench_sso_pending where tenant_id=new.tenant_id and principal_id=new.principal_id;
  perform set_config('eios.tenant_id',coalesce(saved,''),true);
 end if;
 return null;
end $$;
create trigger nx058_sso_pending_promoted after insert on control.nexloop_workbench_role_history
 for each row execute function control.nexloop_workbench_sso_pending_promoted();

do $$ declare f text; begin
 foreach f in array array['control.nexloop_governed_invite_workbench_member(text,text,text,text,text,jsonb)',
   'control.nexloop_workbench_invitation_lookup(text,text)','control.nexloop_workbench_sso_invitation_accepted()',
   'control.nexloop_workbench_password_allowed(text,text)','control.nexloop_workbench_sso_pending_link()',
   'control.nexloop_workbench_sso_pending_revoked()','control.nexloop_workbench_sso_pending_promoted()'] loop
  execute format('alter function %s owner to nexloop_owner',f);
  execute format('revoke all on function %s from public',f);
 end loop;
end $$;
grant execute on function control.nexloop_workbench_invitation_lookup(text,text) to nexloop_identity;
