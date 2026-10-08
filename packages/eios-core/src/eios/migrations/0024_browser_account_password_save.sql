-- Trusted authentication metadata only: existing-account rehash/failure reset.
-- This is not an account administration or business-data write API.
create function control.nexloop_save_browser_account_password(
 p_tenant text,p_application text,p_operator text,p_expected bigint,p_body jsonb,p_hash text,p_history text[],p_request text,p_trace text)
 returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare old control.nexloop_browser_accounts;subject_key text;subject_body jsonb;now_at timestamptz;body jsonb;
begin
 if session_user<>'nexloop_identity' or p_operator is distinct from session_user or p_expected is null
  or p_expected<1 or jsonb_typeof(p_body) is distinct from 'object' or p_body->>'tenant_id' is distinct from p_tenant
  or p_request is null or length(p_request) not between 1 and 320 or p_trace is null or length(p_trace) not between 1 and 320
  or p_hash is null or length(p_hash)>1024 or p_hash not like '$argon2id$%' then
  raise exception 'identity unavailable' using errcode='42501';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 perform 1 from control.nexloop_browser_applications a where a.tenant_id=p_tenant and a.application_id=p_application and a.active for share;
 if not found then raise exception 'identity unavailable' using errcode='42501';end if;
 select a.subject_id into subject_key from control.nexloop_browser_accounts a where a.tenant_id=p_tenant and a.local_account_id=p_body->>'local_account_id';
 select s.payload into subject_body from control.nexloop_browser_subjects s where s.subject_id=subject_key for share;
 if not found or subject_body->>'kind' is distinct from 'human' or subject_body->>'status' is distinct from 'active' then
  raise exception 'identity unavailable' using errcode='42501';end if;
 select * into old from control.nexloop_browser_accounts a where a.tenant_id=p_tenant and a.local_account_id=p_body->>'local_account_id' for update;
 now_at:=clock_timestamp();
 if not found or old.subject_id is distinct from subject_key or old.payload->>'status' is distinct from 'active'
  or (old.payload->>'revision')::bigint<>p_expected or (p_body->>'revision')::bigint<>p_expected
  or p_history is distinct from old.password_history
  or (old.payload->>'locked_until')::timestamptz>now_at
  or p_body->>'updated_at' is null
  or (p_body->>'updated_at')::timestamptz>now_at
  or (p_body->>'updated_at')::timestamptz<(old.payload->>'updated_at')::timestamptz
  or p_body->'failed_attempts' is distinct from '0'::jsonb
  or p_body->'lockout_level' is distinct from '0'::jsonb
  or p_body->'locked_until' is distinct from 'null'::jsonb
  or (p_body-array['updated_at','failed_attempts','lockout_level','locked_until'])
     is distinct from (old.payload-array['updated_at','failed_attempts','lockout_level','locked_until']) then
  raise exception 'identity unavailable' using errcode='42501';end if;
 body:=p_body||jsonb_build_object('revision',p_expected+1);
 update control.nexloop_browser_accounts a set payload=body,password_hash=p_hash where a.tenant_id=p_tenant and a.local_account_id=p_body->>'local_account_id';
 insert into control.nexloop_browser_login_events(tenant_id,subject_id,event_type,outcome,operator_principal_id,request_id,trace_id,details,created_at)
 values(p_tenant,subject_key,'local_password_state','saved',p_operator,p_request,p_trace,
  jsonb_build_object('account_id',p_body->>'local_account_id','account_revision',p_expected+1,'rehash',p_hash is distinct from old.password_hash),now_at);
 return body||jsonb_build_object('password_hash',p_hash,'password_history',p_history);
end $$;
alter function control.nexloop_save_browser_account_password(text,text,text,bigint,jsonb,text,text[],text,text) owner to nexloop_owner;
revoke all on function control.nexloop_save_browser_account_password(text,text,text,bigint,jsonb,text,text[],text,text) from public;
grant execute on function control.nexloop_save_browser_account_password(text,text,text,bigint,jsonb,text,text[],text,text) to nexloop_identity;
