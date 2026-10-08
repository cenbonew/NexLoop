create table control.nexloop_browser_sessions (
 session_id text primary key,session_token_digest bytea not null unique check(octet_length(session_token_digest)=32),
 csrf_token_digest bytea not null check(octet_length(csrf_token_digest)=32),tenant_id text not null,application_id text not null,payload jsonb not null
);
alter table control.nexloop_browser_sessions owner to nexloop_owner;
alter table control.nexloop_browser_sessions enable row level security;
alter table control.nexloop_browser_sessions force row level security;
create policy browser_session_tenant on control.nexloop_browser_sessions to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
revoke all on control.nexloop_browser_sessions from public;
create function control.nexloop_create_browser_session(p_tenant text,p_application text,p_operator text,p_evidence text,p_proof bytea,p_body jsonb,p_token bytea,p_csrf bytea)
 returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare evidence jsonb;member jsonb;now_at timestamptz;field_name text;
begin
 if session_user<>'nexloop_identity' or p_operator is distinct from session_user or jsonb_typeof(p_body) is distinct from 'object'
  or p_token is null or p_csrf is null or octet_length(p_token)<>32 or octet_length(p_csrf)<>32 or p_token=p_csrf
  or p_body->>'tenant_id' is distinct from p_tenant or p_body->>'application_id' is distinct from p_application then
  raise exception 'identity unavailable' using errcode='42501';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 -- Exclusive lock first: concurrent callers never upgrade a shared evidence lock.
 perform 1 from control.nexloop_browser_evidence e where e.evidence_id=p_evidence and e.tenant_id=p_tenant for update;
 evidence:=control.nexloop_browser_evidence(p_tenant,p_application,p_operator,'inspect',p_evidence,p_proof,null);
 if evidence is null then raise exception 'authentication rejected' using errcode='EID03';end if;
 select m.payload into member from control.nexloop_browser_memberships m where m.tenant_id=p_tenant and m.principal_id=p_body->>'principal_id' for share;
 now_at:=clock_timestamp();
 if not found or member->>'subject_id' is distinct from evidence->>'subject_id' or member->>'status' is distinct from 'active'
  or (member->>'valid_from')::timestamptz>now_at or (member->>'valid_until')::timestamptz<=now_at
  or p_body->'membership_revision' is distinct from member->'revision' or (evidence->>'expires_at')::timestamptz<=now_at then
  raise exception 'authentication rejected' using errcode='EID03';end if;
 foreach field_name in array array['subject_id','subject_kind','subject_revision','credential_kind','credential_tenant_id','credential_id','credential_revision','credential_session_epoch','provider_id','provider_revision','provider_configuration_fingerprint','authentication_methods','restricted','application_revision'] loop
  if p_body->field_name is distinct from evidence->field_name then raise exception 'authentication rejected' using errcode='EID03';end if;
 end loop;
 if p_body->>'session_id' is null or length(p_body->>'session_id') not between 1 and 320 or p_body->'revision' is distinct from '1'::jsonb
  or p_body->'revoked_at' is distinct from 'null'::jsonb or p_body->>'created_at' is null
  or p_body->>'last_seen_at' is distinct from p_body->>'created_at' or p_body->>'idle_expires_at' is null or p_body->>'absolute_expires_at' is null
  or (p_body->>'created_at')::timestamptz<(evidence->>'issued_at')::timestamptz or (p_body->>'created_at')::timestamptz>now_at
  or (p_body->>'idle_expires_at')::timestamptz<>(p_body->>'created_at')::timestamptz+interval '7 days'
  or (p_body->>'absolute_expires_at')::timestamptz<>(p_body->>'created_at')::timestamptz+interval '30 days' then
  raise exception 'authentication rejected' using errcode='EID03';end if;
 insert into control.nexloop_browser_sessions values(p_body->>'session_id',p_token,p_csrf,p_tenant,p_application,p_body);
 update control.nexloop_browser_evidence set consumed_at=now_at where evidence_id=p_evidence;
 return p_body;
end $$;
alter function control.nexloop_create_browser_session(text,text,text,text,bytea,jsonb,bytea,bytea) owner to nexloop_owner;
revoke all on function control.nexloop_create_browser_session(text,text,text,text,bytea,jsonb,bytea,bytea) from public;
grant execute on function control.nexloop_create_browser_session(text,text,text,text,bytea,jsonb,bytea,bytea) to nexloop_identity;
