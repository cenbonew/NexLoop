-- NX-058 slice A (docs/design/account-self-service.md v7 §1, §1c; owner Q-D = C 2026-10-23; ADR-020 §3 amendment): owner invitations of
-- operators and reviewers, and the role-template instantiation every governed member change uses. The migration number is temporary; it
-- lives only in the file name and the catalog.
--
-- * Role templates: the configurator (session nexloop_configurator only, the trusted configuration path of workbench_roles.apply) publishes
--   per workbench role the Human authority facts compile_member writes, for a placeholder principal and subject, and the revoke form
--   (revoke_member's empty grant sets). Append-only, versioned, digested. "What a role grants" is written only there. A new version
--   rewrites, in the same publication, every member who holds the role through an instantiation (design §1c, AA4).
-- * Instantiation (internal, granted to no role): the template's placeholders become the member's principal and subject; every digest
--   that depends on them is recomputed in the canonical form of eios.authz.facts (each grant's digest and each changed fact's
--   snapshot_digest; facts that do not mention the member - resource graph, policies - are the template's own bytes); the facts are
--   written as 0050 writes them; an append-only instance record (template version and digest, facts digest, source) is kept. The golden
--   test (tests/test_workbench_invitations_pg.py) compares the result byte for byte with compile_member / revoke_member.
-- * Invitations: governed owner Actions nexloop.workbench_member.invite (operator or reviewer; owners: invite_owner, slice C, with
--   approval) and nexloop.workbench_member.revoke_invitation. The token is drawn by the workbench entry; only its HMAC is stored. Nothing
--   is written to the identity directory until acceptance.
-- * Acceptance (nexloop_identity, the unauthenticated workbench endpoint): under the tenant's member lock, the invitation (issued,
--   unexpired, its issuer still an active owner), then the Human (subject, membership, account with the password hash the caller drew,
--   shapes checked as 0050's initial identity checks them), one more workbench configuration version (recorded_by invitation:<id>; the
--   configurator's own path is unchanged), the role's grants instantiated, the role history started, a login event and an audit event.
set local timezone = 'UTC';

create table control.nexloop_workbench_role_templates (
 tenant_id text not null references control.nexloop_tenants(tenant_id),role text not null check(role in ('owner','operator','reviewer')),
 version integer not null check(version>=1),roles_manifest_version integer not null check(roles_manifest_version>=1),
 grant_facts jsonb not null check(jsonb_typeof(grant_facts)='array'),revoke_facts jsonb not null check(jsonb_typeof(revoke_facts)='array'),
 digest text not null check(digest~'^[0-9a-f]{64}$'),published_by text not null,published_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,role,version)
);
create table control.nexloop_workbench_grant_instances (
 tenant_id text not null references control.nexloop_tenants(tenant_id),instance_id bigint generated always as identity primary key,
 principal_id text not null,subject_id text not null,role text not null check(role in ('owner','operator','reviewer')),
 direction text not null check(direction in ('grant','revoke')),template_version integer not null,template_digest text not null,
 facts_digest text not null check(facts_digest~'^[0-9a-f]{64}$'),source text not null check(source~'^(invitation|action|template):[A-Za-z0-9._:-]{1,200}$'),
 recorded_at timestamptz not null default clock_timestamp()
);
create index nexloop_workbench_grant_instances_principal on control.nexloop_workbench_grant_instances(tenant_id,principal_id,instance_id desc);
-- Design §7 (X2, Z4): when a subject started and stopped holding a role; the vote-eligibility source. world: the workbench's (real).
create table control.nexloop_workbench_role_history (
 tenant_id text not null references control.nexloop_tenants(tenant_id),entry_id bigint generated always as identity primary key,
 world text not null default 'real' check(world='real'),subject_id text not null,principal_id text not null,
 role text not null check(role in ('owner','operator','reviewer')),event text not null check(event in ('start','stop')),
 source text not null,recorded_at timestamptz not null default clock_timestamp()
);
create index nexloop_workbench_role_history_subject on control.nexloop_workbench_role_history(tenant_id,subject_id,entry_id desc);
create table control.nexloop_workbench_invitations (
 tenant_id text not null references control.nexloop_tenants(tenant_id),invitation_id uuid primary key default gen_random_uuid(),
 request_id text not null check(char_length(request_id) between 1 and 200),
 username text not null check(username~'^[a-z0-9][a-z0-9._-]{2,63}$'),role text not null check(role in ('owner','operator','reviewer')),
 token_hmac text not null unique check(token_hmac~'^[0-9a-f]{64}$'),
 issued_by_principal text not null,issued_by_subject text not null,issued_at timestamptz not null,expires_at timestamptz not null,
 issuer_source text check(issuer_source is null or issuer_source~'^[0-9a-f]{64}$'),
 state text not null check(state in ('issued','accepted','revoked','voided')),failures integer not null default 0 check(failures between 0 and 5),
 accepting_subject text,accepted_principal text,accepted_at timestamptz,acceptor_source text check(acceptor_source is null or acceptor_source~'^[0-9a-f]{64}$'),
 revoked_reason text check(revoked_reason is null or revoked_reason in ('reissued','owner_revoked','operator_reset','member_changed')),
 unique(tenant_id,request_id),check(expires_at>issued_at)
);
create index nexloop_workbench_invitations_username on control.nexloop_workbench_invitations(tenant_id,username,state);
do $$ declare t text; begin
 foreach t in array array['control.nexloop_workbench_role_templates','control.nexloop_workbench_grant_instances','control.nexloop_workbench_role_history',
   'control.nexloop_workbench_invitations'] loop
  execute format('alter table %s owner to nexloop_owner',t);
  execute format('alter table %s enable row level security',t);
  execute format('alter table %s force row level security',t);
  execute format('create policy tenant_boundary on %s to nexloop_owner using(tenant_id=current_setting(''eios.tenant_id'',true)) with check(tenant_id=current_setting(''eios.tenant_id'',true))',t);
  execute format('revoke all on %s from public',t);
 end loop;
end $$;
-- As 0050's initial identity: a canonical Human subject is inserted only for the invitation this transaction is accepting (its
-- accepting_subject), never by an unscoped owner insert.
create policy workbench_invitation_subject_insert on control.nexloop_browser_subjects
 for insert to nexloop_owner with check(exists(
  select 1 from control.nexloop_workbench_invitations i
   where i.tenant_id=current_setting('eios.tenant_id',true) and i.state='issued' and i.accepting_subject=nexloop_browser_subjects.subject_id));
create trigger nx058_append_only before update or delete on control.nexloop_workbench_role_templates for each row execute function control.nexloop_nx022_append_only();
create trigger nx058_append_only before update or delete on control.nexloop_workbench_grant_instances for each row execute function control.nexloop_nx022_append_only();
create trigger nx058_append_only before update or delete on control.nexloop_workbench_role_history for each row execute function control.nexloop_nx022_append_only();

-- The canonical digest of eios.authz.facts (_canonical_digest over a JSON value: sorted keys, compact, UTF-8).
create function control.nexloop_authority_digest(p_value jsonb) returns text language sql immutable
 set search_path=pg_catalog,pg_temp as $$ select encode(sha256(convert_to(runtime.nexloop_canonical_json(p_value),'UTF8')),'hex') $$;

-- One template entry with the member in place of the placeholders and every member-dependent digest recomputed.
create function control.nexloop_workbench_fact_instance(p_fact jsonb,p_principal text,p_subject text) returns jsonb
 language plpgsql immutable set search_path=pg_catalog,pg_temp as $$
declare v jsonb;p jsonb;g jsonb;grants jsonb:='[]';
begin
 v:=replace(replace(p_fact::text,'nexloop-template-principal',p_principal),'nexloop-template-subject',p_subject)::jsonb;
 -- A payload that does not mention the member (resource graph, policies: only their key does): the template's own bytes, snapshot included.
 if v->'payload'=p_fact->'payload' then return v;end if;
 p:=v->'payload';
 if v->>'kind'='grants' then
  for g in select value from jsonb_array_elements(p->'grants') loop
   grants:=grants||jsonb_build_array(g||jsonb_build_object('digest',control.nexloop_authority_digest(jsonb_build_object(
    'grant',g->'resource'->>'resource_id','operations',jsonb_build_array('execute'),'principal',p_principal))));
  end loop;
  p:=p||jsonb_build_object('grants',grants);
 end if;
 if p ? 'snapshot_digest' then p:=p||jsonb_build_object('snapshot_digest',control.nexloop_authority_digest(p-'snapshot_digest'-'repository_witness'));end if;
 return v||jsonb_build_object('payload',p);
end $$;

-- Write a role's grant or revoke form for one member (internal; granted to no role). Returns the facts digest.
create function control.nexloop_workbench_grants_write(p_tenant text,p_principal text,p_subject text,p_role text,p_direction text,p_source text) returns text
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare t control.nexloop_workbench_role_templates;f jsonb;facts jsonb:='[]';v_key text[];inst jsonb;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into t from control.nexloop_workbench_role_templates where tenant_id=p_tenant and role=p_role order by version desc limit 1;
 if not found then raise exception 'workbench member: role_template_missing' using errcode='22023';end if;
 for f in select value from jsonb_array_elements(case p_direction when 'grant' then t.grant_facts else t.revoke_facts end) loop
  inst:=control.nexloop_workbench_fact_instance(f,p_principal,p_subject);
  select array_agg(value order by ordinal) into v_key from jsonb_array_elements_text(inst->'key') with ordinality as x(value,ordinal);
  insert into authz.nexloop_authority_facts values(p_tenant,inst->>'kind',v_key,inst->'payload') on conflict(tenant_id,fact_kind,entity_key)
   do update set payload=excluded.payload where nexloop_authority_facts.payload is distinct from excluded.payload;
  facts:=facts||jsonb_build_array(inst);
 end loop;
 insert into control.nexloop_workbench_grant_instances(tenant_id,principal_id,subject_id,role,direction,template_version,template_digest,facts_digest,source)
  values(p_tenant,p_principal,p_subject,p_role,p_direction,t.version,t.digest,control.nexloop_authority_digest(facts),p_source);
 return control.nexloop_authority_digest(facts);
end $$;

-- The configurator publishes the role templates (workbench_roles.apply, after the member configuration). A changed role gets a new version
-- and every member holding it through an instantiation is rewritten from it in the same transaction (AA4).
create function control.nexloop_publish_workbench_role_templates(p_tenant text,p_templates jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare r text;e jsonb;t control.nexloop_workbench_role_templates;v_digest text;f jsonb;changed jsonb:='[]';i record;prior text:=current_setting('eios.tenant_id',true);
begin
 if session_user<>'nexloop_configurator' then raise exception 'workbench role templates are trusted configuration only' using errcode='42501';end if;
 if jsonb_typeof(p_templates) is distinct from 'object' or (select array_agg(k order by k) from jsonb_object_keys(p_templates) k) is distinct from array['operator','owner','reviewer'] then
  raise exception 'workbench role templates invalid' using errcode='22023';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 perform control.nexloop_workbench_member_lock(p_tenant);
 foreach r in array array['operator','owner','reviewer'] loop
  e:=p_templates->r;
  if jsonb_typeof(e) is distinct from 'object' or (select array_agg(k order by k) from jsonb_object_keys(e) k) is distinct from array['grant_facts','revoke_facts','roles_manifest_version']
   or jsonb_typeof(e->'grant_facts') is distinct from 'array' or jsonb_typeof(e->'revoke_facts') is distinct from 'array' or jsonb_typeof(e->'roles_manifest_version') is distinct from 'number' then
   raise exception 'workbench role templates invalid' using errcode='22023';end if;
  for f in select value from jsonb_array_elements((e->'grant_facts')||(e->'revoke_facts')) loop
   if f->>'kind' is null or f->>'kind' not in ('actor','subject_authority','resource_graph','grants','scope','controls','policies')
    or f->'payload'->>'tenant_id' is distinct from p_tenant or jsonb_typeof(f->'key') is distinct from 'array' then
    raise exception 'workbench role templates invalid' using errcode='22023';end if;
  end loop;
  if exists(select 1 from jsonb_array_elements(e->'revoke_facts') x where x->>'kind'<>'grants') then raise exception 'workbench role templates invalid' using errcode='22023';end if;
  v_digest:=control.nexloop_authority_digest(e);
  select * into t from control.nexloop_workbench_role_templates where tenant_id=p_tenant and role=r order by version desc limit 1;
  if found and t.digest=v_digest then continue;end if;
  insert into control.nexloop_workbench_role_templates(tenant_id,role,version,roles_manifest_version,grant_facts,revoke_facts,digest,published_by)
   values(p_tenant,r,coalesce(t.version,0)+1,(e->>'roles_manifest_version')::integer,e->'grant_facts',e->'revoke_facts',v_digest,session_user) returning * into t;
  changed:=changed||jsonb_build_array(jsonb_build_object('role',r,'version',t.version));
  -- AA4: every member whose latest instance of this role is a grant, and who still holds the role, is rewritten from the new version.
  for i in select distinct on (g.principal_id) g.principal_id,g.subject_id,g.direction from control.nexloop_workbench_grant_instances g
    where g.tenant_id=p_tenant and g.role=r order by g.principal_id,g.instance_id desc loop
   if i.direction='grant' and control.nexloop_workbench_role(p_tenant,i.principal_id)=r then
    perform control.nexloop_workbench_grants_write(p_tenant,i.principal_id,i.subject_id,r,'grant','template:'||t.version);
   end if;
  end loop;
 end loop;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return jsonb_build_object('published',changed);
end $$;

-- One more workbench configuration version from a governed member change (invitation:<id> or action:<intent>): the latest version's
-- roles and application, the member map as given. The configurator's own control.nexloop_configure_workbench is unchanged.
create function control.nexloop_workbench_configuration_append(p_tenant text,p_members jsonb,p_source text) returns integer
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare cur control.nexloop_workbench_configurations;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into cur from control.nexloop_workbench_configurations where tenant_id=p_tenant order by manifest_version desc limit 1;
 if not found then raise exception 'workbench member: not_configured' using errcode='22023';end if;
 if exists(select 1 from jsonb_each_text(p_members) m where not (cur.roles ? m.value)) then raise exception 'workbench member: invalid' using errcode='22023';end if;
 insert into control.nexloop_workbench_configurations(tenant_id,manifest_version,roles_version,application_id,roles,members,roles_digest,recorded_by)
  values(p_tenant,cur.manifest_version+1,cur.roles_version,cur.application_id,cur.roles,p_members,cur.roles_digest,p_source);
 return cur.manifest_version+1;
end $$;

create function control.nexloop_governed_invite_workbench_member(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare r control.nexloop_workbench_invitations;v_hours integer;
begin
 if jsonb_typeof(body) is distinct from 'object' or exists(select 1 from jsonb_object_keys(body) k where k not in ('operation','request_id','username','role','token_hmac','issuer_source','expires_in_hours'))
  or jsonb_typeof(body->'request_id') is distinct from 'string' or char_length(body->>'request_id') not between 1 and 200
  or coalesce(body->>'username','')!~'^[a-z0-9][a-z0-9._-]{2,63}$' or coalesce(body->>'token_hmac','')!~'^[0-9a-f]{64}$'
  or coalesce(body->>'issuer_source','')!~'^[0-9a-f]{64}$' or jsonb_typeof(body->'expires_in_hours') is distinct from 'number'
  or (body->>'expires_in_hours')!~'^[0-9]{1,2}$' or (body->>'expires_in_hours')::integer not between 1 and 72 then
  raise exception 'workbench member: invalid' using errcode='22023';end if;
 -- invite is for operators and reviewers; an owner is invited by invite_owner, with approval (slice C, design §1b).
 if body->>'role' is null or body->>'role' not in ('operator','reviewer') then raise exception 'workbench member: role_not_allowed' using errcode='22023';end if;
 if p_world is distinct from 'real' then raise exception 'workbench member: owner_world_only' using errcode='22023';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 perform control.nexloop_workbench_member_lock(p_tenant);
 select * into r from control.nexloop_workbench_invitations where tenant_id=p_tenant and request_id=body->>'request_id';
 if found then
  if (r.username,r.role,r.token_hmac) is distinct from (body->>'username',body->>'role',body->>'token_hmac') then raise exception 'workbench member: request_reused' using errcode='22023';end if;
  return jsonb_build_object('invitation_id',r.invitation_id,'username',r.username,'role',r.role,'expires_at',r.expires_at,'replayed',true);
 end if;
 if not control.nexloop_workbench_member_active(p_tenant,p_principal,true) then raise exception 'workbench member: issuer_not_owner' using errcode='22023';end if;
 if (select count(*) from control.nexloop_workbench_invitations where tenant_id=p_tenant and issued_by_principal=p_principal
     and issued_at>clock_timestamp()-interval '15 minutes')>=10 then raise exception 'workbench member: rate_limited' using errcode='22023';end if;
 if exists(select 1 from control.nexloop_browser_accounts a where a.tenant_id=p_tenant and lower(a.username)=body->>'username') then
  raise exception 'workbench member: username_taken' using errcode='22023';end if;
 update control.nexloop_workbench_invitations set state='revoked',revoked_reason='reissued' where tenant_id=p_tenant and username=body->>'username' and state='issued';
 v_hours:=(body->>'expires_in_hours')::integer;
 insert into control.nexloop_workbench_invitations(tenant_id,request_id,username,role,token_hmac,issued_by_principal,issued_by_subject,issued_at,expires_at,issuer_source,state)
  values(p_tenant,body->>'request_id',body->>'username',body->>'role',body->>'token_hmac',p_principal,p_subject,clock_timestamp(),
   clock_timestamp()+make_interval(hours=>v_hours),body->>'issuer_source','issued') returning * into r;
 insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,payload,created_at)
  values('workbench-invite:'||r.invitation_id,'nexloop.workbench_member.invite',p_tenant,p_principal,'workbench_member',p_intent,
   jsonb_build_object('invitation_id',r.invitation_id,'role',r.role,'expires_at',r.expires_at),clock_timestamp());
 return jsonb_build_object('invitation_id',r.invitation_id,'username',r.username,'role',r.role,'expires_at',r.expires_at,'replayed',false);
end $$;

create function control.nexloop_governed_revoke_workbench_invitation(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare r control.nexloop_workbench_invitations;
begin
 if jsonb_typeof(body) is distinct from 'object' or exists(select 1 from jsonb_object_keys(body) k where k not in ('operation','request_id','invitation_id'))
  or jsonb_typeof(body->'request_id') is distinct from 'string' or char_length(body->>'request_id') not between 1 and 200
  or coalesce(body->>'invitation_id','')!~'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' then
  raise exception 'workbench member: invalid' using errcode='22023';end if;
 if p_world is distinct from 'real' then raise exception 'workbench member: owner_world_only' using errcode='22023';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 perform control.nexloop_workbench_member_lock(p_tenant);
 select * into r from control.nexloop_workbench_invitations where tenant_id=p_tenant and invitation_id=(body->>'invitation_id')::uuid for update;
 if not found then raise exception 'workbench member: invitation_not_found' using errcode='22023';end if;
 if r.state='issued' then
  update control.nexloop_workbench_invitations set state='revoked',revoked_reason='owner_revoked' where invitation_id=r.invitation_id;
  insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,payload,created_at)
   values('workbench-invite-revoke:'||r.invitation_id,'nexloop.workbench_member.revoke_invitation',p_tenant,p_principal,'workbench_member',p_intent,
    jsonb_build_object('invitation_id',r.invitation_id),clock_timestamp());
  return jsonb_build_object('invitation_id',r.invitation_id,'state','revoked','changed',true);
 end if;
 return jsonb_build_object('invitation_id',r.invitation_id,'state',r.state,'changed',false);
end $$;

-- The acceptance's first read (nexloop_identity): the username and role the identity process builds the Human with, or null for every
-- invitation that cannot be accepted now - one answer for every refusal.
create function control.nexloop_workbench_invitation_lookup(p_tenant text,p_token_hmac text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare r control.nexloop_workbench_invitations;
begin
 if session_user<>'nexloop_identity' or coalesce(p_token_hmac,'')!~'^[0-9a-f]{64}$' then raise exception 'identity unavailable' using errcode='42501';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into r from control.nexloop_workbench_invitations where tenant_id=p_tenant and token_hmac=p_token_hmac;
 if not found or r.state<>'issued' or r.expires_at<=clock_timestamp() then return null;end if;
 return jsonb_build_object('invitation_id',r.invitation_id,'username',r.username,'role',r.role);
end $$;

create function control.nexloop_workbench_invitation_failed(p_tenant text,p_token_hmac text) returns void
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
begin
 if session_user<>'nexloop_identity' then raise exception 'identity unavailable' using errcode='42501';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 update control.nexloop_workbench_invitations set failures=failures+1,state=case when failures+1>=5 then 'voided' else state end
  where tenant_id=p_tenant and token_hmac=p_token_hmac and state='issued';
end $$;

create function control.nexloop_accept_workbench_invitation(p_tenant text,p_token_hmac text,p_subject jsonb,p_membership jsonb,p_account jsonb,
  p_hash text,p_source text,p_trace text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on set timezone='UTC' as $$
declare r control.nexloop_workbench_invitations;cur control.nexloop_workbench_configurations;v_subject text;v_principal text;v_account text;v_version integer;now_at timestamptz:=clock_timestamp();
begin
 if session_user<>'nexloop_identity' or coalesce(p_token_hmac,'')!~'^[0-9a-f]{64}$' or p_hash is null or p_hash not like '$argon2id$%' or octet_length(p_hash)>1024
  or (p_source is not null and p_source!~'^[0-9a-f]{64}$') or p_trace is null or length(p_trace) not between 1 and 320
  or jsonb_typeof(p_subject) is distinct from 'object' or jsonb_typeof(p_membership) is distinct from 'object' or jsonb_typeof(p_account) is distinct from 'object' then
  raise exception 'identity unavailable' using errcode='42501';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 perform control.nexloop_workbench_member_lock(p_tenant);
 select * into r from control.nexloop_workbench_invitations where tenant_id=p_tenant and token_hmac=p_token_hmac for update;
 if not found or r.state<>'issued' or r.expires_at<=now_at or not control.nexloop_workbench_member_active(p_tenant,r.issued_by_principal,true) then
  raise exception 'invitation invalid' using errcode='22023';end if;
 -- The Human the identity process built: the shapes 0050's initial identity checks, active, for this tenant and this invitation's username.
 v_subject:=p_subject->>'subject_id';v_principal:=p_membership->>'principal_id';v_account:=p_account->>'local_account_id';
 if v_subject is null or v_principal is null or v_account is null
  or p_subject->>'kind' is distinct from 'human' or p_subject->>'status' is distinct from 'active' or p_subject->'revision' is distinct from '1'::jsonb
  or p_membership->>'tenant_id' is distinct from p_tenant or p_membership->>'subject_id' is distinct from v_subject
  or p_membership->'trusted_attributes' is distinct from '{}'::jsonb or p_membership->>'status' is distinct from 'active'
  or p_membership->'revision' is distinct from '1'::jsonb or p_membership->>'valid_from' is null or (p_membership->>'valid_from')::timestamptz>now_at
  or p_membership->'valid_until' is distinct from 'null'::jsonb
  or p_account->>'tenant_id' is distinct from p_tenant or p_account->>'subject_id' is distinct from v_subject or p_account->>'username' is distinct from r.username
  or p_account->>'status' is distinct from 'active' or p_account->'revision' is distinct from '1'::jsonb or p_account->'session_epoch' is distinct from '1'::jsonb
  or p_account->'failed_attempts' is distinct from '0'::jsonb or p_account->'lockout_level' is distinct from '0'::jsonb
  or p_account->'locked_until' is distinct from 'null'::jsonb or p_account ? 'password_hash' or p_account ? 'password_history' then
  raise exception 'identity unavailable' using errcode='42501';end if;
 if exists(select 1 from control.nexloop_browser_accounts a where a.tenant_id=p_tenant and lower(a.username)=r.username)
  or exists(select 1 from control.nexloop_browser_memberships m where m.tenant_id=p_tenant and m.principal_id=v_principal) then
  raise exception 'invitation invalid' using errcode='22023';end if;
 update control.nexloop_workbench_invitations set accepting_subject=v_subject where invitation_id=r.invitation_id;
 insert into control.nexloop_browser_subjects values(v_subject,p_subject);
 insert into control.nexloop_browser_memberships values(p_tenant,v_principal,v_subject,p_membership);
 insert into control.nexloop_browser_accounts values(p_tenant,v_account,v_subject,p_account->>'username',p_hash,'{}',p_account);
 -- Configuration first (grant_guard checks a grant against its holder's configured role), then the role's grants (design §1c).
 select * into cur from control.nexloop_workbench_configurations where tenant_id=p_tenant order by manifest_version desc limit 1;
 if not found then raise exception 'invitation invalid' using errcode='22023';end if;
 v_version:=control.nexloop_workbench_configuration_append(p_tenant,cur.members||jsonb_build_object(v_principal,r.role),'invitation:'||r.invitation_id);
 perform control.nexloop_workbench_grants_write(p_tenant,v_principal,v_subject,r.role,'grant','invitation:'||r.invitation_id);
 insert into control.nexloop_workbench_role_history(tenant_id,subject_id,principal_id,role,event,source) values(p_tenant,v_subject,v_principal,r.role,'start','invitation:'||r.invitation_id);
 update control.nexloop_workbench_invitations set state='accepted',accepted_principal=v_principal,accepted_at=now_at,acceptor_source=p_source where invitation_id=r.invitation_id;
 insert into control.nexloop_browser_login_events(tenant_id,subject_id,event_type,outcome,operator_principal_id,request_id,trace_id,details,created_at)
  values(p_tenant,v_subject,'invitation_accepted','created',r.issued_by_principal,'workbench-invite:'||r.invitation_id,p_trace,
   jsonb_build_object('invitation_id',r.invitation_id,'account_id',v_account,'principal_id',v_principal,'role',r.role,'configuration_version',v_version,
    'same_source_as_issuer',r.issuer_source is not null and r.issuer_source=p_source),now_at);
 insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,payload,created_at)
  values('workbench-invite-accept:'||r.invitation_id,'nexloop.workbench_member.invitation_accepted',p_tenant,v_principal,'workbench_member','workbench-invite:'||r.invitation_id,
   jsonb_build_object('invitation_id',r.invitation_id,'issued_by',r.issued_by_principal,'principal_id',v_principal,'role',r.role,
    'same_source_as_issuer',r.issuer_source is not null and r.issuer_source=p_source),now_at);
 return jsonb_build_object('invitation_id',r.invitation_id,'principal_id',v_principal,'role',r.role);
end $$;

do $$ declare f text; begin
 foreach f in array array['control.nexloop_authority_digest(jsonb)','control.nexloop_workbench_fact_instance(jsonb,text,text)',
   'control.nexloop_workbench_grants_write(text,text,text,text,text,text)','control.nexloop_publish_workbench_role_templates(text,jsonb)',
   'control.nexloop_workbench_configuration_append(text,jsonb,text)','control.nexloop_governed_invite_workbench_member(text,text,text,text,text,jsonb)',
   'control.nexloop_governed_revoke_workbench_invitation(text,text,text,text,text,jsonb)','control.nexloop_workbench_invitation_lookup(text,text)',
   'control.nexloop_workbench_invitation_failed(text,text)',
   'control.nexloop_accept_workbench_invitation(text,text,jsonb,jsonb,jsonb,text,text,text)'] loop
  execute format('alter function %s owner to nexloop_owner',f);
  execute format('revoke all on function %s from public',f);
 end loop;
end $$;
grant execute on function control.nexloop_publish_workbench_role_templates(text,jsonb) to nexloop_configurator;
grant execute on function control.nexloop_workbench_invitation_lookup(text,text),control.nexloop_workbench_invitation_failed(text,text),
 control.nexloop_accept_workbench_invitation(text,text,jsonb,jsonb,jsonb,text,text,text) to nexloop_identity;
insert into control.nexloop_governed_capabilities(capability,operation,subject_rule,handler,source_task) values
 ('workbench_member.invite','invite_workbench_member','human','control.nexloop_governed_invite_workbench_member(text,text,text,text,text,jsonb)','NX-058'),
 ('workbench_member.revoke_invitation','revoke_workbench_invitation','human','control.nexloop_governed_revoke_workbench_invitation(text,text,text,text,text,jsonb)','NX-058');
