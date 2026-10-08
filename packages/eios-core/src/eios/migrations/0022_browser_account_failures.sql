-- Authentication failure state follows frozen EIOS 0053 lockout semantics.
create table control.nexloop_browser_login_events (
 event_id uuid primary key default gen_random_uuid(),tenant_id text not null,subject_id text not null,
 event_type text not null,outcome text not null,operator_principal_id text not null,request_id text not null,trace_id text not null,
 details jsonb not null,created_at timestamptz not null
);
alter table control.nexloop_browser_login_events owner to nexloop_owner;
alter table control.nexloop_browser_login_events enable row level security;
alter table control.nexloop_browser_login_events force row level security;
create policy browser_login_event_tenant on control.nexloop_browser_login_events to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
revoke all on control.nexloop_browser_login_events from public;
create function control.nexloop_record_browser_account_failure(p_tenant text,p_application text,p_operator text,p_account text,p_failed_at timestamptz,p_request text,p_trace text)
 returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare account_row control.nexloop_browser_accounts;subject_key text;subject_body jsonb;now_at timestamptz;failures integer;level integer;locked_until timestamptz;body jsonb;
begin
 if session_user<>'nexloop_identity' or p_operator is distinct from session_user or p_failed_at is null
  or p_request is null or length(p_request) not between 1 and 320 or p_trace is null or length(p_trace) not between 1 and 320 then
  raise exception 'identity unavailable' using errcode='42501';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 perform 1 from control.nexloop_browser_applications a where a.tenant_id=p_tenant and a.application_id=p_application and a.active for share;
 if not found then raise exception 'identity unavailable' using errcode='42501';end if;
 select a.subject_id into subject_key from control.nexloop_browser_accounts a where a.tenant_id=p_tenant and a.local_account_id=p_account;
 if not found then raise exception 'identity unavailable' using errcode='42501';end if;
 select s.payload into subject_body from control.nexloop_browser_subjects s where s.subject_id=subject_key for share;
 if not found or subject_body->>'kind' is distinct from 'human' or subject_body->>'status' is distinct from 'active' then
  raise exception 'identity unavailable' using errcode='42501';end if;
 select * into account_row from control.nexloop_browser_accounts a where a.tenant_id=p_tenant and a.local_account_id=p_account for update;
 if not found or account_row.subject_id is distinct from subject_key or account_row.payload->>'status' is distinct from 'active' then
  raise exception 'identity unavailable' using errcode='42501';end if;
 now_at:=clock_timestamp();
 if p_failed_at>now_at or p_failed_at<(account_row.payload->>'created_at')::timestamptz then
  raise exception 'identity unavailable' using errcode='42501';end if;
 locked_until:=(account_row.payload->>'locked_until')::timestamptz;
 if locked_until is not null and now_at<locked_until then
  return account_row.payload || jsonb_build_object('password_hash',account_row.password_hash,'password_history',account_row.password_history);
 end if;
 failures:=(account_row.payload->>'failed_attempts')::integer+1;level:=(account_row.payload->>'lockout_level')::integer;locked_until:=null;
 if failures>=5 then
  failures:=0;level:=least(level+1,3);locked_until:=now_at+(case level when 1 then interval '15 minutes' when 2 then interval '30 minutes' else interval '60 minutes' end);
 end if;
 body:=account_row.payload || jsonb_build_object('failed_attempts',failures,'lockout_level',level,'locked_until',locked_until,
  'updated_at',greatest((account_row.payload->>'updated_at')::timestamptz,now_at),'revision',(account_row.payload->>'revision')::integer+1);
 update control.nexloop_browser_accounts a set payload=body where a.tenant_id=p_tenant and a.local_account_id=p_account;
 insert into control.nexloop_browser_login_events(tenant_id,subject_id,event_type,outcome,operator_principal_id,request_id,trace_id,details,created_at)
  values(p_tenant,subject_key,'local_authentication','failure',p_operator,p_request,p_trace,
   jsonb_build_object('failed_attempts',failures,'lockout_level',level),p_failed_at);
 return body || jsonb_build_object('password_hash',account_row.password_hash,'password_history',account_row.password_history);
end $$;
alter function control.nexloop_record_browser_account_failure(text,text,text,text,timestamptz,text,text) owner to nexloop_owner;
revoke all on function control.nexloop_record_browser_account_failure(text,text,text,text,timestamptz,text,text) from public;
grant execute on function control.nexloop_record_browser_account_failure(text,text,text,text,timestamptz,text,text) to nexloop_identity;
