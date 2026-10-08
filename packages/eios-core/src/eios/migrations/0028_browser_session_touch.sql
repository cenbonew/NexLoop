create function control.nexloop_touch_browser_session(p_tenant text,p_application text,p_operator text,p_command jsonb,p_csrf bytea)
 returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare entry control.nexloop_browser_sessions;body jsonb;canonical jsonb;seen_at timestamptz;field_name text;
begin
 if session_user<>'nexloop_identity' or p_operator is distinct from session_user
  or jsonb_typeof(p_command) is distinct from 'object' or p_command->>'tenant_id' is distinct from p_tenant
  or p_command->>'application_id' is distinct from p_application or p_command->>'seen_at' is null
  or (p_csrf is not null and octet_length(p_csrf)<>32) then raise exception 'identity unavailable' using errcode='42501';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into entry from control.nexloop_browser_sessions s where s.tenant_id=p_tenant and s.application_id=p_application and s.session_id=p_command->>'session_id' for update;
 if not found then raise exception 'authentication rejected' using errcode='EID03';end if;
 body:=entry.payload;
 -- Hold canonical authority rows through the write/commit.
 perform 1 from control.nexloop_browser_applications a where a.tenant_id=p_tenant and a.application_id=p_application for share;
 perform 1 from control.nexloop_browser_subjects s where s.subject_id=body->>'subject_id' for share;
 perform 1 from control.nexloop_browser_accounts a where a.tenant_id=p_tenant and a.local_account_id=body->>'credential_id' for share;
 perform 1 from control.nexloop_browser_memberships m where m.tenant_id=p_tenant and m.principal_id=body->>'principal_id' for share;
 canonical:=control.nexloop_read_browser_session(p_tenant,p_application,p_operator,entry.session_id,null);
 if canonical is null or body->'revision' is distinct from p_command->'expected_revision' then raise exception 'authentication rejected' using errcode='EID03';end if;
 foreach field_name in array array['subject_id','principal_id','application_revision','credential_tenant_id','membership_revision'] loop
  if body->field_name is distinct from p_command->field_name then raise exception 'authentication rejected' using errcode='EID03';end if;
 end loop;
 seen_at:=(p_command->>'seen_at')::timestamptz;
 if seen_at<(body->>'last_seen_at')::timestamptz or seen_at>clock_timestamp() or seen_at>=(body->>'idle_expires_at')::timestamptz
  or seen_at>=(body->>'absolute_expires_at')::timestamptz then raise exception 'authentication rejected' using errcode='EID03';end if;
 body:=body||jsonb_build_object('last_seen_at',seen_at,'idle_expires_at',least(seen_at+interval '7 days',(body->>'absolute_expires_at')::timestamptz));
 update control.nexloop_browser_sessions s set payload=body,csrf_token_digest=coalesce(p_csrf,entry.csrf_token_digest) where s.session_id=entry.session_id;
 return body||jsonb_build_object('session_token_digest',encode(entry.session_token_digest,'hex'),'csrf_token_digest',encode(coalesce(p_csrf,entry.csrf_token_digest),'hex'));
end $$;
alter function control.nexloop_touch_browser_session(text,text,text,jsonb,bytea) owner to nexloop_owner;
revoke all on function control.nexloop_touch_browser_session(text,text,text,jsonb,bytea) from public;
grant execute on function control.nexloop_touch_browser_session(text,text,text,jsonb,bytea) to nexloop_identity;
