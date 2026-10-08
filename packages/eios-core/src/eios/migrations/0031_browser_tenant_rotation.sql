-- Canonical read at one repeatable-read snapshot; mutations revalidate again.
create or replace function control.nexloop_read_browser_session(p_tenant text,p_application text,p_operator text,p_id text,p_digest bytea)
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
 perform set_config('eios.tenant_id',body->>'credential_tenant_id',true);
 select a.payload into account_body from control.nexloop_browser_accounts a where a.tenant_id=body->>'credential_tenant_id' and a.local_account_id=body->>'credential_id' and a.password_hash is not null;
 if not found or body->>'credential_kind' is distinct from 'local_account'
  or account_body->>'status' is distinct from 'active' or account_body->'subject_id' is distinct from body->'subject_id'
  or account_body->'revision' is distinct from body->'credential_revision' or account_body->'session_epoch' is distinct from body->'credential_session_epoch'
  or account_body->'must_change_password' is distinct from body->'restricted' or (account_body->>'locked_until')::timestamptz>now_at then return null;end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 select m.payload into member from control.nexloop_browser_memberships m where m.tenant_id=p_tenant and m.principal_id=body->>'principal_id';
 if not found or member->>'status' is distinct from 'active' or member->'subject_id' is distinct from body->'subject_id'
  or member->'revision' is distinct from body->'membership_revision' or member->>'valid_from' is null
  or (member->>'valid_from')::timestamptz>now_at or (member->>'valid_until')::timestamptz<=now_at then return null;end if;
 return body||jsonb_build_object('session_token_digest',encode(entry.session_token_digest,'hex'),'csrf_token_digest',encode(entry.csrf_token_digest,'hex'));
end $$;
alter function control.nexloop_read_browser_session(text,text,text,text,bytea) owner to nexloop_owner;
revoke all on function control.nexloop_read_browser_session(text,text,text,text,bytea) from public;
grant execute on function control.nexloop_read_browser_session(text,text,text,text,bytea) to nexloop_identity;

create or replace function control.nexloop_touch_browser_session(p_tenant text,p_application text,p_operator text,p_command jsonb,p_csrf bytea)
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
 perform set_config('eios.tenant_id',body->>'credential_tenant_id',true);
 perform 1 from control.nexloop_browser_accounts a where a.tenant_id=body->>'credential_tenant_id' and a.local_account_id=body->>'credential_id' for share;
 perform set_config('eios.tenant_id',p_tenant,true);
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

create or replace function control.nexloop_revoke_browser_session(p_tenant text,p_application text,p_operator text,p_command jsonb)
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
 perform set_config('eios.tenant_id',body->>'credential_tenant_id',true);
 perform 1 from control.nexloop_browser_accounts a where a.tenant_id=body->>'credential_tenant_id' and a.local_account_id=body->>'credential_id' for share;
 perform set_config('eios.tenant_id',p_tenant,true);
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
-- Bounded private discovery for a verified global Human in the source realm.
create policy browser_membership_identity_discovery on control.nexloop_browser_memberships to nexloop_owner
 using(subject_id=current_setting('eios.identity_discovery_subject',true));
create function control.nexloop_list_browser_memberships(p_tenant text,p_application text,p_operator text,p_subject text)
 returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare subject_body jsonb;result jsonb;previous text;
begin
 subject_body:=control.nexloop_read_browser_identity(p_tenant,p_application,p_operator,'subject',p_subject);
 if subject_body is null or subject_body->>'kind' is distinct from 'human' or subject_body->>'status' is distinct from 'active' then raise exception 'authentication rejected' using errcode='EID03';end if;
 previous:=current_setting('eios.identity_discovery_subject',true);
 perform set_config('eios.identity_discovery_subject',p_subject,true);
 select coalesce(jsonb_agg(m.payload),'[]'::jsonb) into result from
  (select payload from control.nexloop_browser_memberships where subject_id=p_subject order by tenant_id,principal_id limit 1001) m;
 perform set_config('eios.identity_discovery_subject',coalesce(previous,''),true);
 if jsonb_array_length(result)>1000 then raise exception 'identity unavailable' using errcode='42501';end if;
 return result;
end $$;
alter function control.nexloop_list_browser_memberships(text,text,text,text) owner to nexloop_owner;
revoke all on function control.nexloop_list_browser_memberships(text,text,text,text) from public;
grant execute on function control.nexloop_list_browser_memberships(text,text,text,text) to nexloop_identity;
create function control.nexloop_rotate_browser_tenant(p_tenant text,p_application text,p_operator text,p_command jsonb,p_source_token bytea,p_source_csrf bytea,p_token bytea,p_csrf bytea)
 returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare entry control.nexloop_browser_sessions;source_body jsonb;body jsonb;member jsonb;target text;app_revision integer;field_name text;now_at timestamptz;
begin
 if session_user<>'nexloop_identity' or p_operator is distinct from session_user
  or p_command->>'source_tenant_id' is distinct from p_tenant or p_command->>'source_application_id' is distinct from p_application
  or p_command->>'rotation_kind' is distinct from 'tenant_switch' or p_source_token is null or p_source_csrf is null
  or p_token is null or p_csrf is null or octet_length(p_token)<>32 or octet_length(p_csrf)<>32 or p_token=p_csrf
  then raise exception 'identity unavailable' using errcode='42501';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into entry from control.nexloop_browser_sessions s where s.tenant_id=p_tenant and s.application_id=p_application and s.session_id=p_command->>'source_session_id' for update;
 if not found or entry.session_token_digest is distinct from p_source_token or entry.csrf_token_digest is distinct from p_source_csrf
  or p_token=entry.session_token_digest or p_csrf=entry.csrf_token_digest then raise exception 'authentication rejected' using errcode='EID03';end if;
 source_body:=entry.payload;body:=p_command->'replacement_session';target:=body->>'tenant_id';
 if target is null or target=p_tenant or body->>'session_id' is null or body->>'session_id'=entry.session_id
  or source_body->'restricted' is distinct from 'false'::jsonb or body->'restricted' is distinct from 'false'::jsonb
  or body->'revision' is distinct from '1'::jsonb or body->'revoked_at' is distinct from 'null'::jsonb then raise exception 'authentication rejected' using errcode='EID03';end if;
 foreach field_name in array array['subject_id','subject_kind','subject_revision','principal_id','membership_revision','application_id','application_revision','credential_tenant_id','credential_kind','credential_id','credential_revision','credential_session_epoch','provider_id','provider_revision','provider_configuration_fingerprint','authentication_methods','restricted','absolute_expires_at'] loop
  if source_body->field_name is distinct from p_command->('source_'||field_name) then raise exception 'authentication rejected' using errcode='EID03';end if;
 end loop;
 foreach field_name in array array['subject_id','subject_kind','subject_revision','application_id','application_revision','credential_tenant_id','credential_kind','credential_id','credential_revision','credential_session_epoch','provider_id','provider_revision','provider_configuration_fingerprint','authentication_methods','restricted','absolute_expires_at'] loop
  if source_body->field_name is distinct from body->field_name then raise exception 'authentication rejected' using errcode='EID03';end if;
 end loop;
 -- Locks and canonical revalidation use the original credential tenant.
 perform control.nexloop_revoke_browser_session(p_tenant,p_application,p_operator,jsonb_build_object('session_id',entry.session_id,'expected_revision',p_command->'expected_source_revision','revoked_at',p_command->'revoked_at'));
 perform set_config('eios.tenant_id',target,true);
 select a.revision into app_revision from control.nexloop_browser_applications a where a.tenant_id=target and a.application_id=p_application and a.active for share;
 if not found or app_revision::text is distinct from body->>'application_revision' then raise exception 'authentication rejected' using errcode='EID03';end if;
 select m.payload into member from control.nexloop_browser_memberships m where m.tenant_id=target and m.principal_id=body->>'principal_id' for share;
 now_at:=clock_timestamp();
 if not found or member->>'subject_id' is distinct from body->>'subject_id' or member->>'status' is distinct from 'active'
  or member->'revision' is distinct from body->'membership_revision' or member->'revision' is distinct from p_command->'target_membership_revision'
  or member->>'valid_from' is null or (member->>'valid_from')::timestamptz>now_at or (member->>'valid_until')::timestamptz<=now_at
  or body->>'created_at' is distinct from p_command->>'revoked_at' or body->>'last_seen_at' is distinct from body->>'created_at'
  or body->>'created_at' is null or body->>'idle_expires_at' is null
  or (body->>'idle_expires_at')::timestamptz<>least((body->>'created_at')::timestamptz+interval '7 days',(body->>'absolute_expires_at')::timestamptz)
  then raise exception 'authentication rejected' using errcode='EID03';end if;
 insert into control.nexloop_browser_sessions values(body->>'session_id',p_token,p_csrf,target,p_application,body);
 return body;
end $$;
alter function control.nexloop_rotate_browser_tenant(text,text,text,jsonb,bytea,bytea,bytea,bytea) owner to nexloop_owner;
revoke all on function control.nexloop_rotate_browser_tenant(text,text,text,jsonb,bytea,bytea,bytea,bytea) from public;
grant execute on function control.nexloop_rotate_browser_tenant(text,text,text,jsonb,bytea,bytea,bytea,bytea) to nexloop_identity;
