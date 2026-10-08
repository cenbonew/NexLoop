-- Canonical read at one repeatable-read snapshot; mutations revalidate again.
create function control.nexloop_read_browser_session(p_tenant text,p_application text,p_operator text,p_id text,p_digest bytea)
 returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare entry control.nexloop_browser_sessions;body jsonb;subject_body jsonb;account_body jsonb;member jsonb;app_revision integer;now_at timestamptz;
begin
 if session_user<>'nexloop_identity' or p_operator is distinct from session_user
  or (p_id is null and p_digest is null) or (p_id is not null and p_digest is not null)
  or (p_digest is not null and octet_length(p_digest)<>32) or (p_id is not null and length(p_id) not between 1 and 320) then
  raise exception 'identity unavailable' using errcode='42501';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into entry from control.nexloop_browser_sessions s where s.tenant_id=p_tenant and s.application_id=p_application
  and ((p_id is not null and s.session_id=p_id) or (p_digest is not null and s.session_token_digest=p_digest));
 if not found then return null;end if;
 body:=entry.payload;now_at:=clock_timestamp();
 if body->'revoked_at' is distinct from 'null'::jsonb or (body->>'idle_expires_at')::timestamptz<=now_at or (body->>'absolute_expires_at')::timestamptz<=now_at then return null;end if;
 select a.revision into app_revision from control.nexloop_browser_applications a where a.tenant_id=p_tenant and a.application_id=p_application and a.active;
 if not found or app_revision::text is distinct from body->>'application_revision' then return null;end if;
 select s.payload into subject_body from control.nexloop_browser_subjects s where s.subject_id=body->>'subject_id';
 if not found or subject_body->>'kind' is distinct from 'human' or subject_body->>'status' is distinct from 'active'
  or subject_body->'revision' is distinct from body->'subject_revision' then return null;end if;
 select a.payload into account_body from control.nexloop_browser_accounts a where a.tenant_id=p_tenant and a.local_account_id=body->>'credential_id' and a.password_hash is not null;
 if not found or body->>'credential_kind' is distinct from 'local_account' or body->>'credential_tenant_id' is distinct from p_tenant
  or account_body->>'status' is distinct from 'active' or account_body->'subject_id' is distinct from body->'subject_id'
  or account_body->'revision' is distinct from body->'credential_revision' or account_body->'session_epoch' is distinct from body->'credential_session_epoch'
  or account_body->'must_change_password' is distinct from body->'restricted' or (account_body->>'locked_until')::timestamptz>now_at then return null;end if;
 select m.payload into member from control.nexloop_browser_memberships m where m.tenant_id=p_tenant and m.principal_id=body->>'principal_id';
 if not found or member->>'status' is distinct from 'active' or member->'subject_id' is distinct from body->'subject_id'
  or member->'revision' is distinct from body->'membership_revision' or member->>'valid_from' is null
  or (member->>'valid_from')::timestamptz>now_at or (member->>'valid_until')::timestamptz<=now_at then return null;end if;
 return body||jsonb_build_object('session_token_digest',encode(entry.session_token_digest,'hex'),'csrf_token_digest',encode(entry.csrf_token_digest,'hex'));
end $$;
alter function control.nexloop_read_browser_session(text,text,text,text,bytea) owner to nexloop_owner;
revoke all on function control.nexloop_read_browser_session(text,text,text,text,bytea) from public;
grant execute on function control.nexloop_read_browser_session(text,text,text,text,bytea) to nexloop_identity;
