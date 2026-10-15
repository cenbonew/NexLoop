-- G-PW-RESET (owner-approved, 2026-10-15; TEMPORARY migration number, renumbered by the dispatcher at merge): an identity
-- operator resets a local browser account's password. On the stage the six simulated WebChat customers were created with
-- mistyped passwords nobody knows, and their addresses are .invalid, so the e-mail reset flow cannot reach them.
--
-- 0024 control.nexloop_save_browser_account_password cannot do this: it keeps the password history and the session epoch
-- unchanged and refuses a locked account (it is the rehash / failure-reset path). This function is the operator reset:
--   * caller: session_user nexloop_identity only, the operator id equal to it (the same boundary as 0024 and the initial identity
--     operator 0162); granted to nexloop_identity only, owned by nexloop_owner; no other role can execute it;
--   * target: an active local account of an active human subject of the tenant, on an active browser application; the account's
--     current revision must equal p_expected (optimistic lock);
--   * effect: the new argon2id hash; the previous hash first in password_history (at most 10, LocalAccount); session_epoch + 1, so
--     every browser session of the account fails its next read (0027/0045 compare the epoch); failed_attempts, lockout_level and
--     locked_until cleared; updated_at now; revision + 1. Status, username, e-mail, membership and the allowance quota unchanged;
--   * evidence: one control.nexloop_browser_login_events row 'operator_password_reset' with the operator label, request id, account,
--     new revision and epoch; never a password or hash;
--   * replay: a request id already recorded for this account returns the current account (replayed true) and changes nothing; the
--     same request id for another account is refused (22023). The caller checks password reuse against the stored hashes (it
--     holds the password, SQL never does); when the new password equals the current one it calls with p_replay_only true, which
--     succeeds only as a replay (otherwise 22023 'password reuse').
create function control.nexloop_operator_reset_browser_password(
 p_tenant text,p_application text,p_operator text,p_account text,p_expected bigint,p_hash text,p_request text,p_trace text,p_label text,p_replay_only boolean)
 returns jsonb language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare old control.nexloop_browser_accounts;subject_body jsonb;prior jsonb;now_at timestamptz;body jsonb;history text[];
begin
 if session_user<>'nexloop_identity' or p_operator is distinct from session_user or p_expected is null or p_expected<1
  or p_account is null or length(p_account) not between 1 and 320 or p_request is null or length(p_request) not between 1 and 320
  or p_trace is null or length(p_trace) not between 1 and 320 or p_label is null or length(p_label) not between 1 and 320
  or p_hash is null or length(p_hash)>1024 or p_hash not like '$argon2id$%' then
  raise exception 'identity unavailable' using errcode='42501';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 perform 1 from control.nexloop_browser_applications a where a.tenant_id=p_tenant and a.application_id=p_application and a.active for share;
 if not found then raise exception 'identity unavailable' using errcode='42501';end if;
 select e.details into prior from control.nexloop_browser_login_events e
  where e.tenant_id=p_tenant and e.event_type='operator_password_reset' and e.request_id=p_request;
 if found then
  if prior->>'account_id' is distinct from p_account then raise exception 'password reset request conflict' using errcode='22023';end if;
  select * into old from control.nexloop_browser_accounts a where a.tenant_id=p_tenant and a.local_account_id=p_account;
  return old.payload||jsonb_build_object('replayed',true);
 end if;
 -- The caller found that the new password equals the current one: only a replay of the request that set it is acceptable.
 if p_replay_only is distinct from false then raise exception 'password reuse' using errcode='22023';end if;
 select * into old from control.nexloop_browser_accounts a where a.tenant_id=p_tenant and a.local_account_id=p_account for update;
 if not found then raise exception 'identity unavailable' using errcode='42501';end if;
 select s.payload into subject_body from control.nexloop_browser_subjects s where s.subject_id=old.subject_id for share;
 if not found or subject_body->>'kind' is distinct from 'human' or subject_body->>'status' is distinct from 'active'
  or old.payload->>'status' is distinct from 'active' or (old.payload->>'revision')::bigint<>p_expected then
  raise exception 'identity unavailable' using errcode='42501';end if;
 now_at:=clock_timestamp();
 history:=(case when old.password_hash is null then old.password_history else array[old.password_hash]||old.password_history end)[1:10];
 body:=old.payload||jsonb_build_object('revision',p_expected+1,'session_epoch',(old.payload->>'session_epoch')::bigint+1,'failed_attempts',0,
  'lockout_level',0,'locked_until',null,'updated_at',to_jsonb(now_at));
 update control.nexloop_browser_accounts a set payload=body,password_hash=p_hash,password_history=history
  where a.tenant_id=p_tenant and a.local_account_id=p_account;
 insert into control.nexloop_browser_login_events(tenant_id,subject_id,event_type,outcome,operator_principal_id,request_id,trace_id,details,created_at)
 values(p_tenant,old.subject_id,'operator_password_reset','reset',p_operator,p_request,p_trace,
  jsonb_build_object('account_id',p_account,'application_id',p_application,'account_revision',p_expected+1,
   'session_epoch',(body->>'session_epoch')::bigint,'operator_label',p_label),now_at);
 return body||jsonb_build_object('replayed',false);
end $$;
alter function control.nexloop_operator_reset_browser_password(text,text,text,text,bigint,text,text,text,text,boolean) owner to nexloop_owner;
revoke all on function control.nexloop_operator_reset_browser_password(text,text,text,text,bigint,text,text,text,text,boolean) from public;
grant execute on function control.nexloop_operator_reset_browser_password(text,text,text,text,bigint,text,text,text,text,boolean) to nexloop_identity;
