create function control.nexloop_revoke_browser_session(p_tenant text,p_application text,p_operator text,p_command jsonb)
 returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare entry control.nexloop_browser_sessions;body jsonb;canonical jsonb;seen_at timestamptz;field_name text;
begin
 if session_user<>'nexloop_identity' or p_operator is distinct from session_user
  or jsonb_typeof(p_command) is distinct from 'object' or p_command->>'revoked_at' is null then raise exception 'identity unavailable' using errcode='42501';end if;
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
 seen_at:=(p_command->>'revoked_at')::timestamptz;
 if seen_at<(body->>'last_seen_at')::timestamptz or seen_at>clock_timestamp() or seen_at>=(body->>'idle_expires_at')::timestamptz
  or seen_at>=(body->>'absolute_expires_at')::timestamptz then raise exception 'authentication rejected' using errcode='EID03';end if;
 body:=body||jsonb_build_object('revoked_at',seen_at,'revision',(body->>'revision')::integer+1);
 update control.nexloop_browser_sessions s set payload=body where s.session_id=entry.session_id;
 return body||jsonb_build_object('session_token_digest',encode(entry.session_token_digest,'hex'),'csrf_token_digest',encode(entry.csrf_token_digest,'hex'));
end $$;
alter function control.nexloop_revoke_browser_session(text,text,text,jsonb) owner to nexloop_owner;
revoke all on function control.nexloop_revoke_browser_session(text,text,text,jsonb) from public;
grant execute on function control.nexloop_revoke_browser_session(text,text,text,jsonb) to nexloop_identity;
