-- NX-058 slice B (docs/design/account-self-service.md §2, §1b, §4; L6-reviewed v5): the owner issues a one-time reset code for an
-- operator or reviewer of the workbench, without e-mail. The migration number is temporary; it lives only in the file name and the
-- catalog.
--
-- * Issue: governed owner Action nexloop.workbench_member.issue_reset (capability workbench_member.issue_reset, operation
--   issue_workbench_reset). The workbench HTTP layer draws the 128-bit code and hands the handler only its HMAC (the key is private
--   configuration the database never sees). Under the tenant's member lock the handler rechecks the target: an active member of the
--   tenant's workbench, not an owner (owners: issue_owner_reset, slice C, with approval), not the requester. Every earlier unused code of
--   the target is revoked, the target's account gets session_epoch + 1 at once (no session survives), the code is stored by its HMAC
--   for 30 minutes, an audit event names issuer and target (never a code).
-- * Complete: nexloop_identity only (the unauthenticated workbench endpoint): the code's HMAC finds the record; it must be issued and
--   unexpired, its issuer still an active owner of the tenant's workbench, its target still an active non-owner member. The 0198
--   effect follows (new argon2id hash, history, session_epoch + 1, lockout cleared); the record is completed; a login event and an
--   audit event link issuer and completer (the reset id). A failure of the new password's rules (checked by the caller) counts against
--   the code; five void it. A reset failure never touches the login lockout.
-- * Notice: for 7 days after a completed reset every login shows it (control.nexloop_workbench_reset_notices); the member's own
--   security page lists the last 90 days; owners see every reset of 30 days. Nothing here can be dismissed.
set local timezone = 'UTC';

create table control.nexloop_workbench_reset_codes (
 tenant_id text not null references control.nexloop_tenants(tenant_id),reset_id uuid primary key default gen_random_uuid(),
 request_id text not null check(char_length(request_id) between 1 and 200),
 target_principal text not null,target_subject text not null,
 code_hmac text not null unique check(code_hmac~'^[0-9a-f]{64}$'),
 issued_by_principal text not null,issued_by_subject text not null,issued_at timestamptz not null,expires_at timestamptz not null,
 issuer_source text check(issuer_source is null or issuer_source~'^[0-9a-f]{64}$'),
 state text not null check(state in ('issued','completed','revoked','voided')),
 failures integer not null default 0 check(failures between 0 and 5),
 completed_at timestamptz,completer_source text check(completer_source is null or completer_source~'^[0-9a-f]{64}$'),
 revoked_reason text check(revoked_reason is null or revoked_reason in ('reissued','operator_reset','member_changed','password_changed')),
 unique(tenant_id,request_id)
);
alter table control.nexloop_workbench_reset_codes owner to nexloop_owner;
alter table control.nexloop_workbench_reset_codes enable row level security;
alter table control.nexloop_workbench_reset_codes force row level security;
create policy tenant_boundary on control.nexloop_workbench_reset_codes to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
revoke all on control.nexloop_workbench_reset_codes from public;
create index nexloop_workbench_reset_codes_target on control.nexloop_workbench_reset_codes(tenant_id,target_principal,state);

-- The tenant's member lock (design §1, Y1): the advisory lock every member change of the tenant takes first, before any row lock.
-- NX-056 takes the same key for its member changes and consumption once it is merged (one key, tenant level).
create function control.nexloop_workbench_member_lock(p_tenant text) returns void language sql
 set search_path=pg_catalog,pg_temp as $$ select pg_advisory_xact_lock(hashtextextended('nexloop-approval-eligibility:'||p_tenant,0)) $$;

-- The local account of a member principal of the tenant (membership -> subject -> account), or null.
create function control.nexloop_workbench_member_account(p_tenant text,p_principal text) returns control.nexloop_browser_accounts
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare m control.nexloop_browser_memberships;a control.nexloop_browser_accounts;
begin
 select * into m from control.nexloop_browser_memberships where tenant_id=p_tenant and principal_id=p_principal;
 if not found then return null;end if;
 select * into a from control.nexloop_browser_accounts where tenant_id=p_tenant and subject_id=m.subject_id;
 return a;
end $$;

-- Whether a principal is an active member of the tenant's workbench holding p_role (owner) or a non-owner role (p_role null).
create function control.nexloop_workbench_member_active(p_tenant text,p_principal text,p_owner boolean) returns boolean
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare r text:=control.nexloop_workbench_role(p_tenant,p_principal);m control.nexloop_browser_memberships;
begin
 if r is null or (r='owner') is distinct from p_owner then return false;end if;
 select * into m from control.nexloop_browser_memberships where tenant_id=p_tenant and principal_id=p_principal;
 return found and m.payload->>'status'='active';
end $$;

-- Bump an account's session epoch (every browser session of it fails its next read, 0027).
create function control.nexloop_workbench_kick(p_tenant text,p_account control.nexloop_browser_accounts) returns void
 language sql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
 update control.nexloop_browser_accounts set payload=payload||jsonb_build_object('session_epoch',(payload->>'session_epoch')::bigint+1,
   'revision',(payload->>'revision')::bigint+1,'updated_at',to_jsonb(clock_timestamp()))
  where tenant_id=p_tenant and local_account_id=(p_account).local_account_id $$;

-- The password write every reset shares (0198's operator reset since 0334, and the owner-issued code below; design §3, I-2): the new
-- argon2id hash, the previous one first in the history (at most 10), revision and session_epoch + 1, failures and lockout cleared.
create function control.nexloop_browser_password_write(p_tenant text,p_account control.nexloop_browser_accounts,p_hash text,p_at timestamptz) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare history text[];body jsonb;
begin
 history:=(case when (p_account).password_hash is null then (p_account).password_history else array[(p_account).password_hash]||(p_account).password_history end)[1:10];
 body:=(p_account).payload||jsonb_build_object('revision',((p_account).payload->>'revision')::bigint+1,'session_epoch',((p_account).payload->>'session_epoch')::bigint+1,
  'failed_attempts',0,'lockout_level',0,'locked_until',null,'updated_at',to_jsonb(p_at));
 update control.nexloop_browser_accounts a set payload=body,password_hash=p_hash,password_history=history
  where a.tenant_id=p_tenant and a.local_account_id=(p_account).local_account_id;
 return body;
end $$;

-- Every unused code of a subject's account ends (I4): an operator reset (0198, since 0334), a reissue, later a member change.
create function control.nexloop_workbench_reset_revoke(p_tenant text,p_subject text,p_reason text) returns void
 language sql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
 update control.nexloop_workbench_reset_codes set state='revoked',revoked_reason=p_reason
  where tenant_id=p_tenant and target_subject=p_subject and state='issued' $$;

create function control.nexloop_governed_issue_workbench_reset(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare r control.nexloop_workbench_reset_codes;a control.nexloop_browser_accounts;target_subject text;
begin
 if jsonb_typeof(body) is distinct from 'object' or exists(select 1 from jsonb_object_keys(body) k where k not in ('operation','request_id','target_principal','code_hmac','issuer_source'))
  or jsonb_typeof(body->'request_id') is distinct from 'string' or char_length(body->>'request_id') not between 1 and 200
  or jsonb_typeof(body->'target_principal') is distinct from 'string' or char_length(body->>'target_principal') not between 1 and 320
  or coalesce(body->>'code_hmac','')!~'^[0-9a-f]{64}$' or (body ? 'issuer_source' and coalesce(body->>'issuer_source','')!~'^[0-9a-f]{64}$') then
  raise exception 'workbench member: invalid' using errcode='22023';end if;
 if p_world is distinct from 'real' then raise exception 'workbench member: owner_world_only' using errcode='22023';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 perform control.nexloop_workbench_member_lock(p_tenant);
 select * into r from control.nexloop_workbench_reset_codes where tenant_id=p_tenant and request_id=body->>'request_id';
 if found then
  if r.target_principal is distinct from body->>'target_principal' or r.code_hmac is distinct from body->>'code_hmac' then
   raise exception 'workbench member: request_reused' using errcode='22023';end if;
  return jsonb_build_object('reset_id',r.reset_id,'target_principal',r.target_principal,'expires_at',r.expires_at,'replayed',true);
 end if;
 -- The issuer, rechecked under the lock (S-1): still an active owner of the tenant's workbench.
 if not control.nexloop_workbench_member_active(p_tenant,p_principal,true) then raise exception 'workbench member: issuer_not_owner' using errcode='22023';end if;
 -- Per issuing owner (design §4, I-1): at most 10 codes in 15 minutes, so a stolen owner session cannot keep ending a member's sessions.
 if (select count(*) from control.nexloop_workbench_reset_codes where tenant_id=p_tenant and issued_by_principal=p_principal
     and issued_at>clock_timestamp()-interval '15 minutes')>=10 then raise exception 'workbench member: rate_limited' using errcode='22023';end if;
 if body->>'target_principal'=p_principal then raise exception 'workbench member: target_is_requester' using errcode='22023';end if;
 -- The target's current role and status, rechecked under the lock (§1b, V1/Y5): an owner target is issue_owner_reset's.
 if control.nexloop_workbench_role(p_tenant,body->>'target_principal')='owner' then raise exception 'workbench member: target_role_changed' using errcode='22023';end if;
 if not control.nexloop_workbench_member_active(p_tenant,body->>'target_principal',false) then
  raise exception 'workbench member: target_not_active' using errcode='22023';end if;
 a:=control.nexloop_workbench_member_account(p_tenant,body->>'target_principal');
 if a.local_account_id is null or a.payload->>'status' is distinct from 'active' then raise exception 'workbench member: target_not_active' using errcode='22023';end if;
 target_subject:=a.subject_id;
 perform control.nexloop_workbench_reset_revoke(p_tenant,target_subject,'reissued');
 insert into control.nexloop_workbench_reset_codes(tenant_id,request_id,target_principal,target_subject,code_hmac,issued_by_principal,issued_by_subject,
   issued_at,expires_at,issuer_source,state)
  values(p_tenant,body->>'request_id',body->>'target_principal',target_subject,body->>'code_hmac',p_principal,p_subject,clock_timestamp(),
   clock_timestamp()+interval '30 minutes',body->>'issuer_source','issued') returning * into r;
 perform control.nexloop_workbench_kick(p_tenant,a);
 insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,payload,created_at)
  values('workbench-reset-issue:'||r.reset_id,'nexloop.workbench_member.issue_reset',p_tenant,p_principal,'workbench_member',p_intent,
   jsonb_build_object('reset_id',r.reset_id,'target_principal',r.target_principal,'expires_at',r.expires_at),clock_timestamp());
 return jsonb_build_object('reset_id',r.reset_id,'target_principal',r.target_principal,'expires_at',r.expires_at,'replayed',false);
end $$;

-- The completion's lookup (nexloop_identity): the target account's current hash and history (the caller checks reuse with them, as
-- the 0198 tool does), or null for any code that cannot complete now - one answer for every refusal.
create function control.nexloop_workbench_reset_lookup(p_tenant text,p_code_hmac text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare r control.nexloop_workbench_reset_codes;a control.nexloop_browser_accounts;
begin
 if session_user<>'nexloop_identity' or coalesce(p_code_hmac,'')!~'^[0-9a-f]{64}$' then raise exception 'identity unavailable' using errcode='42501';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into r from control.nexloop_workbench_reset_codes where tenant_id=p_tenant and code_hmac=p_code_hmac;
 if not found or r.state<>'issued' or r.expires_at<=clock_timestamp() then return null;end if;
 a:=control.nexloop_workbench_member_account(p_tenant,r.target_principal);
 if a.local_account_id is null then return null;end if;
 return jsonb_build_object('reset_id',r.reset_id,'account_id',a.local_account_id,'revision',(a.payload->>'revision')::bigint,
  'password_hash',a.password_hash,'password_history',to_jsonb(a.password_history));
end $$;

-- A new password the rules refused: counts against the code; the fifth voids it.
create function control.nexloop_workbench_reset_failed(p_tenant text,p_code_hmac text) returns void
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
begin
 if session_user<>'nexloop_identity' then raise exception 'identity unavailable' using errcode='42501';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 update control.nexloop_workbench_reset_codes set failures=failures+1,state=case when failures+1>=5 then 'voided' else state end
  where tenant_id=p_tenant and code_hmac=p_code_hmac and state='issued';
end $$;

create function control.nexloop_complete_workbench_reset(p_tenant text,p_code_hmac text,p_expected bigint,p_hash text,p_source text,p_trace text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare r control.nexloop_workbench_reset_codes;a control.nexloop_browser_accounts;now_at timestamptz;body jsonb;
begin
 if session_user<>'nexloop_identity' or coalesce(p_code_hmac,'')!~'^[0-9a-f]{64}$' or p_hash is null or length(p_hash)>1024
  or p_hash not like '$argon2id$%' or (p_source is not null and p_source!~'^[0-9a-f]{64}$') or p_trace is null or length(p_trace) not between 1 and 320 then
  raise exception 'identity unavailable' using errcode='42501';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 perform control.nexloop_workbench_member_lock(p_tenant);
 select * into r from control.nexloop_workbench_reset_codes where tenant_id=p_tenant and code_hmac=p_code_hmac for update;
 -- Every refusal is the same: no such code, not issued, expired, issuer no longer an active owner, target no longer an active
 -- non-owner member, or the account changed since the lookup.
 if not found or r.state<>'issued' or r.expires_at<=clock_timestamp()
  or not control.nexloop_workbench_member_active(p_tenant,r.issued_by_principal,true)
  or not control.nexloop_workbench_member_active(p_tenant,r.target_principal,false) then
  raise exception 'reset invalid' using errcode='22023';end if;
 select * into a from control.nexloop_browser_accounts where tenant_id=p_tenant and subject_id=r.target_subject for update;
 if not found or a.payload->>'status' is distinct from 'active' or (a.payload->>'revision')::bigint is distinct from p_expected then
  raise exception 'reset invalid' using errcode='22023';end if;
 now_at:=clock_timestamp();
 body:=control.nexloop_browser_password_write(p_tenant,a,p_hash,now_at);
 update control.nexloop_workbench_reset_codes set state='completed',completed_at=now_at,completer_source=p_source where reset_id=r.reset_id;
 insert into control.nexloop_browser_login_events(tenant_id,subject_id,event_type,outcome,operator_principal_id,request_id,trace_id,details,created_at)
  values(p_tenant,a.subject_id,'owner_issued_password_reset','reset',r.issued_by_principal,'workbench-reset:'||r.reset_id,p_trace,
   jsonb_build_object('reset_id',r.reset_id,'account_id',a.local_account_id,'account_revision',p_expected+1,'session_epoch',(body->>'session_epoch')::bigint,
    'same_source_as_issuer',r.issuer_source is not null and r.issuer_source=p_source),now_at);
 insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,payload,created_at)
  values('workbench-reset-complete:'||r.reset_id,'nexloop.workbench_member.reset_completed',p_tenant,r.target_principal,'workbench_member','workbench-reset:'||r.reset_id,
   jsonb_build_object('reset_id',r.reset_id,'issued_by',r.issued_by_principal,'target_principal',r.target_principal,
    'same_source_as_issuer',r.issuer_source is not null and r.issuer_source=p_source),now_at);
 return jsonb_build_object('reset_id',r.reset_id,'completed_at',now_at);
end $$;

-- Completed resets of a principal (nexloop_identity: the login notice for 7 days, the member's own page for 90 days).
create function control.nexloop_workbench_reset_notices(p_tenant text,p_principal text,p_days integer) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare out jsonb;
begin
 if session_user<>'nexloop_identity' or p_days not in (7,90) then raise exception 'identity unavailable' using errcode='42501';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 select coalesce(jsonb_agg(jsonb_build_object('reset_id',reset_id,'issued_by',issued_by_principal,'completed_at',completed_at) order by completed_at desc),'[]')
  into out from control.nexloop_workbench_reset_codes
  where tenant_id=p_tenant and target_principal=p_principal and state='completed' and completed_at>clock_timestamp()-make_interval(days=>p_days);
 return out;
end $$;

do $$ declare f text; begin
 foreach f in array array['control.nexloop_workbench_member_lock(text)','control.nexloop_workbench_member_account(text,text)',
   'control.nexloop_workbench_member_active(text,text,boolean)','control.nexloop_workbench_kick(text,control.nexloop_browser_accounts)',
   'control.nexloop_browser_password_write(text,control.nexloop_browser_accounts,text,timestamptz)','control.nexloop_workbench_reset_revoke(text,text,text)',
   'control.nexloop_governed_issue_workbench_reset(text,text,text,text,text,jsonb)','control.nexloop_workbench_reset_lookup(text,text)',
   'control.nexloop_workbench_reset_failed(text,text)','control.nexloop_complete_workbench_reset(text,text,bigint,text,text,text)',
   'control.nexloop_workbench_reset_notices(text,text,integer)'] loop
  execute format('alter function %s owner to nexloop_owner',f);
  execute format('revoke all on function %s from public',f);
 end loop;
end $$;
grant execute on function control.nexloop_workbench_reset_lookup(text,text),control.nexloop_workbench_reset_failed(text,text),
 control.nexloop_complete_workbench_reset(text,text,bigint,text,text,text),control.nexloop_workbench_reset_notices(text,text,integer) to nexloop_identity;
insert into control.nexloop_governed_capabilities(capability,operation,subject_rule,handler,source_task) values
 ('workbench_member.issue_reset','issue_workbench_reset','human','control.nexloop_governed_issue_workbench_reset(text,text,text,text,text,jsonb)','NX-058');
