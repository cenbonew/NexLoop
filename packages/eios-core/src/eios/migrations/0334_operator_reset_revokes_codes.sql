-- NX-058 slice B review I-2: the operator password reset (0198, the emergency channel of design §3) shares the password write of the
-- owner-issued reset codes (control.nexloop_browser_password_write, 0325: the same hash, history, revision, epoch and lockout effect) and
-- ends every unused reset code of the account (reason operator_reset), so a code issued before an emergency reset can no longer change
-- the password. Signature, grants and the rest of the 0198 body unchanged. The migration number is temporary; it lives only in the file
-- name and the catalog. Registered in tests/test_function_copy_guard.py.

create or replace function control.nexloop_operator_reset_browser_password(
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
 body:=control.nexloop_browser_password_write(p_tenant,old,p_hash,now_at);
 perform control.nexloop_workbench_reset_revoke(p_tenant,old.subject_id,'operator_reset');
 insert into control.nexloop_browser_login_events(tenant_id,subject_id,event_type,outcome,operator_principal_id,request_id,trace_id,details,created_at)
 values(p_tenant,old.subject_id,'operator_password_reset','reset',p_operator,p_request,p_trace,
  jsonb_build_object('account_id',p_account,'application_id',p_application,'account_revision',p_expected+1,
   'session_epoch',(body->>'session_epoch')::bigint,'operator_label',p_label),now_at);
 return body||jsonb_build_object('replayed',false);
end $$;
