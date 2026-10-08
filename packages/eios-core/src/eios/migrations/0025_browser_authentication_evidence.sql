-- Private globally unique evidence; proof itself is never persisted.
create table control.nexloop_browser_evidence (
 evidence_id text primary key,proof_digest bytea not null unique check(octet_length(proof_digest)=32),
 tenant_id text not null,application_id text not null,facts jsonb not null,
 issued_at timestamptz not null,expires_at timestamptz not null,consumed_at timestamptz,
 check(expires_at=issued_at+interval '5 minutes')
);
alter table control.nexloop_browser_evidence owner to nexloop_owner;
alter table control.nexloop_browser_evidence enable row level security;
alter table control.nexloop_browser_evidence force row level security;
create policy browser_evidence_tenant on control.nexloop_browser_evidence to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
revoke all on control.nexloop_browser_evidence from public;
create function control.nexloop_browser_evidence(p_tenant text,p_application text,p_operator text,p_mode text,p_id text,p_digest bytea,p_facts jsonb)
 returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare app_revision integer;account_row control.nexloop_browser_accounts;subject_body jsonb;canonical jsonb;entry control.nexloop_browser_evidence;now_at timestamptz;
begin
 if session_user<>'nexloop_identity' or p_operator is distinct from session_user or p_mode not in ('issue','inspect') or p_mode is null
  or p_id is null or length(p_id) not between 1 and 320 or p_digest is null or octet_length(p_digest)<>32 then
  raise exception 'identity unavailable' using errcode='42501';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 select a.revision into app_revision from control.nexloop_browser_applications a where a.tenant_id=p_tenant and a.application_id=p_application and a.active for share;
 if not found then raise exception 'authentication rejected' using errcode='EID03';end if;
 if p_mode='inspect' then
  select * into entry from control.nexloop_browser_evidence e where e.evidence_id=p_id and e.tenant_id=p_tenant and e.application_id=p_application and e.proof_digest=p_digest for share;
  if not found or entry.consumed_at is not null or entry.expires_at<=clock_timestamp() then return null;end if;
  p_facts:=entry.facts;
 end if;
 select s.payload into subject_body from control.nexloop_browser_subjects s where s.subject_id=p_facts->>'subject_id' for share;
 if not found or subject_body->>'kind' is distinct from 'human' or subject_body->>'status' is distinct from 'active' then
  if p_mode='inspect' then return null;end if;
  raise exception 'authentication rejected' using errcode='EID03';end if;
 select * into account_row from control.nexloop_browser_accounts a where a.tenant_id=p_tenant and a.local_account_id=p_facts->>'credential_id' for share;
 now_at:=clock_timestamp();
 if p_mode='inspect' and entry.expires_at<=now_at then return null;end if;
 if not found or account_row.subject_id is distinct from subject_body->>'subject_id' or account_row.password_hash is null
  or account_row.payload->>'status' is distinct from 'active' or (account_row.payload->>'locked_until')::timestamptz>now_at then
  if p_mode='inspect' then return null;end if;
  raise exception 'authentication rejected' using errcode='EID03';end if;
 canonical:=jsonb_build_object('tenant_id',p_tenant,'subject_id',account_row.subject_id,'subject_kind','human',
  'subject_revision',(subject_body->>'revision')::integer,'subject_status','active',
  'credential_kind','local_account','credential_tenant_id',p_tenant,'credential_id',account_row.local_account_id,
  'credential_revision',(account_row.payload->>'revision')::integer,'credential_session_epoch',(account_row.payload->>'session_epoch')::integer,
  'application_id',p_application,'application_revision',app_revision,'authentication_methods',jsonb_build_array('password'),
  'purpose','session.create','restricted',(account_row.payload->>'must_change_password')::boolean,
  'authentication_issued_at',null,'authentication_not_before',null,'authentication_expires_at',null,
  'provider_id',null,'provider_revision',null,'provider_configuration_fingerprint',null);
 if p_facts is distinct from canonical then
  if p_mode='inspect' then return null;end if;
  raise exception 'authentication rejected' using errcode='EID03';end if;
 if p_mode='issue' then
  insert into control.nexloop_browser_evidence(evidence_id,proof_digest,tenant_id,application_id,facts,issued_at,expires_at)
   values(p_id,p_digest,p_tenant,p_application,canonical,now_at,now_at+interval '5 minutes') returning * into entry;
 end if;
 return entry.facts||jsonb_build_object('evidence_id',entry.evidence_id,'issued_at',entry.issued_at,'expires_at',entry.expires_at);
end $$;
alter function control.nexloop_browser_evidence(text,text,text,text,text,bytea,jsonb) owner to nexloop_owner;
revoke all on function control.nexloop_browser_evidence(text,text,text,text,text,bytea,jsonb) from public;
grant execute on function control.nexloop_browser_evidence(text,text,text,text,text,bytea,jsonb) to nexloop_identity;
