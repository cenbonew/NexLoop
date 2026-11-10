-- NX-067 (docs/design/nx067-oidc-group-roles-and-disable-sync.md, L6 conditional PASS with Z1-Z5; dispatcher 2026-11-10): the two gaps
-- NX-065 found in workbench single sign-on (0365). The migration number is temporary.
--
-- * Maximum SSO session: an SSO session's absolute deadline is its creation plus the provider's max_session_seconds (900-43200, default
--   28800, from the provider file), never more than the 30 days of a password session; its idle deadline is the earlier of 7 days and that.
--   control.nexloop_create_workbench_oidc_session_bounded checks it; the 0365 function keeps its body and loses its grant.
-- * Back-channel logout: control.nexloop_workbench_oidc_backchannel takes a verified logout token's (provider, issuer, sub) and the HMAC of
--   its jti. A jti seen before is a replay (nothing happens); a linked subject's account gets session_epoch + 1, which ends every session
--   of the member (password sessions too, design 3.3); an unlinked subject changes nothing (the caller answers 200 either way). jti HMACs
--   are kept until their deadline and cleared here.
-- * IdP groups only narrow (design 2, Z1, Z2, Z5): an owner sets a provider's mapping {IdP group: operator|reviewer} by the governed Action
--   nexloop.workbench_member.set_sso_group_mapping (append-only revisions). At each SSO login the mapped roles of the ID token's groups
--   become the link's role_ceiling; the member's effective workbench Actions are those of its manifest role that one of the ceiling's
--   roles also holds (an intersection, never another role's extra Actions). No mapping: no ceiling (as before). A configured mapping and
--   a missing or malformed groups claim: the login is refused and the ceiling is left as it is; groups that map to nothing, or an empty
--   intersection: the login is refused and the ceiling becomes {} (no workbench Action, password sessions included). Changing a mapping
--   sets every link of the provider to {} and ends its sessions (a new SSO login computes the new ceiling); only clearing the mapping
--   puts the ceilings back to null. The ceiling is read from the current link row on every check (owner promotion deletes the link, 0365).
--   Enforced three times: where the workbench entry picks the Action (authz.nexloop_workbench_action_for: granted false, 403), where
--   browser authority reads a grant (authz.nexloop_browser_authority_fact_snapshot: withheld, the decision fails closed) and in the
--   governed dispatch's own transaction (authz.nexloop_goal_governed_action: 42501, 403).
--   Every ceiling change is an append-only event and an audit row: roles and mapping revision only, never group names or the subject.
set local timezone = 'UTC';

create table control.nexloop_workbench_oidc_logout_jti (
 tenant_id text not null references control.nexloop_tenants(tenant_id),
 provider_id text not null check(provider_id~'^[a-z][a-z0-9_-]{0,31}$'),
 jti_hmac text not null check(jti_hmac~'^[0-9a-f]{64}$'),
 expires_at timestamptz not null,
 primary key(tenant_id,provider_id,jti_hmac)
);
alter table control.nexloop_workbench_oidc_logout_jti owner to nexloop_owner;
alter table control.nexloop_workbench_oidc_logout_jti enable row level security;
alter table control.nexloop_workbench_oidc_logout_jti force row level security;
create policy tenant_boundary on control.nexloop_workbench_oidc_logout_jti to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
revoke all on control.nexloop_workbench_oidc_logout_jti from public;

-- IdP group mapping (design 2).
alter table control.nexloop_workbench_external_identities add column role_ceiling text[]
 check(role_ceiling is null or role_ceiling<@array['operator','reviewer']::text[]);

create table control.nexloop_workbench_oidc_group_mappings (
 tenant_id text not null references control.nexloop_tenants(tenant_id),
 provider_id text not null check(provider_id~'^[a-z][a-z0-9_-]{0,31}$'),
 revision integer not null check(revision between 1 and 1000000),
 group_claim text not null check(group_claim~'^[A-Za-z][A-Za-z0-9_.:/-]{0,127}$'),
 mappings jsonb not null check(jsonb_typeof(mappings)='object'),
 set_by_principal text not null,request_id text not null check(char_length(request_id) between 1 and 200),set_at timestamptz not null,
 primary key(tenant_id,provider_id,revision),unique(tenant_id,request_id)
);
create table control.nexloop_workbench_role_ceiling_events (
 event_id uuid primary key default gen_random_uuid(),tenant_id text not null references control.nexloop_tenants(tenant_id),
 principal_id text not null,provider_id text not null,from_ceiling text[],to_ceiling text[],
 reason text not null check(reason in ('sso_login','no_mapped_role','mapping_changed','mapping_cleared')),
 mapping_revision integer,request_id text not null check(char_length(request_id) between 1 and 320),actor_principal text not null,created_at timestamptz not null
);
create function control.nexloop_workbench_oidc_mapping_append_only() returns trigger language plpgsql
 set search_path=pg_catalog,pg_temp as $$ begin raise exception '% is append-only',tg_table_name using errcode='42501'; end $$;
do $$ declare t text; begin
 foreach t in array array['control.nexloop_workbench_oidc_group_mappings','control.nexloop_workbench_role_ceiling_events'] loop
  execute format('alter table %s owner to nexloop_owner',t);
  execute format('alter table %s enable row level security',t);
  execute format('alter table %s force row level security',t);
  execute format('create policy tenant_boundary on %s to nexloop_owner using(tenant_id=current_setting(''eios.tenant_id'',true)) with check(tenant_id=current_setting(''eios.tenant_id'',true))',t);
  execute format('revoke all on %s from public',t);
  execute format('create trigger append_only before update or delete on %s for each row execute function control.nexloop_workbench_oidc_mapping_append_only()',t);
 end loop;
end $$;
alter function control.nexloop_workbench_oidc_mapping_append_only() owner to nexloop_owner;
revoke all on function control.nexloop_workbench_oidc_mapping_append_only() from public;


-- The SSO session with the provider's maximum length (nexloop_identity). The body is 0365's with the two deadlines changed.
create function control.nexloop_create_workbench_oidc_session_bounded(p_tenant text,p_application text,p_provider text,p_issuer text,p_external text,
  p_fingerprint text,p_body jsonb,p_token bytea,p_csrf bytea,p_trace text,p_max_seconds integer,p_group_claim text,p_groups jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare facts jsonb;now_at timestamptz;field_name text;expected jsonb;life interval;gm control.nexloop_workbench_oidc_group_mappings;ceiling text[];
 principal text;r text;
begin
 if session_user<>'nexloop_identity' or jsonb_typeof(p_body) is distinct from 'object' or coalesce(p_fingerprint,'')!~'^[0-9a-f]{64}$'
  or p_token is null or p_csrf is null or octet_length(p_token)<>32 or octet_length(p_csrf)<>32 or p_token=p_csrf
  or p_trace is null or length(p_trace) not between 1 and 320 or p_max_seconds is null or p_max_seconds not between 900 and 43200
  or p_body->>'tenant_id' is distinct from p_tenant or p_body->>'application_id' is distinct from p_application then
  raise exception 'identity unavailable' using errcode='42501';end if;
 life:=least(make_interval(secs=>p_max_seconds),interval '30 days');
 perform set_config('eios.tenant_id',p_tenant,true);
 perform control.nexloop_workbench_member_lock(p_tenant);
 facts:=control.nexloop_workbench_oidc_resolve(p_tenant,p_application,p_provider,p_issuer,p_external);
 if facts is null or (facts->>'unavailable')::boolean then raise exception 'authentication rejected' using errcode='EID03';end if;
 expected:=jsonb_build_object('subject_id',facts->'membership'->'subject_id','principal_id',facts->'membership'->'principal_id',
  'membership_revision',facts->'membership'->'revision','subject_kind','human','subject_revision',facts->'subject_revision',
  'credential_kind','local_account','credential_tenant_id',p_tenant,'credential_id',facts->'credential_id',
  'credential_revision',facts->'credential_revision','credential_session_epoch',facts->'credential_session_epoch',
  'provider_id',null,'provider_revision',null,'provider_configuration_fingerprint',null,
  'authentication_methods',jsonb_build_array('oidc'),'restricted',facts->'restricted','application_revision',facts->'application_revision');
 foreach field_name in array array(select jsonb_object_keys(expected)) loop
  if p_body->field_name is distinct from expected->field_name then raise exception 'authentication rejected' using errcode='EID03';end if;
 end loop;
 now_at:=clock_timestamp();
 if p_body->>'session_id' is null or length(p_body->>'session_id') not between 1 and 320 or p_body->'revision' is distinct from '1'::jsonb
  or p_body->'revoked_at' is distinct from 'null'::jsonb or p_body->>'created_at' is null
  or p_body->>'last_seen_at' is distinct from p_body->>'created_at' or p_body->>'idle_expires_at' is null or p_body->>'absolute_expires_at' is null
  or (p_body->>'created_at')::timestamptz>now_at or (p_body->>'created_at')::timestamptz<now_at-interval '1 minute'
  or (p_body->>'idle_expires_at')::timestamptz<>(p_body->>'created_at')::timestamptz+least(interval '7 days',life)
  or (p_body->>'absolute_expires_at')::timestamptz<>(p_body->>'created_at')::timestamptz+life then
  raise exception 'authentication rejected' using errcode='EID03';end if;
 -- Groups (design 2.2): only when the provider has a non-empty mapping. A missing or malformed claim (or a claim name from before a
 -- mapping change) is refused with the ceiling untouched (EID04, rolled back); no mapped role or an empty intersection is refused with
 -- the ceiling set to {} - committed, so the answer is a refusal value, not an exception.
 select * into gm from control.nexloop_workbench_oidc_group_mappings where tenant_id=p_tenant and provider_id=p_provider order by revision desc limit 1;
 if gm.tenant_id is not null and gm.mappings<>'{}'::jsonb then
  if p_group_claim is distinct from gm.group_claim or jsonb_typeof(p_groups) is distinct from 'array' or jsonb_array_length(p_groups)>256
   or exists(select 1 from jsonb_array_elements(p_groups) g where jsonb_typeof(g) is distinct from 'string' or char_length(g#>>'{}') not between 1 and 256) then
   raise exception 'groups invalid' using errcode='EID04';end if;
  principal:=facts->'membership'->>'principal_id';
  ceiling:=array(select distinct gm.mappings->>(g#>>'{}') from jsonb_array_elements(p_groups) g where gm.mappings?(g#>>'{}') order by 1);
  r:=control.nexloop_workbench_role(p_tenant,principal);
  if cardinality(ceiling)=0 or r is null or not exists(select 1 from unnest(control.nexloop_workbench_role_actions(p_tenant,r)) x
    where exists(select 1 from unnest(ceiling) c where x=any(control.nexloop_workbench_role_actions(p_tenant,c)))) then
   perform control.nexloop_workbench_set_role_ceiling(p_tenant,principal,'{}'::text[],'no_mapped_role',gm.revision,'oidc-login:'||(p_body->>'session_id'),'nexloop_identity');
   return jsonb_build_object('refused','no_mapped_role');
  end if;
  perform control.nexloop_workbench_set_role_ceiling(p_tenant,principal,ceiling,'sso_login',gm.revision,'oidc-login:'||(p_body->>'session_id'),'nexloop_identity');
 end if;
 insert into control.nexloop_browser_sessions values(p_body->>'session_id',p_token,p_csrf,p_tenant,p_application,p_body);
 insert into control.nexloop_browser_login_events(tenant_id,subject_id,event_type,outcome,operator_principal_id,request_id,trace_id,details,created_at)
  values(p_tenant,p_body->>'subject_id','oidc_login','success','nexloop_identity','oidc-login:'||(p_body->>'session_id'),p_trace,
   jsonb_build_object('provider_id',p_provider,'provider_configuration_fingerprint',p_fingerprint,'principal_id',p_body->>'principal_id',
    'max_session_seconds',p_max_seconds),now_at);
 return p_body;
end $$;

-- A verified back-channel logout token (nexloop_identity): 'replayed', 'not_linked' or 'ended'. Never says more to the IdP than 200.
create function control.nexloop_workbench_oidc_backchannel(p_tenant text,p_provider text,p_issuer text,p_external text,p_jti_hmac text,
  p_expires_at timestamptz,p_trace text) returns text
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare l control.nexloop_workbench_external_identities;a control.nexloop_browser_accounts;n integer;now_at timestamptz:=clock_timestamp();
begin
 if session_user<>'nexloop_identity' or coalesce(p_provider,'')!~'^[a-z][a-z0-9_-]{0,31}$' or coalesce(p_jti_hmac,'')!~'^[0-9a-f]{64}$'
  or p_issuer is null or p_external is null or char_length(p_external) not between 1 and 255
  or p_expires_at is null or p_expires_at<now_at or p_expires_at>now_at+interval '1 hour'
  or p_trace is null or length(p_trace) not between 1 and 320 then raise exception 'identity unavailable' using errcode='42501';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 delete from control.nexloop_workbench_oidc_logout_jti where tenant_id=p_tenant and expires_at<now_at;
 insert into control.nexloop_workbench_oidc_logout_jti values(p_tenant,p_provider,p_jti_hmac,p_expires_at) on conflict do nothing;
 get diagnostics n=row_count;
 if n=0 then return 'replayed';end if;
 perform control.nexloop_workbench_member_lock(p_tenant);
 select * into l from control.nexloop_workbench_external_identities where tenant_id=p_tenant and provider_id=p_provider and external_subject=p_external;
 if not found or l.issuer is distinct from p_issuer then return 'not_linked';end if;
 a:=control.nexloop_workbench_member_account(p_tenant,l.principal_id);
 if a.local_account_id is not null then perform control.nexloop_workbench_kick(p_tenant,a);end if;
 insert into control.nexloop_browser_login_events(tenant_id,subject_id,event_type,outcome,operator_principal_id,request_id,trace_id,details,created_at)
  values(p_tenant,l.subject_id,'oidc_backchannel_logout','success','nexloop_identity','oidc-backchannel:'||p_jti_hmac,p_trace,
   jsonb_build_object('provider_id',p_provider,'principal_id',l.principal_id),now_at);
 return 'ended';
end $$;

do $$ declare f text; begin
 foreach f in array array['control.nexloop_create_workbench_oidc_session_bounded(text,text,text,text,text,text,jsonb,bytea,bytea,text,integer,text,jsonb)',
   'control.nexloop_workbench_oidc_backchannel(text,text,text,text,text,timestamptz,text)'] loop
  execute format('alter function %s owner to nexloop_owner',f);
  execute format('revoke all on function %s from public',f);
  execute format('grant execute on function %s to nexloop_identity',f);
 end loop;
end $$;
-- The unbounded 0365 session function is no longer called: only the bounded one creates SSO sessions.
revoke execute on function control.nexloop_create_workbench_oidc_session(text,text,text,text,text,text,jsonb,bytea,bytea,text) from nexloop_identity;

-- The refusal audit learns the NX-067 reasons (body of 0365 with only the reason list extended; registered in the copy guard).
create or replace function control.nexloop_workbench_oidc_failed(p_tenant text,p_provider text,p_reason text,p_trace text) returns void
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare id uuid:=gen_random_uuid();
begin
 if session_user<>'nexloop_identity' or (p_provider is not null and p_provider!~'^[a-z][a-z0-9_-]{0,31}$')
  or coalesce(p_reason,'') not in ('state_invalid','provider_error','token_exchange_failed','id_token_invalid','not_linked','member_unavailable','groups_invalid','no_mapped_role','logout_token_invalid')
  or p_trace is null or length(p_trace) not between 1 and 320 then raise exception 'identity unavailable' using errcode='42501';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,payload,created_at)
  values('workbench-sso-failed:'||id,'nexloop.workbench_member.sso_login_failed',p_tenant,'nexloop_identity',p_trace,'workbench-sso-failed:'||id,
   jsonb_build_object('provider_id',p_provider,'reason',p_reason),clock_timestamp());
end $$;

-- ---------------------------------------------------------------------------------------------------------------------------------
-- The Actions one role of the current manifest holds.
create function control.nexloop_workbench_role_actions(p_tenant text,p_role text) returns text[]
 language sql stable security definer set search_path=pg_catalog,pg_temp as $$
 select coalesce(array(select jsonb_array_elements_text((control.nexloop_workbench_current(p_tenant)).roles->p_role)),'{}'::text[]) $$;

-- Z1/Z5: may this member use this Action resource now? Read from the current link row every time (no cache, no copy). Without a link or
-- a ceiling: yes (the grants decide, as before). Resources outside the workbench roles are not the ceiling's. The workbench read needs
-- a non-empty ceiling; any other workbench Action must be in the manifest role and in one of the ceiling's roles.
create function control.nexloop_workbench_action_permitted(p_tenant text,p_principal text,p_resource text) returns boolean
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);c text[];found_link boolean;r text;v boolean;
begin
 -- No person (a machine command carries a client, never a principal): nobody's ceiling; the machine core checks its own grants.
 if p_tenant is null or p_principal is null then return true;end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 select true,l.role_ceiling into found_link,c from control.nexloop_workbench_external_identities l where l.tenant_id=p_tenant and l.principal_id=p_principal;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 if found_link is null or c is null then return true;end if;
 if not (p_resource=any(control.nexloop_workbench_actions(p_tenant))) then return true;end if;
 if p_resource='eios:action:nexloop.workbench.read:1' then return cardinality(c)>0;end if;
 r:=control.nexloop_workbench_role(p_tenant,p_principal);
 v:=r is not null and p_resource=any(control.nexloop_workbench_role_actions(p_tenant,r))
  and exists(select 1 from unnest(c) x where p_resource=any(control.nexloop_workbench_role_actions(p_tenant,x)));
 return coalesce(v,false);
end $$;

-- One ceiling change: the link row, an event and an audit row (only when it changes).
create function control.nexloop_workbench_set_role_ceiling(p_tenant text,p_principal text,p_to text[],p_reason text,p_revision integer,p_request text,p_actor text)
 returns void language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare l control.nexloop_workbench_external_identities;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into l from control.nexloop_workbench_external_identities where tenant_id=p_tenant and principal_id=p_principal for update;
 if not found or l.role_ceiling is not distinct from p_to then return;end if;
 update control.nexloop_workbench_external_identities set role_ceiling=p_to where tenant_id=p_tenant and principal_id=p_principal;
 insert into control.nexloop_workbench_role_ceiling_events(tenant_id,principal_id,provider_id,from_ceiling,to_ceiling,reason,mapping_revision,request_id,actor_principal,created_at)
  values(p_tenant,p_principal,l.provider_id,l.role_ceiling,p_to,p_reason,p_revision,p_request,p_actor,clock_timestamp());
 insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,payload,created_at)
  values('workbench-sso-ceiling:'||gen_random_uuid(),'nexloop.workbench_member.sso_role_ceiling_changed',p_tenant,p_actor,'workbench_member',p_request,
   jsonb_build_object('target_principal',p_principal,'provider_id',l.provider_id,'from',to_jsonb(l.role_ceiling),'to',to_jsonb(p_to),
    'reason',p_reason,'mapping_revision',p_revision),clock_timestamp());
end $$;

-- The group claim name the login must read for a provider (nexloop_identity): null while the provider has no (or a cleared) mapping.
create function control.nexloop_workbench_oidc_group_claim(p_tenant text,p_provider text) returns text
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare m control.nexloop_workbench_oidc_group_mappings;
begin
 if session_user<>'nexloop_identity' then raise exception 'identity unavailable' using errcode='42501';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into m from control.nexloop_workbench_oidc_group_mappings where tenant_id=p_tenant and provider_id=p_provider order by revision desc limit 1;
 return case when m.tenant_id is null or m.mappings='{}'::jsonb then null else m.group_claim end;
end $$;

-- The owner's governed Action (Z2): a new revision. A non-empty mapping (first or changed) sets every link of the provider to {} and
-- ends its sessions; an empty one clears the mapping and puts the provider's ceilings back to null.
create function control.nexloop_governed_set_sso_group_mapping(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare cur control.nexloop_workbench_oidc_group_mappings;prev control.nexloop_workbench_oidc_group_mappings;l record;a control.nexloop_browser_accounts;
 rev integer;cleared boolean;affected integer:=0;
begin
 if jsonb_typeof(body) is distinct from 'object' or exists(select 1 from jsonb_object_keys(body) k
   where k not in ('operation','request_id','provider_id','group_claim','mappings','expected_revision'))
  or jsonb_typeof(body->'request_id') is distinct from 'string' or char_length(body->>'request_id') not between 1 and 200
  or coalesce(body->>'provider_id','')!~'^[a-z][a-z0-9_-]{0,31}$'
  or coalesce(body->>'group_claim','')!~'^[A-Za-z][A-Za-z0-9_.:/-]{0,127}$'
  or jsonb_typeof(body->'mappings') is distinct from 'object' or (select count(*) from jsonb_object_keys(body->'mappings'))>64
  or exists(select 1 from jsonb_each(body->'mappings') e where char_length(e.key) not between 1 and 256 or e.key<>btrim(e.key)
   or jsonb_typeof(e.value) is distinct from 'string' or e.value#>>'{}' not in ('operator','reviewer'))
  or jsonb_typeof(body->'expected_revision') is distinct from 'number' or (body->>'expected_revision')!~'^[0-9]{1,7}$' then
  raise exception 'workbench member: invalid' using errcode='22023';end if;
 if p_world is distinct from 'real' then raise exception 'workbench member: owner_world_only' using errcode='22023';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 perform control.nexloop_workbench_member_lock(p_tenant);
 select * into prev from control.nexloop_workbench_oidc_group_mappings where tenant_id=p_tenant and request_id=body->>'request_id';
 if found then
  if prev.provider_id is distinct from body->>'provider_id' or prev.group_claim is distinct from body->>'group_claim' or prev.mappings is distinct from body->'mappings' then
   raise exception 'workbench member: request_reused' using errcode='22023';end if;
  return jsonb_build_object('provider_id',prev.provider_id,'revision',prev.revision,'replayed',true);
 end if;
 if not control.nexloop_workbench_member_active(p_tenant,p_principal,true) then raise exception 'workbench member: issuer_not_owner' using errcode='22023';end if;
 select * into cur from control.nexloop_workbench_oidc_group_mappings where tenant_id=p_tenant and provider_id=body->>'provider_id' order by revision desc limit 1;
 if coalesce(cur.revision,0)<>(body->>'expected_revision')::integer then raise exception 'workbench member: revision_conflict' using errcode='22023';end if;
 rev:=coalesce(cur.revision,0)+1;cleared:=body->'mappings'='{}'::jsonb;
 insert into control.nexloop_workbench_oidc_group_mappings values(p_tenant,body->>'provider_id',rev,body->>'group_claim',body->'mappings',p_principal,body->>'request_id',clock_timestamp());
 for l in select principal_id from control.nexloop_workbench_external_identities where tenant_id=p_tenant and provider_id=body->>'provider_id' order by principal_id loop
  affected:=affected+1;
  if cleared then
   perform control.nexloop_workbench_set_role_ceiling(p_tenant,l.principal_id,null,'mapping_cleared',rev,body->>'request_id',p_principal);
  else
   perform control.nexloop_workbench_set_role_ceiling(p_tenant,l.principal_id,'{}'::text[],'mapping_changed',rev,body->>'request_id',p_principal);
   a:=control.nexloop_workbench_member_account(p_tenant,l.principal_id);
   if a.local_account_id is not null then perform control.nexloop_workbench_kick(p_tenant,a);end if;
  end if;
 end loop;
 insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,payload,created_at)
  values('workbench-sso-mapping:'||p_tenant||':'||(body->>'request_id'),'nexloop.workbench_member.set_sso_group_mapping',p_tenant,p_principal,'workbench_member',p_intent,
   jsonb_build_object('provider_id',body->>'provider_id','revision',rev,'group_claim',body->>'group_claim','mappings',body->'mappings',
    'previous_mappings',coalesce(cur.mappings,'{}'::jsonb),'links_affected',affected),clock_timestamp());
 return jsonb_build_object('provider_id',body->>'provider_id','revision',rev,'cleared',cleared,'links_affected',affected,'replayed',false);
end $$;

do $$ declare f text; begin
 foreach f in array array['control.nexloop_workbench_role_actions(text,text)','control.nexloop_workbench_action_permitted(text,text,text)',
   'control.nexloop_workbench_set_role_ceiling(text,text,text[],text,integer,text,text)','control.nexloop_workbench_oidc_group_claim(text,text)',
   'control.nexloop_governed_set_sso_group_mapping(text,text,text,text,text,jsonb)'] loop
  execute format('alter function %s owner to nexloop_owner',f);
  execute format('revoke all on function %s from public',f);
 end loop;
end $$;
grant execute on function control.nexloop_workbench_oidc_group_claim(text,text) to nexloop_identity;
insert into control.nexloop_governed_capabilities(capability,operation,subject_rule,handler,source_task) values
 ('workbench_member.set_sso_group_mapping','set_sso_group_mapping','human','control.nexloop_governed_set_sso_group_mapping(text,text,text,text,text,jsonb)','NX-067');

-- Enforcement 1: a browser grant fact the ceiling excludes is withheld (every browser authorization decision reads it, reads included).
alter function authz.nexloop_browser_authority_fact_snapshot(text,text,text,text[]) rename to nexloop_browser_authority_fact_snapshot_before_role_ceiling;
create function authz.nexloop_browser_authority_fact_snapshot(p_digest text,p_world text,p_kind text,p_key text[]) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v jsonb:=authz.nexloop_browser_authority_fact_snapshot_before_role_ceiling(p_digest,p_world,p_kind,p_key);binding jsonb;
begin
 if v is null or p_kind is distinct from 'grants' then return v;end if;
 binding:=authz.nexloop_browser_identity_snapshot(p_digest,p_world)->'binding';
 if control.nexloop_workbench_action_permitted(binding->>'tenant_id',binding->>'subject_principal_id',p_key[2]) then return v;end if;
 -- Excluded: no fact. A stored fact carries its own snapshot checksum, so it is withheld rather than edited; the browser authorization
 -- then fails closed (cannot be verified: 503, nothing runs). The governed dispatch below refuses with 42501 (403) on its own.
 return null;
end $$;
alter function authz.nexloop_browser_authority_fact_snapshot(text,text,text,text[]) owner to nexloop_owner;
revoke all on function authz.nexloop_browser_authority_fact_snapshot(text,text,text,text[]) from public;

-- Enforcement 2: the workbench entry's Action pick says granted false for an Action the ceiling excludes.
alter function authz.nexloop_workbench_action_for(text,text,text) rename to nexloop_workbench_action_for_before_role_ceiling;
create function authz.nexloop_workbench_action_for(p_digest text,p_world text,p_capability text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v jsonb:=authz.nexloop_workbench_action_for_before_role_ceiling(p_digest,p_world,p_capability);binding jsonb;
begin
 if v is null or v->'granted' is distinct from 'true'::jsonb then return v;end if;
 binding:=authz.nexloop_service_identity_snapshot(p_digest,p_world)->'binding';
 if not control.nexloop_workbench_action_permitted(binding->>'tenant_id',binding->>'subject_principal_id','eios:action:'||(v->>'stable_name')||':'||(v->>'version')) then
  return v||jsonb_build_object('granted',false);end if;
 return v;
end $$;
alter function authz.nexloop_workbench_action_for(text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_workbench_action_for(text,text,text) from public;

-- Enforcement 3 (Z5, dispatch): the governed Action's own SQL transaction checks the ceiling again, from the signed command's tenant,
-- principal and Action resource, before the wrapped body verifies and runs it (42501: the workbench answers 403). The wrapped body is
-- the NX-062 M5b dispatcher (0442), so the entry is: this ceiling check, then the machine dispatch (people and services to the 0348 body,
-- machine clients to the machine core). A machine command names no principal: action_permitted is true for it.
alter function authz.nexloop_goal_governed_action(text,text,text,text,text) rename to nexloop_goal_governed_action_before_role_ceiling;
create function authz.nexloop_goal_governed_action(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare a jsonb;
begin
 if octet_length(p_text)>1048576 then raise exception 'goal command too large' using errcode='22023';end if;
 a:=p_text::jsonb;
 if not control.nexloop_workbench_action_permitted(a->>'tenant_id',a->>'principal_id',a->>'action_resource') then
  raise exception 'workbench role ceiling' using errcode='42501';end if;
 return authz.nexloop_goal_governed_action_before_role_ceiling(p_digest,p_world,p_text,p_signature,p_payload);
end $$;
alter function authz.nexloop_goal_governed_action(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_goal_governed_action(text,text,text,text,text) from public;

-- The three wrappers get every EXECUTE grantee of the function they wrap, which keeps none (the 0437 / 0438 pattern).
do $$ declare w text;old text;g text; begin
 foreach w in array array['authz.nexloop_browser_authority_fact_snapshot(text,text,text,text[])','authz.nexloop_workbench_action_for(text,text,text)',
   'authz.nexloop_goal_governed_action(text,text,text,text,text)'] loop
  old:=regexp_replace(w,'\(','_before_role_ceiling(');
  for g in select r.rolname from pg_proc p cross join aclexplode(p.proacl) a join pg_roles r on r.oid=a.grantee
    where p.oid=old::regprocedure and a.privilege_type='EXECUTE' and a.grantee<>p.proowner group by r.rolname loop
   execute format('grant execute on function %s to %I',w,g);
   execute format('revoke execute on function %s from %I',old,g);
  end loop;
 end loop;
end $$;
