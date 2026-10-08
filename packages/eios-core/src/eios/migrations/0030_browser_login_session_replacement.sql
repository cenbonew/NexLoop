-- Reauthentication replaces an existing session in the same Evidence/Session UoW.
create function control.nexloop_replace_browser_login_session(p_tenant text,p_application text,p_operator text,p_evidence text,p_proof bytea,p_body jsonb,p_token bytea,p_csrf bytea,p_source text,p_source_digest bytea,p_expected bigint)
 returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare source_row control.nexloop_browser_sessions;active_source jsonb;result jsonb;
begin
 if session_user<>'nexloop_identity' or p_operator is distinct from session_user or p_source is null
  or p_source_digest is null or octet_length(p_source_digest)<>32 or p_expected is null or p_expected<1
  or p_source is not distinct from p_body->>'session_id' then raise exception 'identity unavailable' using errcode='42501';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into source_row from control.nexloop_browser_sessions s where s.session_id=p_source and s.tenant_id=p_tenant and s.application_id=p_application for update;
 if not found or source_row.session_token_digest is distinct from p_source_digest
  or (source_row.payload->>'revision')::bigint<>p_expected then raise exception 'authentication rejected' using errcode='EID03';end if;
 -- Revoke revalidates/locks all current source identity rows, and its update
 -- remains uncommitted until the new Evidence-backed session also succeeds.
 active_source:=control.nexloop_revoke_browser_session(p_tenant,p_application,p_operator,
  jsonb_build_object('session_id',p_source,'expected_revision',p_expected,'revoked_at',p_body->'created_at'));
 result:=control.nexloop_create_browser_session(p_tenant,p_application,p_operator,p_evidence,p_proof,p_body,p_token,p_csrf);
 return result;
end $$;
alter function control.nexloop_replace_browser_login_session(text,text,text,text,bytea,jsonb,bytea,bytea,text,bytea,bigint) owner to nexloop_owner;
revoke all on function control.nexloop_replace_browser_login_session(text,text,text,text,bytea,jsonb,bytea,bytea,text,bytea,bigint) from public;
grant execute on function control.nexloop_replace_browser_login_session(text,text,text,text,bytea,jsonb,bytea,bytea,text,bytea,bigint) to nexloop_identity;
